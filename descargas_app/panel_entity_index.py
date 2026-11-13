"""Encrypted, per-administrator groups/channels index for fast repeat searches.

Only a completely scanned account is eligible for cache hits. Private chats,
private messages and actor-specific Telegram handles are never stored here.
"""
import hashlib
import os
import time

from .creator_access import require_source

SCHEMA = """
CREATE TABLE IF NOT EXISTS descarga_panel_entity_index (
 bot_id BIGINT NOT NULL, actor BIGINT NOT NULL, uid BIGINT NOT NULL,
 source BIGINT NOT NULL, generation BIGINT NOT NULL,
 session_tag TEXT NOT NULL, category TEXT NOT NULL,
 payload BYTEA NOT NULL, seen_at DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(bot_id,actor,uid,source)
);
CREATE INDEX IF NOT EXISTS descarga_panel_entity_by_id
 ON descarga_panel_entity_index(bot_id,actor,category,source,uid);
CREATE INDEX IF NOT EXISTS descarga_panel_entity_by_account
 ON descarga_panel_entity_index(bot_id,actor,uid);
CREATE TABLE IF NOT EXISTS descarga_panel_entity_scans (
 bot_id BIGINT NOT NULL, actor BIGINT NOT NULL, uid BIGINT NOT NULL,
 generation BIGINT NOT NULL, session_tag TEXT NOT NULL,
 completed_at DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(bot_id,actor,uid)
);
"""

ENTITY_MODES=frozenset(('channels','channel_id','groups','group_id'))
DEFAULT_TTL=12*3600


def session_tag(row):
    """Fingerprint of the encrypted linked-account session (never returned)."""
    return hashlib.md5(bytes(row['session'])).hexdigest()


def id_candidates(mode,query):
    if mode not in ('channel_id','group_id'):return None
    term=str(query).strip()
    if not term.lstrip('-').isdigit():return []
    if term.startswith('-'):return [int(term)]
    return [int('-100'+term),-int(term),int(term)]


def stored_chat(panel,chat,actor,uid,generation):
    """Validate an authenticated dialog handle, then strip its actor binding."""
    if not isinstance(chat,dict) or chat.get('type') not in ('group','channel'):
        return None
    try:
        data=panel.unpack(chat.get('handle',''),'peer')
        if (int(data.get('actor',0))!=actor or int(data.get('uid',0))!=uid
            or int(data.get('generation',-1))!=generation):
            return None
        kind=data['kind']
        peerid=int(data['id'])
        ahash=int(data.get('hash') or 0)
        if (peerid<=0 or kind not in ('chat','channel')
            or (chat['type']=='channel' and kind!='channel')
            or (kind=='channel' and ahash<=0)):
            return None
        source=-(1000000000000+peerid) if kind=='channel' else -peerid
        if str(source)!=str(chat.get('id')):return None
        return {'id':str(source),'type':chat['type'],
                'name':str(chat.get('name') or '')[:160],
                'username':str(chat.get('username') or '')[:120],
                'peer':{'kind':kind,'id':peerid,'hash':ahash}}
    except (TypeError,ValueError,KeyError,AttributeError):
        return None


class EntityIndex:
    def __init__(self,panel):
        self.p=panel
        self.s=panel.s

    @staticmethod
    def ttl():
        try:return max(900,min(7*86400,int(os.getenv(
            'PANEL_ENTITY_INDEX_TTL_SECONDS',str(DEFAULT_TTL)))))
        except (ValueError,TypeError):return DEFAULT_TTL

    async def lookup(self,actor,accounts,mode,query,matcher):
        """Use local PostgreSQL only; never open a linked Telegram session."""
        if mode not in ENTITY_MODES or not accounts or not callable(
                getattr(self.p,'pack',None)):
            return [],accounts,[]
        by_uid={int(a['id']):a for a in accounts}
        scans=await self.s.repo.pool.fetch("""
            SELECT s.uid,s.generation,s.session_tag
            FROM descarga_panel_entity_scans s
            JOIN descarga_accounts a ON a.uid=s.uid
            WHERE s.bot_id=$1 AND s.actor=$2
              AND s.uid=ANY($3::bigint[])
              AND s.completed_at>=$4 AND a.session IS NOT NULL
              AND a.generation=s.generation AND md5(a.session)=s.session_tag
        """,self.s.bot_id,actor,list(by_uid),time.time()-self.ttl())
        valid={int(r['uid']):r for r in scans if
               int(r['generation'])==int(by_uid[int(r['uid'])]['generation'])}
        cached=[a for a in accounts if int(a['id']) in valid]
        pending=[a for a in accounts if int(a['id']) not in valid]
        if not cached:return cached,pending,[]
        category='channel' if mode.startswith('channel') else 'group'
        records=await self.s.repo.pool.fetch("""
            SELECT uid,source,category,generation,session_tag,payload
            FROM descarga_panel_entity_index
            WHERE bot_id=$1 AND actor=$2
              AND uid=ANY($3::bigint[]) AND category=$4
              AND ($5::bigint[] IS NULL OR source=ANY($5::bigint[]))
            ORDER BY uid,source
        """,self.s.bot_id,actor,list(valid),category,id_candidates(mode,query))
        hits=[]
        for item in records:
            uid=int(item['uid']);scan=valid.get(uid)
            if (not scan or item['generation']!=scan['generation']
                or item['session_tag']!=scan['session_tag']):continue
            try:
                await require_source(self.s,uid,int(item['source']),actor=actor)
                chat=self.s.repo.decode(item['payload'])
                peer=chat.pop('peer')
                if not isinstance(peer,dict) or peer.get('kind') not in ('chat','channel'):
                    continue
                chat['handle']=self.p.pack('peer',actor=actor,uid=uid,
                    generation=int(scan['generation']),**peer)
                if matcher(chat,mode,query):
                    hits.append({'account':by_uid[uid],'hit':{'chat':chat}})
            except (TypeError,ValueError,KeyError,AttributeError):
                continue
        return cached,pending,hits

    async def store_page(self,conn,actor,account,key,dialogs,started,complete):
        """Called under the search checkpoint transaction, with no new RPCs."""
        uid=int(account['id']);generation=int(account['generation'])
        values=[(self.s.bot_id,actor,uid,int(d['id']),generation,key,
                 d['type'],self.s.repo.encode(d),started) for d in dialogs]
        if values:
            await conn.executemany("""
                INSERT INTO descarga_panel_entity_index(
                    bot_id,actor,uid,source,generation,session_tag,category,
                    payload,seen_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT(bot_id,actor,uid,source)
                DO UPDATE SET generation=excluded.generation,
                    session_tag=excluded.session_tag,category=excluded.category,
                    payload=excluded.payload,seen_at=excluded.seen_at
                WHERE descarga_panel_entity_index.seen_at<=excluded.seen_at
            """,values)
        if not complete:return
        # Publish only after the final page and remove peers that disappeared.
        await conn.execute("""
            DELETE FROM descarga_panel_entity_index
            WHERE bot_id=$1 AND actor=$2 AND uid=$3
              AND (generation<>$4 OR session_tag<>$5 OR seen_at<$6)
        """,self.s.bot_id,actor,uid,generation,key,started)
        await conn.execute("""
            INSERT INTO descarga_panel_entity_scans(
                bot_id,actor,uid,generation,session_tag,completed_at)
            VALUES($1,$2,$3,$4,$5,$6)
            ON CONFLICT(bot_id,actor,uid)
            DO UPDATE SET generation=excluded.generation,
                session_tag=excluded.session_tag,completed_at=excluded.completed_at
            WHERE descarga_panel_entity_scans.completed_at<=excluded.completed_at
        """,self.s.bot_id,actor,uid,generation,key,time.time())
