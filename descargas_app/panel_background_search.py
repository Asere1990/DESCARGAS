"""Durable, read-only panel searches independent of the browser's lifetime.

Runs only in the web service; existing download workers and Telegram sessions are
untouched. Checkpoints and search hits are encrypted with the existing repo cipher.
"""
import asyncio
import hashlib
import json
import logging
import time
import uuid
import unicodedata

from aiohttp import web
from telethon import errors
from .creator_access import require_account, require_source
from .moderation import authorized

SCHEMA = """
CREATE TABLE IF NOT EXISTS descarga_panel_search_jobs (
 id TEXT PRIMARY KEY, bot_id BIGINT NOT NULL, actor BIGINT NOT NULL,
 access_version TEXT NOT NULL, mode TEXT NOT NULL,
 state TEXT NOT NULL, checkpoint BYTEA NOT NULL,
 total INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
 found INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 owner TEXT, lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
 next_at DOUBLE PRECISION NOT NULL DEFAULT 0,
 created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS descarga_panel_search_due
 ON descarga_panel_search_jobs(bot_id,next_at,created)
 WHERE state IN ('queued','running','waiting');
CREATE INDEX IF NOT EXISTS descarga_panel_search_by_actor
 ON descarga_panel_search_jobs(bot_id,actor,created DESC);
CREATE TABLE IF NOT EXISTS descarga_panel_search_hits (
 seq BIGSERIAL PRIMARY KEY, job_id TEXT NOT NULL
 REFERENCES descarga_panel_search_jobs(id) ON DELETE CASCADE,
 hit_key TEXT NOT NULL, payload BYTEA NOT NULL,
 UNIQUE(job_id,hit_key)
);
CREATE INDEX IF NOT EXISTS descarga_panel_search_results
 ON descarga_panel_search_hits(job_id,seq);
"""

class LeaseLost(Exception):
    """The browser paused or another worker owns the job; keep the scheduler alive."""

LOG=logging.getLogger(__name__)
MAX_SEARCH_SECONDS = 3600  # A forgotten panel search must never run for days.
PAGES_PER_TURN = 8        # Yield the scheduler between bounded read-only batches.

MODES = frozenset(('phrases','name','id','username','phone','country',
                   'channels','channel_id','groups','group_id','bots'))
ENTITIES = frozenset(('channels','channel_id','groups','group_id','bots'))


def fold(value):
    value = unicodedata.normalize('NFD',str(value or ''))
    return ''.join(ch for ch in value if not unicodedata.combining(ch)).casefold().strip()


def id_matches(source, query):
    source,query=str(source or '').strip(),str(query or '').strip()
    if not query.lstrip('-').isdigit():return False
    return source==query or (not query.startswith('-') and (
        (source.startswith('-100') and source[4:]==query) or
        (source.startswith('-') and source[1:]==query)))


def matches(account, mode, query):
    term=fold(query)
    if mode=='name':return bool(term and term in fold(account.get('name')))
    if mode=='id':
        value=''.join(c for c in str(query) if c.isdigit())
        return bool(value and value in str(account.get('id','')))
    if mode=='username':
        value=term.removeprefix('@')
        return bool(value and value in fold(account.get('username')).removeprefix('@'))
    if mode=='phone':
        value=''.join(c for c in str(query) if c.isdigit())
        return bool(value and value in ''.join(c for c in str(account.get('phone','')) if c.isdigit()))
    if mode=='country':return bool(term and (term in fold(account.get('country')) or term in fold(account.get('country_code'))))
    return False


def entity_matches(chat,mode,query):
    if not isinstance(chat,dict):return False
    kind=chat.get('type')
    if mode=='bots':
        if kind!='private' or chat.get('bot') is not True:return False
    elif mode in ('channels','channel_id'):
        if kind!='channel':return False
    elif mode in ('groups','group_id'):
        if kind!='group':return False
    else:return False
    if mode in ('channel_id','group_id'):return id_matches(chat.get('id'),query)
    term=fold(query);username=term.removeprefix('@')
    return not term or term in fold(chat.get('name')) or bool(username and username in fold(chat.get('username')).removeprefix('@'))


class BackgroundPanelSearch:
    def __init__(self,panel):
        self.p=panel
        self.s=panel.s
        self.owner=uuid.uuid4().hex
        self.task=None

    async def start(self,app):
        self.task=asyncio.create_task(self.loop(),name='descargas-panel-search')
    async def stop(self,app):
        if self.task:
            self.task.cancel()
            try:await self.task
            except asyncio.CancelledError:pass
        if getattr(self.s.repo,'pool',None) and callable(getattr(self.s.repo.pool,'execute',None)):
            await self.s.repo.pool.execute(
                """UPDATE descarga_panel_search_jobs
                   SET state='queued',owner=NULL,lease_until=0
                   WHERE bot_id=$1 AND owner=$2 AND state='running'""",
                self.s.bot_id,self.owner)

    def checkpoint(self,row):
        return self.s.repo.decode(row['checkpoint'])

    async def owned(self,actor,version,job_id=''):
        if not authorized(actor):raise web.HTTPForbidden()
        if not getattr(self.s.repo,'pool',None):raise ValueError('El servicio todavía no está disponible.')
        if job_id:
            row=await self.s.repo.pool.fetchrow(
                'SELECT * FROM descarga_panel_search_jobs WHERE id=$1 AND bot_id=$2 AND actor=$3',
                job_id,self.s.bot_id,actor)
        else:
            row=await self.s.repo.pool.fetchrow(
                'SELECT * FROM descarga_panel_search_jobs WHERE bot_id=$1 AND actor=$2 ORDER BY created DESC LIMIT 1',
                self.s.bot_id,actor)
        if row and row['access_version']!=version:raise web.HTTPForbidden()
        return row

    async def active_accounts(self,actor):
        # Uses the existing authenticated, policy-filtered account directory and
        # its generation/fingerprint-validated confirmed active state.
        result=[];after=0;seen=set()
        while True:
            page=await self.p.accounts(actor,after)
            for account in page['items']:
                if account['status'].get('state')=='active' and account['id'] not in seen:
                    result.append({key:account.get(key) for key in ('id','name','username',
                                                'phone','country','country_code','generation')})
                    seen.add(account['id'])
            next_page=page['next']
            if not next_page:break
            if int(next_page)<=after:raise ValueError('La lista de cuentas no avanzó.')
            after=int(next_page)
        return result

    async def begin(self,actor,version,mode,query):
        if mode not in MODES:raise ValueError('Tipo de búsqueda no válido.')
        if not isinstance(query,str) or len(query)>100:raise ValueError('Búsqueda demasiado larga.')
        query=query.strip()
        if mode not in ENTITIES and not query:raise ValueError('Escribe un texto para buscar.')
        if mode in ('channel_id','group_id') and (not query.lstrip('-').isdigit()):
            raise ValueError('Introduce el ID completo del canal o grupo.')
        row=await self.owned(actor,version)
        if row and row['state'] in ('queued','running','waiting'):
            old=self.checkpoint(row)
            if row['mode']==mode and old.get('query')==query:
                return {'job':self.summary(row)}
            raise ValueError('Hay una búsqueda en curso. Entra al panel y pulsa Pausar antes de iniciar otra.')
        accounts=await self.active_accounts(actor)
        if not authorized(actor):raise web.HTTPForbidden()
        now=time.time()
        job_id=uuid.uuid4().hex
        data={'query':query,'accounts':accounts,'index':0,'completed':0,'cursor':'','cursors':[],
              'attempts':0,'errors':[]}
        async with self.s.repo.pool.acquire() as conn,conn.transaction():
            # Only one unpaused job per administrator. This also resolves races
            # from two browser tabs starting searches at nearly the same time.
            await conn.execute('SELECT pg_advisory_xact_lock($1,$2)',
                               int(self.s.bot_id)%2000000000,int(actor)%2000000000)
            # A different query supersedes any previous search atomically.
            # An in-flight worker's next checkpoint will fail its state check
            # and roll back, without modifying Telegram sessions.
            await conn.execute("""UPDATE descarga_panel_search_jobs
                SET state='superseded',owner=NULL,lease_until=0,updated=$3
                WHERE bot_id=$1 AND actor=$2 AND state IN
                ('queued','running','waiting','paused','incomplete')""",
                self.s.bot_id,actor,now)
            await conn.execute("""DELETE FROM descarga_panel_search_jobs
                WHERE bot_id=$1 AND actor=$2 AND created<$3
                  AND state NOT IN ('queued','running','waiting')""",
                self.s.bot_id,actor,now-7*86400)
            await conn.execute(
                """INSERT INTO descarga_panel_search_jobs
                   (id,bot_id,actor,access_version,mode,state,checkpoint,total,
                    created,updated,next_at)
                   VALUES($1,$2,$3,$4,$5,'queued',$6,$7,$8,$8,$8)""",
                job_id,self.s.bot_id,actor,version,mode,
                self.s.repo.encode(data),len(accounts),now)
        return await self.status(actor,version,job_id)

    def summary(self,row):
        data=self.checkpoint(row)
        index=data.get('index',0)
        accounts=data.get('accounts') or []
        return {'id':row['id'],'mode':row['mode'],'query':data.get('query',''),
                'state':row['state'],'total':row['total'],'completed':row['completed'],
                'found':row['found'],'failures':row['failures'],
                'current':accounts[index].get('name','') if index<len(accounts) else '',
                'errors':[{'name':e.get('name',''),'reason':e.get('reason','')}
                          for e in data.get('errors',[])][:40],
                'updated':row['updated']}

    async def status(self,actor,version,job_id=''):
        row=await self.owned(actor,version,job_id)
        return {'job':self.summary(row) if row else None}

    async def control(self,actor,version,job_id,action):
        row=await self.owned(actor,version,job_id)
        if not row:raise ValueError('La búsqueda ya no existe.')
        if action=='pause':
            await self.s.repo.pool.execute(
                """UPDATE descarga_panel_search_jobs SET state='paused',
                   owner=NULL,lease_until=0,updated=$4
                   WHERE id=$1 AND actor=$2 AND bot_id=$3
                     AND state IN ('queued','running','waiting')""",
                job_id,actor,self.s.bot_id,time.time())
        elif action=='resume':
            if row['state'] not in ('paused','incomplete'):
                return await self.status(actor,version,job_id)
            data=self.checkpoint(row)
            if row['state']=='incomplete':
                failures=data.pop('errors',[])
                for failed in failures:
                    data['accounts'].append(failed['account'])
                data['cursor']='';data['cursors']=[];data['attempts']=0
            await self.s.repo.pool.execute(
                """UPDATE descarga_panel_search_jobs
                   SET state='queued',checkpoint=$4,failures=0,owner=NULL,
                       lease_until=0,next_at=$5,updated=$5
                   WHERE id=$1 AND actor=$2 AND bot_id=$3
                     AND state IN ('paused','incomplete')""",
                job_id,actor,self.s.bot_id,self.s.repo.encode(data),time.time())
        elif action=='cancel':
            await self.s.repo.pool.execute(
                """UPDATE descarga_panel_search_jobs SET state='cancelled',
                   owner=NULL,lease_until=0,updated=$4
                   WHERE id=$1 AND actor=$2 AND bot_id=$3
                     AND state IN ('queued','running','waiting','paused','incomplete')""",
                job_id,actor,self.s.bot_id,time.time())
        else:raise ValueError('Acción de búsqueda no válida.')
        return await self.status(actor,version,job_id)

    async def results(self,actor,version,job_id,after=0):
        row=await self.owned(actor,version,job_id)
        if not row:raise ValueError('Búsqueda no encontrada.')
        rows=await self.s.repo.pool.fetch(
            """SELECT seq,payload FROM descarga_panel_search_hits
               WHERE job_id=$1 AND seq>$2 ORDER BY seq LIMIT 81""",
            job_id,after)
        items=[];permissions={}
        for hit in rows[:80]:
            data=self.s.repo.decode(hit['payload'])
            account=data['account'];item=data.get('hit')
            uid=int(account['id'])
            if uid not in permissions:
                try:
                    await require_account(self.s,uid,actor)
                    record=await self.s.repo.account(uid)
                    permissions[uid]=bool(record and record['session'] and
                                          record['generation']==account['generation'])
                except ValueError:permissions[uid]=False
            if not permissions[uid]:continue
            if item and item.get('chat'):
                try:await require_source(self.s,uid,int(item['chat']['id']),actor=actor)
                except ValueError:continue
            items.append(data)
        cursor=rows[min(len(rows),80)-1]['seq'] if rows else after
        return {'items':items,'next':cursor if len(rows)>80 else None,
                'cursor':cursor,'total':row['found']}

    def cursor_hash(self,mode,cursor):
        purpose='overview-search' if mode=='phrases' else 'dialogs'
        payload=self.p.unpack(cursor,purpose)
        return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    async def scan_page(self,row,data):
        mode=row['mode'];query=data['query']
        account=data['accounts'][data['index']]
        uid=int(account['id'])
        # Respect session revocation and generation changes before every page.
        await require_account(self.s,uid,row['actor'])
        current=await self.s.repo.account(uid)
        if not current or not current['session'] or current['generation']!=account['generation']:
            raise ValueError('La sesión fue desconectada o cambió durante la búsqueda.')
        if mode=='phrases':
            page=await self.p.features.overview_search(row['actor'],uid,query,data['cursor'])
            hits=[{'account':account,'hit':item} for item in page['items']]
        elif mode in ENTITIES:
            page=await self.p.chats(row['actor'],uid,data['cursor'])
            hits=[{'account':account,'hit':{'chat':chat}} for chat in page['items']
                  if entity_matches(chat,mode,query)]
        else:
            page={'next':None}
            hits=[{'account':account,'hit':None}] if matches(account,mode,query) else []
        next_cursor=page.get('next')
        if next_cursor:
            cursor_hash=self.cursor_hash(mode,next_cursor)
            if cursor_hash in data['cursors']:raise ValueError('Telegram repitió una página.')
        return hits,next_cursor

    async def checkpoint_page(self,row,data,hits,next_cursor):
        account=data['accounts'][data['index']]
        mode=row['mode']
        if next_cursor:
            data['cursor']=next_cursor
            hashes=data['cursors']
            hashes.append(self.cursor_hash(mode,next_cursor))
            data['cursors']=hashes[-1000:]
        else:
            data['index']+=1;data['completed']=data.get('completed',0)+1;data['cursor']='';data['cursors']=[];data['attempts']=0
        async with self.s.repo.pool.acquire() as conn,conn.transaction():
            inserted=0
            for hit in hits:
                item=hit.get('hit')
                chat=(item or {}).get('chat') or {}
                message=(item or {}).get('message') or {}
                key=':'.join((str(account['id']),str(chat.get('id','metadata')),
                              str(message.get('id','title'))))
                fingerprint=hashlib.sha256(key.encode()).hexdigest()
                ok=await conn.fetchval(
                    """INSERT INTO descarga_panel_search_hits(job_id,hit_key,payload)
                       VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING 1""",
                    row['id'],fingerprint,self.s.repo.encode(hit))
                inserted+=int(bool(ok))
            updated=await conn.fetchrow(
                """UPDATE descarga_panel_search_jobs
                   SET checkpoint=$3,completed=$4,found=found+$5,
                       failures=$6,updated=$7,lease_until=$8
                   WHERE id=$1 AND owner=$2 AND state='running'
                   RETURNING state""",
                row['id'],self.owner,self.s.repo.encode(data),
                data.get('completed',0),inserted,len(data['errors']),
                time.time(),time.time()+120)
            if not updated:raise LeaseLost()
        return inserted

    async def checkpoint_error(self,row,data,exc):
        attempts=data.get('attempts',0)+1
        data['attempts']=attempts
        flood=isinstance(exc,errors.FloodWaitError)
        retry_delay=(max(1,int(exc.seconds))+1) if flood else [0,4,20][min(attempts,2)]
        if not flood and attempts>=3:
            account=data['accounts'][data['index']]
            data['errors'].append({'account':account,'name':account['name'],
                                   'reason':type(exc).__name__,'cursor':data.get('cursor','')})
            data['index']+=1;data['cursor']='';data['cursors']=[];data['attempts']=0
        now=time.time()
        next_at=now+retry_delay
        state='waiting' if flood or (attempts<3 and retry_delay>0) else 'running'
        async with self.s.repo.pool.acquire() as conn:
            await conn.execute(
                """UPDATE descarga_panel_search_jobs
                   SET checkpoint=$3,completed=$4,failures=$5,
                       state=$6,next_at=$7,updated=$8,owner=NULL,lease_until=0
                   WHERE id=$1 AND owner=$2 AND state='running'""",
                row['id'],self.owner,self.s.repo.encode(data),
                data.get('completed',0),len(data['errors']),state,next_at,now)
        return state

    async def claim(self):
        now=time.time()
        return await self.s.repo.pool.fetchrow(
            """UPDATE descarga_panel_search_jobs
               SET state='running',owner=$1,lease_until=$2,updated=$3
               WHERE id=(SELECT id FROM descarga_panel_search_jobs
                  WHERE bot_id=$4 AND state IN ('queued','running','waiting')
                    AND next_at<=$3 AND (state<>'running' OR lease_until<$3)
                  ORDER BY created FOR UPDATE SKIP LOCKED LIMIT 1)
               RETURNING *""",self.owner,now+120,now,self.s.bot_id)

    async def run_claimed(self,row):
        data=self.checkpoint(row)
        pages=0
        while True:
            fresh=await self.s.repo.pool.fetchrow(
                'SELECT state,owner,access_version FROM descarga_panel_search_jobs WHERE id=$1',row['id'])
            if not fresh or fresh['state']!='running' or fresh['owner']!=self.owner:return
            if not authorized(row['actor']) or (await self.s.repo.setting(
                    f"web-panel-version:{self.s.bot_id}:{row['actor']}") or '0')!=row['access_version']:
                await self.s.repo.pool.execute(
                    """UPDATE descarga_panel_search_jobs SET state='paused',
                       owner=NULL,lease_until=0 WHERE id=$1 AND owner=$2""",
                    row['id'],self.owner)
                return
            if time.time()-row['created']>MAX_SEARCH_SECONDS:
                await self.s.repo.pool.execute(
                    """UPDATE descarga_panel_search_jobs
                       SET state='expired',owner=NULL,lease_until=0,updated=$3
                       WHERE id=$1 AND owner=$2 AND state='running'""",
                    row['id'],self.owner,time.time())
                return
            if data['index']>=len(data['accounts']):
                await self.s.repo.pool.execute(
                    """UPDATE descarga_panel_search_jobs
                       SET state=$3,owner=NULL,lease_until=0,updated=$4
                       WHERE id=$1 AND owner=$2 AND state='running'""",
                    row['id'],self.owner,'incomplete' if data['errors'] else 'complete',time.time())
                return
            try:
                hits,next_cursor=await self.scan_page(row,data)
                await self.checkpoint_page(row,data,hits,next_cursor)
                pages+=1
            except asyncio.CancelledError:raise
            except LeaseLost:return
            except Exception as exc:
                next_state=await self.checkpoint_error(row,data,exc)
                if next_state=='waiting':return
            if pages>=PAGES_PER_TURN:
                # Release the durable search lease so ordinary panel requests
                # do not compete against a single uninterrupted search loop.
                await self.s.repo.pool.execute(
                    """UPDATE descarga_panel_search_jobs
                       SET state='queued',owner=NULL,lease_until=0,
                           next_at=$3,updated=$3
                       WHERE id=$1 AND owner=$2 AND state='running'""",
                    row['id'],self.owner,time.time()+2)
                return
            await asyncio.sleep(0.6)

    async def loop(self):
        while True:
            try:
                if not self.s.ready or not getattr(self.s.repo,'pool',None):
                    await asyncio.sleep(2);continue
                row=await self.claim()
                if row:await self.run_claimed(row)
                else:await asyncio.sleep(2)
            except asyncio.CancelledError:raise
            except Exception as exc:
                # Preserve checkpoints and retry on the next scheduler pass;
                # never let a search error crash the web service or log query text.
                LOG.warning('Panel background search scheduler retry: %s',type(exc).__name__)
                await asyncio.sleep(4)
