"""Policies and idempotent subscription grants in the existing private database."""
import time
import secrets

SCHEMA='''
CREATE TABLE IF NOT EXISTS descarga_star_receipts(bot_id BIGINT NOT NULL,charge TEXT NOT NULL,uid BIGINT NOT NULL,token TEXT NOT NULL,PRIMARY KEY(bot_id,charge));
CREATE TABLE IF NOT EXISTS descarga_disputes(id TEXT PRIMARY KEY,charge TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',available DOUBLE PRECISION NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS descarga_dispute_blocks(bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,dispute TEXT NOT NULL,PRIMARY KEY(bot_id,uid,dispute));
CREATE TABLE IF NOT EXISTS descarga_policies(bot_id BIGINT NOT NULL,target BIGINT NOT NULL,pay BOOLEAN NOT NULL DEFAULT FALSE,locked BOOLEAN NOT NULL DEFAULT FALSE,actor BIGINT NOT NULL,PRIMARY KEY(bot_id,target));
CREATE TABLE IF NOT EXISTS descarga_memberships(bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,provider TEXT NOT NULL,subscription TEXT NOT NULL,paid_until DOUBLE PRECISION NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'active',changed DOUBLE PRECISION NOT NULL DEFAULT 0,PRIMARY KEY(bot_id,provider,subscription));
CREATE INDEX IF NOT EXISTS descarga_member_uid ON descarga_memberships(bot_id,uid,paid_until);
ALTER TABLE descarga_memberships ADD COLUMN IF NOT EXISTS confirmed BOOLEAN NOT NULL DEFAULT TRUE;
CREATE TABLE IF NOT EXISTS descarga_payment_joins(bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,joined DOUBLE PRECISION NOT NULL,remove_after DOUBLE PRECISION NOT NULL,removed BOOLEAN NOT NULL DEFAULT FALSE,PRIMARY KEY(bot_id,uid));
CREATE TABLE IF NOT EXISTS descarga_payment_notices(invoice TEXT PRIMARY KEY,bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',created DOUBLE PRECISION NOT NULL,available DOUBLE PRECISION NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS descarga_buyer_profiles(invoice TEXT PRIMARY KEY,bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,payload BYTEA NOT NULL,state TEXT NOT NULL DEFAULT 'pending',available DOUBLE PRECISION NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS descarga_payment_intents(token TEXT PRIMARY KEY,bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,provider TEXT NOT NULL,created DOUBLE PRECISION NOT NULL,url TEXT,subscription TEXT);
CREATE TABLE IF NOT EXISTS descarga_payment_events(provider TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(provider,id));
CREATE TABLE IF NOT EXISTS descarga_stripe_periods(subscription TEXT NOT NULL,invoice TEXT PRIMARY KEY,paid_until DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS descarga_stripe_states(subscription TEXT PRIMARY KEY,state TEXT NOT NULL,changed DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS descarga_private_cache(key TEXT PRIMARY KEY,bot_id BIGINT NOT NULL,payload BYTEA NOT NULL,created DOUBLE PRECISION NOT NULL);
'''

class BillingRepository:
    async def policy_set(self,bot_id,target,field,value,actor):
        if field not in ('pay','locked'):raise ValueError('Invalid policy')
        await self.pool.execute(f'''INSERT INTO descarga_policies(bot_id,target,{field},actor) VALUES($1,$2,$3,$4)
            ON CONFLICT(bot_id,target) DO UPDATE SET {field}=excluded.{field},actor=excluded.actor''',bot_id,target,value,actor)
    async def policy(self,bot_id,uid,source=None):
        from .moderation import authorized
        if authorized(uid):return {'pay':False,'locked':False}
        row=await self.pool.fetchrow('SELECT COALESCE(bool_or(pay),false) AS pay,COALESCE(bool_or(locked),false) AS locked FROM descarga_policies WHERE bot_id=$1 AND target=ANY($2::bigint[])',bot_id,[uid]+([source] if source is not None else []))
        return dict(row)
    async def member_until(self,bot_id,uid):
        return await self.pool.fetchval("SELECT COALESCE(max(paid_until),0) FROM descarga_memberships WHERE bot_id=$1 AND uid=$2 AND confirmed AND state NOT IN ('refunded','revoked','incomplete_expired')",bot_id,uid)
    async def payment_intent(self,bot_id,uid,provider):
        row=await self.pool.fetchrow('SELECT * FROM descarga_payment_intents WHERE bot_id=$1 AND uid=$2 AND provider=$3 AND created>$4 AND subscription IS NULL ORDER BY created DESC LIMIT 1',bot_id,uid,provider,time.time()-3600)
        if row:return dict(row)
        token='d_'+secrets.token_urlsafe(24)
        await self.pool.execute('INSERT INTO descarga_payment_intents(token,bot_id,uid,provider,created) VALUES($1,$2,$3,$4,$5)',token,bot_id,uid,provider,time.time())
        return {'token':token,'uid':uid,'bot_id':bot_id,'provider':provider,'url':None}
    async def intent(self,token):
        return await self.pool.fetchrow('SELECT * FROM descarga_payment_intents WHERE token=$1',token)
    async def invoice_url(self,token,url):
        await self.pool.execute('UPDATE descarga_payment_intents SET url=$2 WHERE token=$1',token,url)
    async def grant_stars(self,bot_id,uid,token,charge,until):
        async with self.pool.acquire() as c,c.transaction():
            row=await c.fetchrow("SELECT * FROM descarga_payment_intents WHERE token=$1 AND bot_id=$2 AND uid=$3 AND provider='stars' FOR UPDATE",token,bot_id,uid)
            if not row:raise ValueError('Invalid payment owner')
            fresh=await c.fetchval("INSERT INTO descarga_payment_events VALUES('stars',$1) ON CONFLICT DO NOTHING RETURNING id",charge)
            await c.execute('INSERT INTO descarga_star_receipts VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING',bot_id,charge,uid,token)
            if not fresh:return False
            subscription=row['subscription'] or charge
            await c.execute('UPDATE descarga_payment_intents SET subscription=$2 WHERE token=$1',token,subscription)
            await c.execute("INSERT INTO descarga_memberships(bot_id,uid,provider,subscription,paid_until,state,changed) VALUES($1,$2,'stars',$3,$4,'active',$5) ON CONFLICT(bot_id,provider,subscription) DO UPDATE SET paid_until=GREATEST(descarga_memberships.paid_until,excluded.paid_until),state='active'",bot_id,uid,subscription,until,time.time())
            return True
    async def refund_stars(self,bot_id,token,charge):
        async with self.pool.acquire() as c,c.transaction():
            row=await c.fetchrow("SELECT uid,subscription FROM descarga_payment_intents WHERE bot_id=$1 AND token=$2 AND provider='stars' FOR UPDATE",bot_id,token)
            if not row or not row['subscription']:return None
            owner=await c.fetchval('SELECT uid FROM descarga_star_receipts WHERE bot_id=$1 AND charge=$2 AND token=$3',bot_id,charge,token)
            if owner is None and row['subscription']==charge:owner=row['uid']
            if owner!=row['uid']:return None
            await c.execute('INSERT INTO descarga_dispute_blocks VALUES($1,$2,$3) ON CONFLICT DO NOTHING',bot_id,owner,'stars:'+charge)
            await c.execute("UPDATE descarga_memberships SET state='refunded' WHERE bot_id=$1 AND uid=$2 AND provider='stars' AND subscription=$3",bot_id,owner,row['subscription'])
            return owner

    async def stripe_event(self,event,checkout=None,period=None,state=None,require_group=False):
        async with self.pool.acquire() as c,c.transaction():
            # Event ordering and invoice-before-checkout are handled in one transaction.
            await c.execute('SELECT pg_advisory_xact_lock(751,1)')
            if not await c.fetchval("INSERT INTO descarga_payment_events VALUES('stripe',$1) ON CONFLICT DO NOTHING RETURNING id",event['id']):return None
            if checkout:
                token,subscription,until=checkout
                row=await c.fetchrow("SELECT * FROM descarga_payment_intents WHERE token=$1 AND provider='stripe' FOR UPDATE",token)
                if not row:return None
                if row['subscription'] and row['subscription']!=subscription:raise ValueError('Payment reference already redeemed')
                bound=await c.fetchrow("SELECT uid,bot_id FROM descarga_memberships WHERE provider='stripe' AND subscription=$1",subscription)
                if bound and (bound['uid']!=row['uid'] or bound['bot_id']!=row['bot_id']):raise ValueError('Subscription ownership mismatch')
                pending=await c.fetchval('SELECT COALESCE(max(paid_until),0) FROM descarga_stripe_periods WHERE subscription=$1',subscription)
                prior=await c.fetchrow('SELECT state,changed FROM descarga_stripe_states WHERE subscription=$1',subscription)
                await c.execute('UPDATE descarga_payment_intents SET subscription=$2 WHERE token=$1',token,subscription)
                joined=await c.fetchval('SELECT joined FROM descarga_payment_joins WHERE bot_id=$1 AND uid=$2',row['bot_id'],row['uid'])
                confirmed=not require_group or bool(joined and joined>=event['created']-600)
                await c.execute("INSERT INTO descarga_memberships(bot_id,uid,provider,subscription,paid_until,state,changed,confirmed) VALUES($1,$2,'stripe',$3,$4,$5,$6,$7) ON CONFLICT(bot_id,provider,subscription) DO UPDATE SET paid_until=GREATEST(descarga_memberships.paid_until,excluded.paid_until),confirmed=descarga_memberships.confirmed OR excluded.confirmed",row['bot_id'],row['uid'],subscription,max(until,pending),prior['state'] if prior else 'active',prior['changed'] if prior else event['created'],confirmed)
                return row['uid']
            if period:
                subscription,invoice,until=period
                await c.execute('INSERT INTO descarga_stripe_periods VALUES($1,$2,$3) ON CONFLICT DO NOTHING',subscription,invoice,until)
                row=await c.fetchrow("UPDATE descarga_memberships SET paid_until=GREATEST(paid_until,$2) WHERE provider='stripe' AND subscription=$1 RETURNING uid",subscription,until)
                return row['uid'] if row else None
            if state:
                subscription,status=state
                await c.execute('INSERT INTO descarga_stripe_states VALUES($1,$2,$3) ON CONFLICT(subscription) DO UPDATE SET state=excluded.state,changed=excluded.changed WHERE descarga_stripe_states.changed<=excluded.changed',subscription,status,event['created'])
                await c.execute("UPDATE descarga_memberships SET state=$2,changed=$3 WHERE provider='stripe' AND subscription=$1 AND changed<=$3",subscription,status,event['created'])
    async def private_cache(self,key):
        row=await self.pool.fetchrow('SELECT payload FROM descarga_private_cache WHERE key=$1',key)
        return self.decode(row['payload']) if row else None
    async def private_ready(self,key,bot_id,payload):
        await self.pool.execute('INSERT INTO descarga_private_cache VALUES($1,$2,$3,$4) ON CONFLICT(key) DO NOTHING',key,bot_id,self.encode(payload),time.time())
    async def cached_file_payload(self,key,bot_id):
        row=await self.pool.fetchrow('SELECT payload FROM descarga_backups WHERE key=$1 AND bot_id=$2',key,bot_id)
        return self.decode(row['payload']) if row else None

    async def confirm_payment_join(self,bot_id,uid):
        now=time.time()
        async with self.pool.acquire() as c,c.transaction():
            fresh=await c.fetchval('INSERT INTO descarga_payment_joins VALUES($1,$2,$3,$4,FALSE) ON CONFLICT(bot_id,uid) DO UPDATE SET joined=excluded.joined,remove_after=excluded.remove_after,removed=FALSE WHERE descarga_payment_joins.removed OR descarga_payment_joins.joined<$5 RETURNING uid',bot_id,uid,now,now+300,now-60)
            await c.execute("UPDATE descarga_memberships SET confirmed=TRUE WHERE bot_id=$1 AND uid=$2 AND provider='stripe'",bot_id,uid)
            return fresh is not None
    async def queue_buyer(self,invoice,bot_id,uid,payload):
        if not invoice:return
        async with self.pool.acquire() as c,c.transaction():
            await c.execute('INSERT INTO descarga_buyer_profiles(invoice,bot_id,uid,payload) VALUES($1,$2,$3,$4) ON CONFLICT(invoice) DO NOTHING',invoice,bot_id,uid,self.encode(payload))
            await c.execute('INSERT INTO descarga_payment_notices(invoice,bot_id,uid,created) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING',invoice,bot_id,uid,time.time())
