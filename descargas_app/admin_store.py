"""Durable, idempotent delivery counters and short-lived administrator selections."""
import time

SCHEMA='''
CREATE TABLE IF NOT EXISTS descarga_directory(bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,name BYTEA,country TEXT NOT NULL DEFAULT 'ZZ',PRIMARY KEY(bot_id,uid));
ALTER TABLE descarga_directory ADD COLUMN IF NOT EXISTS last_seen DOUBLE PRECISION NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS descarga_activity_recent ON descarga_directory(bot_id,last_seen DESC);
CREATE INDEX IF NOT EXISTS descarga_directory_country ON descarga_directory(bot_id,country,uid);
CREATE TABLE IF NOT EXISTS descarga_delivery_events(job TEXT NOT NULL,position INTEGER NOT NULL,bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,source BIGINT NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(job,position));
CREATE TABLE IF NOT EXISTS descarga_user_totals(bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,total BIGINT NOT NULL DEFAULT 0,PRIMARY KEY(bot_id,uid));
CREATE INDEX IF NOT EXISTS descarga_user_ranking ON descarga_user_totals(bot_id,total DESC);
CREATE TABLE IF NOT EXISTS descarga_source_totals(bot_id BIGINT NOT NULL,source BIGINT NOT NULL,total BIGINT NOT NULL DEFAULT 0,name BYTEA,PRIMARY KEY(bot_id,source));
CREATE INDEX IF NOT EXISTS descarga_source_ranking ON descarga_source_totals(bot_id,total DESC);
CREATE TABLE IF NOT EXISTS descarga_user_sources(bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,source BIGINT NOT NULL,total BIGINT NOT NULL DEFAULT 0,PRIMARY KEY(bot_id,uid,source));
CREATE TABLE IF NOT EXISTS descarga_admin_selections(bot_id BIGINT NOT NULL,admin BIGINT NOT NULL,chat BIGINT NOT NULL,payload BYTEA NOT NULL,expires DOUBLE PRECISION NOT NULL,PRIMARY KEY(bot_id,admin,chat));
ALTER TABLE descarga_jobs ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS descarga_queue_priority ON descarga_jobs(bot_id,lane,priority DESC,created) WHERE state='queued';
'''

class AdminRepository:
    async def touch_activity(self,bot_id,uid,name=None):
        await self.pool.execute('''INSERT INTO descarga_directory(bot_id,uid,name,last_seen) VALUES($1,$2,$3,$4)
            ON CONFLICT(bot_id,uid) DO UPDATE SET last_seen=excluded.last_seen,name=COALESCE(excluded.name,descarga_directory.name)
            WHERE descarga_directory.last_seen<$4-30''',bot_id,uid,self.encode({'name':name}) if name else None,time.time())

    async def remember_person(self,bot_id,uid,name=None,country=None):
        await self.pool.execute('''INSERT INTO descarga_directory(bot_id,uid,name,country) VALUES($1,$2,$3,$4) ON CONFLICT(bot_id,uid) DO UPDATE SET name=COALESCE(excluded.name,descarga_directory.name),country=CASE WHEN excluded.country='ZZ' THEN descarga_directory.country ELSE excluded.country END''',bot_id,uid,self.encode({'name':name}) if name else None,country or 'ZZ')

    async def record_delivery(self,job,position,count,source=0):
        async with self.pool.acquire() as c,c.transaction():
            await self.record_delivery_on(c,job,position,count,source)

    async def record_delivery_on(self,c,job,position,count,source=0):
        if count<=0:return
        bot,uid=job['bot_id'],job['uid'];source=source or 0
        fresh=await c.fetchval('INSERT INTO descarga_delivery_events VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING RETURNING job',job['id'],position,bot,uid,source,count)
        if not fresh:return
        await c.execute('INSERT INTO descarga_user_totals VALUES($1,$2,$3) ON CONFLICT(bot_id,uid) DO UPDATE SET total=descarga_user_totals.total+excluded.total',bot,uid,count)
        if source:
            await c.execute('INSERT INTO descarga_source_totals(bot_id,source,total) VALUES($1,$2,$3) ON CONFLICT(bot_id,source) DO UPDATE SET total=descarga_source_totals.total+excluded.total',bot,source,count)
            await c.execute('INSERT INTO descarga_user_sources VALUES($1,$2,$3,$4) ON CONFLICT(bot_id,uid,source) DO UPDATE SET total=descarga_user_sources.total+excluded.total',bot,uid,source,count)
