"""Integration tests: isolated local PostgreSQL only; never contact Telegram."""
import asyncio
import os
import time
import unittest
from urllib.parse import urlparse
from cryptography.fernet import Fernet
from descargas_app.pg_store import Repository

URL=os.environ.get('TEST_DATABASE_URL','')

@unittest.skipUnless(URL,'Requires isolated TEST_DATABASE_URL')
class PostgresIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed=urlparse(URL)
        if parsed.hostname not in ('localhost','127.0.0.1') or parsed.path!='/descargas_test':
            raise RuntimeError('Integration tests refuse nonlocal/non-test databases')
        self.repo=Repository(URL,Fernet(Fernet.generate_key()),pool_size=12)
        await self.repo.start()
        await self.repo.pool.execute('TRUNCATE descarga_payment_joins,descarga_buyer_profiles,descarga_policies,descarga_memberships,descarga_payment_intents,descarga_payment_events,descarga_stripe_periods,descarga_stripe_states,descarga_private_cache,descarga_blocks,descarga_origins,descarga_backups,descarga_jobs,descarga_archives,descarga_accounts,descarga_settings,descarga_rate,descarga_media_cache,descarga_media_routes')
    async def asyncTearDown(self):await self.repo.close()
    async def test_monthly_grants_replay_renewal_and_invoice_before_checkout(self):
        intent=await self.repo.payment_intent(100,10,'stars')
        self.assertTrue(await self.repo.grant_stars(100,10,intent['token'],'first',2000))
        self.assertFalse(await self.repo.grant_stars(100,10,intent['token'],'first',4000))
        self.assertTrue(await self.repo.grant_stars(100,10,intent['token'],'renewal',3000))
        self.assertEqual(await self.repo.member_until(100,10),3000)
        with self.assertRaises(ValueError):await self.repo.grant_stars(100,20,intent['token'],'stolen',9000)
        intent=await self.repo.payment_intent(100,20,'stripe')
        event={'id':'invoice-first','created':100}
        await self.repo.stripe_event(event,period=('sub_own','in_1',5000))
        await self.repo.stripe_event({'id':'checkout','created':101},checkout=(intent['token'],'sub_own',4000))
        self.assertEqual(await self.repo.member_until(100,20),5000)
        await self.repo.stripe_event({'id':'late-failure','created':102},state=('sub_own','past_due'))
        self.assertEqual(await self.repo.member_until(100,20),5000)
        await self.repo.stripe_event({'id':'cancel','created':104},state=('sub_own','canceled'))
        await self.repo.stripe_event({'id':'old-active','created':103},state=('sub_own','active'))
        self.assertEqual(await self.repo.pool.fetchval("SELECT state FROM descarga_memberships WHERE provider='stripe'"),'canceled')
        self.assertEqual(await self.repo.member_until(100,20),5000)

    async def test_paid_membership_requires_own_group_entry_and_keeps_encrypted_profile(self):
        intent=await self.repo.payment_intent(100,10,'stripe')
        until=time.time()+1000
        await self.repo.stripe_event({'id':'paid','created':int(time.time())},checkout=(intent['token'],'sub_group',until),require_group=True)
        self.assertEqual(await self.repo.member_until(100,10),0)
        await self.repo.confirm_payment_join(100,20)
        self.assertEqual(await self.repo.member_until(100,10),0)
        await self.repo.confirm_payment_join(100,10)
        self.assertEqual(await self.repo.member_until(100,10),until)
        await self.repo.queue_buyer('in_own',100,10,{'email':'buyer@example.test','card_fingerprint':'sensitive'})
        row=await self.repo.pool.fetchrow('SELECT payload FROM descarga_buyer_profiles')
        self.assertNotIn(b'buyer@example.test',bytes(row['payload']))
        self.assertEqual(self.repo.decode(row['payload'])['email'],'buyer@example.test')

    async def test_pay_and_lock_independent_and_encrypted_private_cache(self):
        await self.repo.policy_set(100,-100123,'pay',True,50)
        await self.repo.policy_set(100,10,'locked',True,50)
        self.assertEqual(await self.repo.policy(100,10,-100123),{'pay':True,'locked':True})
        await self.repo.policy_set(100,10,'locked',False,50)
        self.assertEqual(await self.repo.policy(100,10,-100123),{'pay':True,'locked':False})
        await self.repo.private_ready('private',100,{'items':[{'file_id':'secret-private-id'}]})
        row=await self.repo.pool.fetchrow('SELECT payload FROM descarga_private_cache')
        self.assertNotIn(b'secret-private-id',bytes(row['payload']))
        self.assertEqual((await self.repo.private_cache('private'))['items'][0]['file_id'],'secret-private-id')
        self.assertEqual(await self.repo.pool.fetchval('SELECT count(*) FROM descarga_backups'),0)

    async def test_persistent_blocks_and_album_provenance(self):
        await self.repo.register_origins(100,-100999,[91,92],10,-100123)
        self.assertEqual(dict(await self.repo.archive_origin(100,-100999,92)),{'uid':10,'source':-100123})
        self.assertIsNone(await self.repo.archive_origin(200,-100999,92))
        await self.repo.register_origins(100,-100999,[91],20,-100456)
        self.assertEqual((await self.repo.archive_origin(100,-100999,91))['uid'],10)
        await self.repo.set_blocks(100,[10,-100123],True,50)
        self.assertTrue(await self.repo.is_blocked(100,10))
        self.assertTrue(await self.repo.is_blocked(100,20,-100123))
        self.assertFalse(await self.repo.is_blocked(200,10,-100123))
        await self.repo.set_blocks(100,[10],False,50)
        self.assertFalse(await self.repo.is_blocked(100,10))
        self.assertTrue(await self.repo.is_blocked(100,10,-100123))
        await self.repo.set_blocks(100,[-100123],False,50)
        self.assertFalse(await self.repo.is_blocked(100,10,-100123))

    async def test_backup_atomic_outbox_claim_retry_and_fencing(self):
        identity='a'*64
        payload={'bot_id':100,'items':[{'file_id':'private-file-id'}]}
        await self.repo.media_publishing(identity)
        await self.repo.media_ready(identity,{'ids':[101]},backup=payload)
        row=await self.repo.backup_claim(100)
        self.assertEqual(self.repo.decode(row['payload']),payload)
        self.assertNotIn(b'private-file-id',bytes(row['payload']))
        self.assertIsNone(await self.repo.backup_claim(100))
        self.assertIsNone(await self.repo.backup_claim(999))
        self.assertFalse(await self.repo.backup_update(identity,'wrong','done'))
        await self.repo.pool.execute("UPDATE descarga_backups SET heartbeat=$1",time.time()-200)
        newer=await self.repo.backup_claim(100)
        self.assertNotEqual(newer['lease'],row['lease'])
        self.assertFalse(await self.repo.backup_update(identity,row['lease'],'done'))
        self.assertTrue(await self.repo.backup_update(identity,newer['lease'],'retry'))
        self.assertIsNone(await self.repo.backup_claim(100))
        await self.repo.pool.execute("UPDATE descarga_backups SET available=0")
        final=await self.repo.backup_claim(100)
        self.assertTrue(await self.repo.backup_update(identity,final['lease'],'done'))
        await self.repo.backup_enqueue(identity,100,payload)
        self.assertIsNone(await self.repo.backup_claim(100))
    async def test_decorative_progress_never_extends_a_busy_send_queue(self):
        self.assertTrue(await self.repo.try_pace('bot-send',.06))
        reserved=time.time()+20
        await self.repo.pool.execute("UPDATE descarga_rate SET next_at=$1 WHERE key='bot-send'",reserved)
        self.assertFalse(await self.repo.try_pace('bot-send',.06))
        self.assertEqual(await self.repo.pool.fetchval("SELECT next_at FROM descarga_rate WHERE key='bot-send'"),reserved)
    async def account(self,uid):await self.repo.save_account(uid,'authorized-session',{'phone':'+14155552671','phone_country':'US'})
    async def test_cache_cross_process_lock_and_encrypted_reference(self):
        other=Repository(URL,self.repo.cipher,pool_size=4);await other.start()
        published=[]
        async def request(repo):
            async with repo.media_lock('shared-album'):
                row=await repo.media_cache('shared-album')
                if row:return repo.decode(row['payload'])
                await repo.media_publishing('shared-album')
                published.append(1)
                await asyncio.sleep(0.05)
                await repo.media_ready('shared-album',{'ids':[101,102]})
                return {'ids':[101,102]}
        try:
            results=await asyncio.gather(request(self.repo),request(other),request(self.repo))
            self.assertEqual(len(published),1)
            self.assertEqual(results,[{'ids':[101,102]}]*3)
            row=await self.repo.media_cache('shared-album')
            self.assertNotIn(b'101',bytes(row['payload']))
        finally:await other.close()
    async def test_fast_and_download_lanes_are_independent_and_fenced(self):
        for uid in (1,2):await self.account(uid);await self.enqueue(uid)
        await self.repo.pool.execute("UPDATE descarga_jobs SET lane='fast'")
        first=await self.repo.claim(100,'fast')
        await self.repo.reroute(first,'slow')
        download=await self.repo.claim(100,'slow')
        fast=await self.repo.claim(100,'fast')
        self.assertNotEqual(download['id'],fast['id'])
        self.assertEqual(download['id'],first['id'])
        with self.assertRaises(RuntimeError):await self.repo.reroute(first,'fast')
        await self.repo.state(download,'sending')
        with self.assertRaises(RuntimeError):await self.repo.reroute(download,'fast')
        await self.repo.checkpoint(download,1,1,{'links':['a','b'],'seen':[]})
        await self.repo.reroute(download,'fast')
        resumed=await self.repo.claim(100,'fast')
        self.assertEqual(resumed['position'],1)
        self.assertEqual(resumed['count'],1)
    async def test_enqueue_routes_known_links_without_using_hint_as_authorization(self):
        link='https://t.me/c/123/1'
        await self.repo.media_publishing('cached')
        await self.repo.media_ready('cached',{'ids':[55]})
        await self.repo.remember_media_route(100,link,'cached')
        for uid in (1,2):await self.account(uid)
        await self.repo.enqueue(100,1,1,[link],'Known')
        await self.repo.enqueue(100,2,2,['https://t.me/c/999/8'],'New')
        self.assertEqual((await self.repo.claim(100,'fast'))['uid'],1)
        self.assertEqual((await self.repo.claim(100,'slow'))['uid'],2)
        self.assertEqual(await self.repo.enqueue(100,3,999,[link],'Disconnected'),'disconnected')
    async def enqueue(self,uid,update=None):
        return await self.repo.enqueue(100,uid if update is None else update,uid,['https://t.me/c/123/1'],'Tester')
    async def test_01_encryption_and_preserved_profile(self):
        await self.account(10)
        account=await self.repo.account(10)
        self.assertNotIn(b'14155552671',bytes(account['profile']))
        self.assertEqual(self.repo.decode(account['profile'])['phone_country'],'US')
        await self.repo.cancel_user(10);await self.repo.erase_session(10)
        account=await self.repo.account(10)
        self.assertIsNone(account['session']);self.assertTrue(account['profile'])
        self.assertEqual(account['generation'],1)
    async def test_02_duplicate_and_one_active_order(self):
        await self.account(20)
        results=await asyncio.gather(*(self.enqueue(20,5) for _ in range(30)))
        self.assertEqual(results.count('queued'),1);self.assertEqual(results.count('duplicate'),29)
        self.assertEqual(await self.enqueue(20,6),'busy')
    async def test_03_parallel_claims(self):
        for uid in range(1,51):await self.account(uid);await self.enqueue(uid)
        claimed=await asyncio.gather(*(self.repo.claim(100) for _ in range(70)))
        ids=[row['id'] for row in claimed if row]
        self.assertEqual(len(ids),50);self.assertEqual(len(set(ids)),50)
    async def test_04_restart_and_uncertain_send(self):
        for uid in (1,2):await self.account(uid);await self.enqueue(uid)
        preparing=await self.repo.claim(100);sending=await self.repo.claim(100)
        await self.repo.state(sending,'sending')
        await self.repo.pool.execute('UPDATE descarga_jobs SET heartbeat=$1',time.time()-200)
        other=Repository(URL,self.repo.cipher);await other.start()
        try:
            await other.recover()
            rows=await other.pool.fetch('SELECT id,state FROM descarga_jobs')
            states={r['id']:r['state'] for r in rows}
            self.assertEqual(states[preparing['id']],'queued')
            self.assertEqual(states[sending['id']],'review')
            with self.assertRaises(RuntimeError):await self.repo.state(preparing,'sending')
            reclaimed=await other.claim(100)
            self.assertEqual(reclaimed['id'],preparing['id'])
        finally:await other.close()
    async def test_05_queue_limit_and_generation(self):
        for uid in (1,2):await self.account(uid)
        self.assertEqual(await self.repo.enqueue(100,1,1,['link'],'A',limit=1),'queued')
        self.assertEqual(await self.repo.enqueue(100,2,2,['link'],'B',limit=1),'full')
        await self.repo.cancel_user(1)
        self.assertEqual(await self.repo.pool.fetchval("SELECT state FROM descarga_jobs WHERE uid=1"),'cancelled')
        self.assertEqual(await self.repo.enqueue(100,2,2,['link'],'B',limit=1),'queued')
    async def test_06_checkpoint_restores_progress(self):
        await self.account(1);await self.enqueue(1)
        job=await self.repo.claim(100);payload=self.repo.decode(job['payload'])
        payload['seen']=[[-100123,777]]
        await self.repo.checkpoint(job,1,3,payload)
        row=await self.repo.pool.fetchrow('SELECT * FROM descarga_jobs WHERE id=$1',job['id'])
        self.assertEqual((row['position'],row['count'],row['state']),(1,3,'preparing'))
        self.assertEqual(self.repo.decode(row['payload'])['seen'],[[-100123,777]])
    async def test_07_independent_bot_offsets(self):
        await self.repo.set_setting('offset:100',555)
        self.assertIsNone(await self.repo.setting('offset:200'))
        self.assertEqual(await self.repo.setting('offset:100'),'555')
    async def test_archive_queue_independent_and_deduplicated(self):
        await self.repo.enqueue_archive(100,10,'Tester',[101,102])
        await self.repo.enqueue_archive(100,10,'Tester',[101,102])
        self.assertEqual(await self.repo.pool.fetchval('SELECT count(*) FROM descarga_archives'),1)
        job=await self.repo.claim_archive(100)
        self.assertEqual(self.repo.decode(job['payload'])['ids'],[101,102])
        await self.repo.archive_state(job,'sending')
        await self.repo.pool.execute('UPDATE descarga_archives SET heartbeat=$1',time.time()-200)
        await self.repo.recover()
        self.assertEqual(await self.repo.pool.fetchval('SELECT state FROM descarga_archives'),'review')

    async def test_busy_account_lock_does_not_block_frontend(self):
        async with self.repo.account_lock(10):
            with self.assertRaises(ValueError):
                async with self.repo.account_lock(10,wait=False):pass
    async def test_shared_send_pacing(self):
        started=time.monotonic()
        await asyncio.gather(*(self.repo.pace('test-rate',0.03) for _ in range(3)))
        self.assertGreaterEqual(time.monotonic()-started,0.05)

    async def test_08_load_5000_users_100000_links(self):
        count=5000
        session=self.repo.cipher.encrypt(b'session')
        await self.repo.pool.executemany('INSERT INTO descarga_accounts(uid,session) VALUES($1,$2)',[(uid,session) for uid in range(1,count+1)])
        links=['https://t.me/c/123/'+str(mid) for mid in range(1,21)]
        semaphore=asyncio.Semaphore(100)
        async def submit(uid):
            async with semaphore:return await self.repo.enqueue(100,uid,uid,links,'Test')
        started=time.monotonic();results=await asyncio.gather(*(submit(uid) for uid in range(1,count+1)))
        elapsed=time.monotonic()-started
        self.assertEqual(results.count('queued'),count)
        self.assertEqual(await self.repo.pool.fetchval('SELECT count(*) FROM descarga_jobs'),count)
        print(f'QUEUE LOAD TEST: {count} orders / {20*count} links accepted in {elapsed:.2f}s; NO Telegram downloads performed')
        self.assertLess(elapsed,180)
