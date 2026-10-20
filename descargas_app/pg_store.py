"""Shared encrypted registry and durable PostgreSQL queue for scalable deployment."""
import asyncio
from contextlib import asynccontextmanager
import json
import hashlib
import time
import uuid
import asyncpg
from .billing_store import BillingRepository,SCHEMA as BILLING_SCHEMA
from .admin_store import AdminRepository,SCHEMA as ADMIN_SCHEMA

SCHEMA='''
CREATE TABLE IF NOT EXISTS descarga_blocks(
 bot_id BIGINT NOT NULL,target BIGINT NOT NULL,actor BIGINT NOT NULL,created DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(bot_id,target));
CREATE TABLE IF NOT EXISTS descarga_origins(
 bot_id BIGINT NOT NULL,almacen BIGINT NOT NULL,message_id BIGINT NOT NULL,
 uid BIGINT NOT NULL,source BIGINT NOT NULL,PRIMARY KEY(bot_id,almacen,message_id));
CREATE TABLE IF NOT EXISTS descarga_accounts(
 uid BIGINT PRIMARY KEY, session BYTEA, profile BYTEA, generation BIGINT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS descarga_jobs(
 id TEXT PRIMARY KEY, bot_id BIGINT NOT NULL, update_id BIGINT NOT NULL, uid BIGINT NOT NULL,
 generation BIGINT NOT NULL, payload BYTEA NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 position INTEGER NOT NULL DEFAULT 0, count INTEGER NOT NULL DEFAULT 0,
 owner TEXT, heartbeat DOUBLE PRECISION, created DOUBLE PRECISION NOT NULL,
 UNIQUE(bot_id, update_id));
CREATE UNIQUE INDEX IF NOT EXISTS descarga_one_user_job ON descarga_jobs(uid)
 WHERE state IN ('queued','preparing','sending');
CREATE INDEX IF NOT EXISTS descarga_queue ON descarga_jobs(created) WHERE state='queued';
CREATE INDEX IF NOT EXISTS descarga_leases ON descarga_jobs(heartbeat) WHERE state IN ('preparing','sending');
ALTER TABLE descarga_jobs ADD COLUMN IF NOT EXISTS lane TEXT NOT NULL DEFAULT 'fast';
CREATE INDEX IF NOT EXISTS descarga_lane_queue ON descarga_jobs(bot_id,lane,created) WHERE state='queued';
CREATE TABLE IF NOT EXISTS descarga_archives(id TEXT PRIMARY KEY,bot_id BIGINT NOT NULL,payload BYTEA NOT NULL,state TEXT NOT NULL DEFAULT 'queued',owner TEXT,heartbeat DOUBLE PRECISION,created DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS descarga_archive_queue ON descarga_archives(created) WHERE state='queued';
CREATE TABLE IF NOT EXISTS descarga_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS descarga_rate(key TEXT PRIMARY KEY,next_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS descarga_media_cache(
 key TEXT PRIMARY KEY, state TEXT NOT NULL, payload BYTEA, created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS descarga_media_routes(key TEXT PRIMARY KEY,media_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS descarga_backups(
 key TEXT PRIMARY KEY,bot_id BIGINT NOT NULL,payload BYTEA NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued',lease TEXT,available DOUBLE PRECISION NOT NULL DEFAULT 0,
 heartbeat DOUBLE PRECISION,attempts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS descarga_backup_pending ON descarga_backups(bot_id,available) WHERE state<>'done';
'''

SCHEMA+=BILLING_SCHEMA+ADMIN_SCHEMA

class Repository(BillingRepository,AdminRepository):
    def __init__(self,url,cipher,pool_size=10):
        self.url=url;self.cipher=cipher;self.pool_size=pool_size;self.pool=None
        self.owner=uuid.uuid4().hex
    async def start(self):
        self.pool=await asyncpg.create_pool(self.url,min_size=1,max_size=self.pool_size,
            command_timeout=20,max_inactive_connection_lifetime=60)
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(741,1)')
            await conn.execute(SCHEMA)
    async def close(self):
        if self.pool:await self.pool.close()
    def encode(self,data):return self.cipher.encrypt(json.dumps(data,ensure_ascii=False).encode())
    def decode(self,data):return json.loads(self.cipher.decrypt(bytes(data)))
    async def is_blocked(self,bot_id,uid,source=None):
        return bool(await self.pool.fetchval('SELECT 1 FROM descarga_blocks WHERE bot_id=$1 AND target=ANY($2::bigint[]) UNION ALL SELECT 1 FROM descarga_dispute_blocks WHERE bot_id=$1 AND uid=ANY($2::bigint[]) LIMIT 1',bot_id,[uid]+([source] if source is not None else [])))
    async def set_blocks(self,bot_id,targets,blocked,actor):
        async with self.pool.acquire() as conn,conn.transaction():
            if blocked:
                await conn.executemany('INSERT INTO descarga_blocks VALUES($1,$2,$3,$4) ON CONFLICT(bot_id,target) DO UPDATE SET actor=excluded.actor,created=excluded.created',[(bot_id,t,actor,time.time()) for t in targets])
            else:await conn.execute('DELETE FROM descarga_blocks WHERE bot_id=$1 AND target=ANY($2::bigint[])',bot_id,targets)
    async def register_origins(self,bot_id,almacen,ids,uid,source):
        if source is None:raise ValueError('Missing source provenance')
        await self.pool.executemany('INSERT INTO descarga_origins VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING',[(bot_id,almacen,mid,uid,source) for mid in ids])
    async def bind_cached_origin(self,bot_id,almacen,ids,source,identity):
        # Preserve the first requester's attribution when enriching older cached albums.
        if ids and await self.archive_origin(bot_id,almacen,ids[0]):return
        row=await self.pool.fetchrow('SELECT payload FROM descarga_backups WHERE key=$1 AND bot_id=$2',identity,bot_id)
        if row:
            uid=self.decode(row['payload']).get('uid')
            if isinstance(uid,int) and uid>0:await self.register_origins(bot_id,almacen,ids,uid,source)
    async def archive_origin(self,bot_id,almacen,message_id):
        return await self.pool.fetchrow('SELECT uid,source FROM descarga_origins WHERE bot_id=$1 AND almacen=$2 AND message_id=$3',bot_id,almacen,message_id)

    async def account(self,uid):
        row=await self.pool.fetchrow('SELECT * FROM descarga_accounts WHERE uid=$1',uid)
        return row
    async def save_account(self,uid,session,profile):
        await self.pool.execute('''INSERT INTO descarga_accounts(uid,session,profile) VALUES($1,$2,$3)
            ON CONFLICT(uid) DO UPDATE SET session=excluded.session,profile=excluded.profile''',
            uid,self.cipher.encrypt(session.encode()),self.encode(profile))
    async def setting(self,key):return await self.pool.fetchval('SELECT value FROM descarga_settings WHERE key=$1',key)
    @asynccontextmanager
    async def media_lock(self,key):
        slot=int.from_bytes(hashlib.sha256(key.encode()).digest()[:4],'big',signed=True)
        async with self.pool.acquire() as conn:
            while not await conn.fetchval('SELECT pg_try_advisory_lock(748,$1)',slot):
                await asyncio.sleep(0.5)
            try:yield
            finally:await conn.execute('SELECT pg_advisory_unlock(748,$1)',slot)
    async def media_cache(self,key):
        return await self.pool.fetchrow('SELECT state,payload FROM descarga_media_cache WHERE key=$1',key)
    async def media_publishing(self,key):
        await self.pool.execute("INSERT INTO descarga_media_cache VALUES($1,'publishing',NULL,$2)",key,time.time())
    async def media_ready(self,key,payload,backup=None):
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute("UPDATE descarga_media_cache SET state='ready',payload=$2 WHERE key=$1",key,self.encode(payload))
            if backup:
                await conn.execute('INSERT INTO descarga_backups(key,bot_id,payload) VALUES($1,$2,$3) ON CONFLICT(key) DO NOTHING',key,backup['bot_id'],self.encode(backup))
    async def backup_enqueue(self,key,bot_id,payload):
        await self.pool.execute('INSERT INTO descarga_backups(key,bot_id,payload) VALUES($1,$2,$3) ON CONFLICT(key) DO NOTHING',key,bot_id,self.encode(payload))
    async def backup_leased(self,key,lease,bot_id):
        return await self.pool.fetchrow("SELECT payload FROM descarga_backups WHERE key=$1 AND lease=$2 AND bot_id=$3 AND state='working' AND heartbeat>$4-180",key,lease,bot_id,time.time())
    async def backup_claim(self,bot_id):
        now=time.time();lease=uuid.uuid4().hex
        return await self.pool.fetchrow("""WITH candidate AS (
            SELECT key FROM descarga_backups WHERE bot_id=$1 AND available<=$2
            AND (state='queued' OR (state='working' AND heartbeat<$2-180))
            ORDER BY available,key FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE descarga_backups b SET state='working',lease=$3,heartbeat=$2,attempts=attempts+1
            FROM candidate c WHERE b.key=c.key RETURNING b.*""",bot_id,now,lease)
    async def backup_update(self,key,lease,operation):
        if operation=='heartbeat':
            result=await self.pool.execute("UPDATE descarga_backups SET heartbeat=$3 WHERE key=$1 AND lease=$2 AND state='working'",key,lease,time.time())
        else:
            result=await self.pool.execute("UPDATE descarga_backups SET state=$3,available=$4,lease=NULL WHERE key=$1 AND lease=$2 AND state='working'",key,lease,'done' if operation=='done' else 'suppressed' if operation=='suppressed' else 'queued',time.time()+300)
        return result=='UPDATE 1'
    def route_key(self,bot_id,link):
        return hashlib.sha256(f'{bot_id}:{link}'.encode()).hexdigest()
    async def remember_media_route(self,bot_id,link,media_key):
        await self.pool.execute('INSERT INTO descarga_media_routes VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET media_key=excluded.media_key',self.route_key(bot_id,link),media_key)
    async def set_setting(self,key,value):
        await self.pool.execute('INSERT INTO descarga_settings VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=excluded.value',key,str(value))
    @asynccontextmanager
    async def account_lock(self,uid,wait=True):
        # A dedicated session lock serializes downloads, login and logout across processes.
        async with self.pool.acquire() as conn:
            if wait:await conn.execute('SELECT pg_advisory_lock($1)',uid)
            elif not await conn.fetchval('SELECT pg_try_advisory_lock($1)',uid):
                raise ValueError('Espera a que termine la transferencia en curso y vuelve a intentarlo.')
            try:yield
            finally:await conn.execute('SELECT pg_advisory_unlock($1)',uid)
    @asynccontextmanager
    async def leader(self,bot_id):
        async with self.pool.acquire() as conn:
            # Negative namespace avoids positive user IDs used by account_lock.
            await conn.execute('SELECT pg_advisory_lock($1)',-bot_id)
            try:yield
            finally:await conn.execute('SELECT pg_advisory_unlock($1)',-bot_id)
    async def enqueue(self,bot_id,update_id,uid,links,name,limit=10000,priority=0):
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(0)')
            if await conn.fetchval('SELECT 1 FROM descarga_jobs WHERE bot_id=$1 AND update_id=$2',bot_id,update_id):return 'duplicate'
            account=await conn.fetchrow('SELECT generation,session FROM descarga_accounts WHERE uid=$1 FOR UPDATE',uid)
            if not account or not account['session']:return 'disconnected'
            if await conn.fetchval("SELECT 1 FROM descarga_jobs WHERE uid=$1 AND state IN ('queued','preparing','sending')",uid):return 'busy'
            if await conn.fetchval("SELECT count(*) FROM descarga_jobs WHERE state IN ('queued','preparing','sending')")>=limit:return 'full'
            # A routing hint is never access authorization. Workers still fetch the
            # source with the requester's session before delivering cached media.
            known=await conn.fetchval("SELECT 1 FROM descarga_media_routes r LEFT JOIN descarga_media_cache c ON c.key=r.media_key LEFT JOIN descarga_private_cache p ON p.key=r.media_key WHERE r.key=$1 AND (c.state='ready' OR p.key IS NOT NULL)",self.route_key(bot_id,links[0]))
            await conn.execute('''INSERT INTO descarga_jobs(id,bot_id,update_id,uid,generation,payload,created,lane,priority)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)''',uuid.uuid4().hex,bot_id,update_id,uid,account['generation'],
                self.encode({'links':links,'name':name,'seen':[]}),time.time(),'fast' if known else 'slow',priority)
            await conn.execute('INSERT INTO descarga_directory(bot_id,uid,name) VALUES($1,$2,$3) ON CONFLICT(bot_id,uid) DO UPDATE SET name=excluded.name',bot_id,uid,self.encode({'name':name}))
            return 'queued'
    async def claim(self,bot_id,lane=None):
        return await self.pool.fetchrow('''WITH candidate AS (
            SELECT id FROM descarga_jobs WHERE state='queued' AND bot_id=$1 AND ($4::text IS NULL OR lane=$4)
            ORDER BY priority DESC,created FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE descarga_jobs j SET state='preparing',owner=$2,heartbeat=$3
            FROM candidate c WHERE j.id=c.id RETURNING j.*''',bot_id,uuid.uuid4().hex,time.time(),lane)
    async def reroute(self,job,lane):
        if lane not in ('fast','slow'):raise ValueError('Invalid queue lane')
        result=await self.pool.execute("UPDATE descarga_jobs SET lane=$3,state='queued',owner=NULL,heartbeat=NULL WHERE id=$1 AND owner=$2 AND state='preparing'",job['id'],job['owner'],lane)
        if result!='UPDATE 1':raise RuntimeError('Lost job ownership or delivery already started')
    async def heartbeat(self,job):
        return await self.pool.execute("UPDATE descarga_jobs SET heartbeat=$3 WHERE id=$1 AND owner=$2 AND state IN ('preparing','sending')",job['id'],job['owner'],time.time())
    async def state(self,job,state):
        result=await self.pool.execute('UPDATE descarga_jobs SET state=$3,heartbeat=$4 WHERE id=$1 AND owner=$2',job['id'],job['owner'],state,time.time())
        if result!='UPDATE 1':raise RuntimeError('Lost job ownership')
    async def checkpoint(self,job,position,count,payload,source=0,source_name=None):
        async with self.pool.acquire() as conn,conn.transaction():
            old=await conn.fetchrow('SELECT count,position FROM descarga_jobs WHERE id=$1 AND owner=$2 FOR UPDATE',job['id'],job['owner'])
            if not old:raise RuntimeError('Lost job ownership')
            await conn.execute("UPDATE descarga_jobs SET position=$3,count=$4,payload=$5,state='preparing',heartbeat=$6 WHERE id=$1 AND owner=$2",job['id'],job['owner'],position,count,self.encode(payload),time.time())
            await self.record_delivery_on(conn,job,position-1,count-old['count'],source)
            if source and source_name:
                await conn.execute('UPDATE descarga_source_totals SET name=$3 WHERE bot_id=$1 AND source=$2',job['bot_id'],source,self.encode({'name':source_name}))
    async def recover(self):
        # No automatic replay after sending starts: Telegram may already have accepted it.
        async with self.pool.acquire() as conn,conn.transaction():
            cutoff=time.time()-180
            await conn.execute("UPDATE descarga_jobs SET state='queued',owner=NULL WHERE state='preparing' AND heartbeat<$1",cutoff)
            await conn.execute("UPDATE descarga_jobs SET state='review',owner=NULL WHERE state='sending' AND heartbeat<$1",cutoff)
            await conn.execute("UPDATE descarga_archives SET state='review',owner=NULL WHERE state='sending' AND heartbeat<$1",cutoff)
            await conn.execute("UPDATE descarga_archives SET state='queued',owner=NULL WHERE state='preparing' AND heartbeat<$1",cutoff)
            await conn.execute("DELETE FROM descarga_archives WHERE state IN ('done','review') AND created<$1",time.time()-7*86400)
            await conn.execute("DELETE FROM descarga_jobs WHERE state IN ('done','failed','cancelled','review') AND created<$1",time.time()-7*86400)
    async def cancel_user(self,uid):
        async with self.pool.acquire() as conn,conn.transaction():
            await conn.execute('UPDATE descarga_accounts SET generation=generation+1 WHERE uid=$1',uid)
            await conn.execute("UPDATE descarga_jobs SET state='cancelled' WHERE uid=$1 AND state='queued'",uid)
    async def erase_session(self,uid):
        await self.pool.execute('UPDATE descarga_accounts SET session=NULL WHERE uid=$1',uid)
    async def enqueue_archive(self,bot_id,uid,name,ids):
        key=hashlib.sha256(f'{bot_id}:{uid}:{ids}'.encode()).hexdigest()
        await self.pool.execute("INSERT INTO descarga_archives(id,bot_id,payload,created) VALUES($1,$2,$3,$4) ON CONFLICT(id) DO NOTHING",
            key,bot_id,self.encode({'uid':uid,'name':name,'ids':ids}),time.time())
    async def claim_archive(self,bot_id):
        return await self.pool.fetchrow("""WITH candidate AS (
            SELECT id FROM descarga_archives WHERE state='queued' AND bot_id=$1 ORDER BY created FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE descarga_archives j SET state='preparing',owner=$2,heartbeat=$3
            FROM candidate c WHERE j.id=c.id RETURNING j.*""",bot_id,uuid.uuid4().hex,time.time())
    async def archive_state(self,job,state):
        result=await self.pool.execute('UPDATE descarga_archives SET state=$3,heartbeat=$4 WHERE id=$1 AND owner=$2',job['id'],job['owner'],state,time.time())
        if result!='UPDATE 1':raise RuntimeError('Lost archive ownership')
    async def archive_heartbeat(self,job):
        await self.pool.execute('UPDATE descarga_archives SET heartbeat=$3 WHERE id=$1 AND owner=$2',job['id'],job['owner'],time.time())

    async def try_pace(self,key,interval):
        # Decorative updates may consume an idle slot, never extend an existing send queue.
        now=time.time()
        slot=await self.pool.fetchval('''INSERT INTO descarga_rate VALUES($1,$2)
            ON CONFLICT(key) DO UPDATE SET next_at=$2 WHERE descarga_rate.next_at<=$3
            RETURNING key''',key,now+interval,now)
        return slot is not None
    async def pace(self,key,interval):
        async with self.pool.acquire() as conn,conn.transaction():
            now=time.time()
            slot=await conn.fetchval('''INSERT INTO descarga_rate VALUES($1,$2)
                ON CONFLICT(key) DO UPDATE SET next_at=greatest(descarga_rate.next_at,$3)+$4
                RETURNING next_at-$4''',key,now+interval,now,interval)
        await asyncio.sleep(max(0,slot-time.time()))
