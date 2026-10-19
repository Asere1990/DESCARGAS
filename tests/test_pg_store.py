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
        await self.repo.pool.execute('TRUNCATE descarga_jobs,descarga_archives,descarga_accounts,descarga_settings,descarga_rate')
    async def asyncTearDown(self):await self.repo.close()
    async def account(self,uid):await self.repo.save_account(uid,'authorized-session',{'phone':'+14155552671','phone_country':'US'})
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
        await self.repo.state(sending['id'],'sending')
        await self.repo.pool.execute('UPDATE descarga_jobs SET heartbeat=$1',time.time()-200)
        other=Repository(URL,self.repo.cipher);await other.start()
        try:
            await other.recover()
            rows=await other.pool.fetch('SELECT id,state FROM descarga_jobs')
            states={r['id']:r['state'] for r in rows}
            self.assertEqual(states[preparing['id']],'queued')
            self.assertEqual(states[sending['id']],'review')
            with self.assertRaises(RuntimeError):await self.repo.state(preparing['id'],'sending')
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
        await self.repo.checkpoint(job['id'],1,3,payload)
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
        await self.repo.archive_state(job['id'],'sending')
        await self.repo.pool.execute('UPDATE descarga_archives SET heartbeat=$1',time.time()-200)
        await self.repo.recover()
        self.assertEqual(await self.repo.pool.fetchval('SELECT state FROM descarga_archives'),'review')

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
