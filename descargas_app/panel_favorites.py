"""Personal panel favorites. Metadata-only; never opens or changes a Telegram session."""
import time
from types import SimpleNamespace

from aiohttp import web

from .creator_access import require_account, require_source
from .moderation import authorized

SCHEMA = """
CREATE TABLE IF NOT EXISTS descarga_panel_favorites (
 bot_id BIGINT NOT NULL, actor BIGINT NOT NULL, uid BIGINT NOT NULL,
 source BIGINT NOT NULL, kind TEXT NOT NULL, payload BYTEA NOT NULL,
 created DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(bot_id,actor,uid,source)
);
CREATE INDEX IF NOT EXISTS descarga_panel_favorites_by_actor
 ON descarga_panel_favorites(bot_id,actor,created DESC);
-- Separate lightweight account bookmarks: never modify chat favorites,
-- Telegram sessions, download jobs, or other account metadata.
CREATE TABLE IF NOT EXISTS descarga_panel_favorite_accounts (
 bot_id BIGINT NOT NULL, actor BIGINT NOT NULL, uid BIGINT NOT NULL,
 payload BYTEA NOT NULL, created DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(bot_id,actor,uid)
);
CREATE INDEX IF NOT EXISTS descarga_panel_favorite_accounts_by_actor
 ON descarga_panel_favorite_accounts(bot_id,actor,created DESC);
"""


def clean_name(value, fallback='Sin nombre'):
    if not isinstance(value,str):
        return fallback
    return ' '.join(value.split())[:160] or fallback


class PanelFavorites:
    def __init__(self,panel):
        self.p=panel
        self.s=panel.s

    async def listing(self,actor):
        if not authorized(actor):
            raise web.HTTPForbidden()
        rows=await self.s.repo.pool.fetch(
            """SELECT f.uid,f.source,f.kind,f.payload,
                      a.generation,a.session IS NOT NULL AS connected
               FROM descarga_panel_favorites f
               LEFT JOIN descarga_accounts a ON a.uid=f.uid
               WHERE f.bot_id=$1 AND f.actor=$2
               ORDER BY f.created DESC LIMIT 1000""",
            self.s.bot_id,actor)
        items=[]
        for row in rows:
            uid=int(row['uid'])
            try:
                await require_source(self.s,uid,int(row['source']),actor=actor)
            except ValueError:
                continue
            data=self.s.repo.decode(row['payload'])
            generation=row['generation']
            available=bool(row['connected'] and generation==data.get('generation'))
            peer=data.get('peer') or {}
            handle=self.p.pack('peer',actor=actor,uid=uid,generation=generation,**peer) if available else None
            items.append({'account_id':str(uid),'id':str(row['source']),
                          'name':data.get('name','Sin nombre'),
                          'username':data.get('username',''),
                          'account':data.get('account','Cuenta de Telegram'),
                          'type':row['kind'],'handle':handle,
                          'generation':generation,'available':available})
        # A separate category means bookmarking an account never occupies a
        # chat favorite slot or touches an account's saved Telegram session.
        rows=await self.s.repo.pool.fetch("""
            SELECT f.uid,f.payload,a.session IS NOT NULL AS connected,
                   a.generation
            FROM descarga_panel_favorite_accounts f
            LEFT JOIN descarga_accounts a ON a.uid=f.uid
            WHERE f.bot_id=$1 AND f.actor=$2
            ORDER BY f.created DESC LIMIT 1000
        """,self.s.bot_id,actor)
        accounts=[]
        for row in rows:
            uid=int(row['uid'])
            try:
                await require_account(self.s,uid,actor)
            except ValueError:
                continue
            saved=self.s.repo.decode(row['payload'])
            if not isinstance(saved,dict):
                continue
            accounts.append({
                'id':str(uid),
                'name':clean_name(saved.get('name'),'Cuenta de Telegram'),
                'username':clean_name(saved.get('username'),''),
                'generation':row['generation'],
                'available':bool(row['connected']),
            })
        return {'items':items,'accounts':accounts}

    async def toggle_account(self,actor,data):
        """Actor-scoped, encrypted bookmark; no client connection or mutation."""
        from .web_panel import integer
        if not authorized(actor):
            raise web.HTTPForbidden()
        if not isinstance(data,dict):
            raise ValueError('Cuenta favorita no válida.')
        uid=integer(data.get('account'))
        wanted=data.get('favorite')
        if not isinstance(wanted,bool):
            raise ValueError('Indica si quieres marcar o desmarcar la cuenta.')
        if not wanted:
            # Removing one's own bookmark is possible after disconnecting.
            await self.s.repo.pool.execute("""
                DELETE FROM descarga_panel_favorite_accounts
                WHERE bot_id=$1 AND actor=$2 AND uid=$3
            """,self.s.bot_id,actor,uid)
            return {'favorite':False}
        await require_account(self.s,uid,actor)
        exists=await self.s.repo.pool.fetchval(
            'SELECT 1 FROM descarga_accounts WHERE uid=$1',uid)
        if not exists:
            raise ValueError('La cuenta ya no está vinculada al panel.')
        payload=self.s.repo.encode({
            'name':clean_name(data.get('name'),'Cuenta de Telegram'),
            'username':clean_name(data.get('username'),''),
        })
        await self.s.repo.pool.execute("""
            INSERT INTO descarga_panel_favorite_accounts
                (bot_id,actor,uid,payload,created)
            VALUES($1,$2,$3,$4,$5)
            ON CONFLICT(bot_id,actor,uid)
            DO UPDATE SET payload=excluded.payload
        """,self.s.bot_id,actor,uid,payload,time.time())
        return {'favorite':True}

    async def toggle(self,actor,data):
        from .web_panel import integer,source_id
        if not authorized(actor):
            raise web.HTTPForbidden()
        if not isinstance(data,dict):
            raise ValueError('Favorito no válido.')
        uid=integer(data.get('account'))
        peer=self.p.unpack(data.get('peer'),'peer')
        if peer.get('uid')!=uid or peer.get('actor')!=actor:
            raise web.HTTPForbidden()
        source=source_id(peer)
        kind=data.get('type')
        if kind not in ('private','group','channel'):
            raise ValueError('Tipo de conversación no válido.')
        if (peer['kind'] in ('user','self'))!=(kind=='private') or (
                peer['kind']=='chat' and kind!='group'):
            raise ValueError('Tipo de conversación no válido.')
        await require_account(self.s,uid,actor)
        await require_source(self.s,uid,source,actor=actor,
            entity=SimpleNamespace(bot=bool(peer.get('is_bot')),
                                   username=peer.get('username','')))
        connected=await self.s.repo.pool.fetchrow(
            'SELECT generation,session IS NOT NULL AS connected FROM descarga_accounts WHERE uid=$1',
            uid)
        if not connected or not connected['connected'] or connected['generation']!=peer.get('generation'):
            raise ValueError('La sesión cambió. Recarga la lista antes de guardar este favorito.')
        wanted=data.get('favorite')
        if not isinstance(wanted,bool):
            raise ValueError('Indica si quieres marcar o desmarcar este favorito.')
        if wanted:
            values={k:peer[k] for k in ('kind','id','hash','is_bot','username') if k in peer}
            payload=self.s.repo.encode({'name':clean_name(data.get('name')),
                'username':clean_name(data.get('username'),''),
                'account':clean_name(data.get('account_name'),'Cuenta de Telegram'),
                'generation':connected['generation'],'peer':values})
            await self.s.repo.pool.execute(
                """INSERT INTO descarga_panel_favorites
                   (bot_id,actor,uid,source,kind,payload,created)
                   VALUES($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT(bot_id,actor,uid,source) DO UPDATE
                   SET kind=excluded.kind,payload=excluded.payload""",
                self.s.bot_id,actor,uid,source,kind,payload,time.time())
        else:
            await self.remove(actor,uid,source)
        return {'favorite':wanted}

    async def remove(self,actor,uid,source):
        if not authorized(actor):
            raise web.HTTPForbidden()
        # Removal touches only this actor's saved bookmark, even if the
        # corresponding Telegram account was subsequently disconnected.
        await self.s.repo.pool.execute(
            """DELETE FROM descarga_panel_favorites
               WHERE bot_id=$1 AND actor=$2 AND uid=$3 AND source=$4""",
            self.s.bot_id,actor,uid,source)
        return {'favorite':False}
