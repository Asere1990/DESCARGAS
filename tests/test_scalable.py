import asyncio
from contextlib import asynccontextmanager
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from cryptography.fernet import Fernet
from descargas_app.scalable import ScaleService,CURRENT_JOB
from descargas_app.media_cache import CacheMiss

class ScalableServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'TOKEN':'123:test','ID':'123','HASH':'a'*32,
            'DESCARGAS_SESSION_KEY':Fernet.generate_key().decode(),'DESCARGAS_DATA_DIR':self.directory.name})
        self.env.start();self.service=ScaleService();self.service.bot_id=123
        self.service.tell=AsyncMock();self.service.repo.is_blocked=AsyncMock(return_value=False);self.service.repo.policy=AsyncMock(return_value={'pay':False,'locked':False})
    async def asyncTearDown(self):
        await self.service.close();self.env.stop();self.directory.cleanup()
    async def test_admin_download_gets_queue_priority(self):
        self.service.repo.enqueue=AsyncMock(return_value='queued')
        await self.service.update({'update_id':900,'message':{'chat':{'type':'private'},'from':{'id':1609522430},'text':'https://t.me/c/123/5'}})
        self.assertEqual(self.service.repo.enqueue.call_args.kwargs['priority'],1)

    async def test_protected_source_rejected_before_enqueue(self):
        from descargas_app.protected import MESSAGE
        self.service.repo.enqueue=AsyncMock()
        with patch('descargas_app.protected.sources',return_value={-100123}):
            await self.service.update({'update_id':901,'message':{'chat':{'type':'private'},'from':{'id':10},'text':'https://t.me/c/123/5'}})
        self.service.repo.enqueue.assert_not_awaited()
        self.service.tell.assert_awaited_once_with(10,MESSAGE)

    async def test_graceful_shutdown_finishes_short_active_download(self):
        done=asyncio.Event()
        async def active():await asyncio.sleep(0.01);done.set()
        task=asyncio.create_task(active());self.service.download_tasks=[task]
        await self.service.close()
        self.assertTrue(done.is_set());self.assertFalse(task.cancelled())

    async def test_private_request_bound_to_sender(self):
        self.service.repo.enqueue=AsyncMock(return_value='queued')
        await self.service.update({'update_id':555,'message':{'chat':{'type':'private'},'from':{'id':10,'first_name':'Test'},'text':'https://t.me/c/123/5'}})
        args=self.service.repo.enqueue.call_args.args
        self.assertEqual(args[:3],(123,555,10));self.assertEqual(args[3],['https://t.me/c/123/5'])
        self.service.tell.assert_not_awaited()
    async def test_five_link_batch_limit(self):
        self.service.repo.enqueue=AsyncMock(return_value='queued')
        message={'chat':{'type':'private'},'from':{'id':10}}
        await self.service.update({'update_id':555,'message':dict(message,text='\n'.join(f'https://t.me/c/123/{i}' for i in range(6)))})
        self.service.repo.enqueue.assert_not_awaited()
        self.assertIn('máximo de 5 enlaces',self.service.tell.call_args.args[1])
        await self.service.update({'update_id':556,'message':dict(message,text='\n'.join(f'https://t.me/c/123/{i}' for i in range(5)))})
        self.assertEqual(len(self.service.repo.enqueue.call_args.args[3]),5)

    async def test_duplicate_is_not_acknowledged_twice(self):
        self.service.repo.enqueue=AsyncMock(return_value='duplicate')
        await self.service.update({'update_id':555,'message':{'chat':{'type':'private'},'from':{'id':10},'text':'https://t.me/c/123/5'}})
        self.service.tell.assert_not_awaited()
    async def test_revoked_generation_cannot_send(self):
        self.service.runtime_config={'token':'test'}
        encoded=self.service.repo.encode(self.service.runtime_config).decode()
        self.service.repo.setting=AsyncMock(return_value=encoded)
        self.service.repo.account=AsyncMock(return_value={'generation':2,'session':b'session'})
        self.service.repo.state=AsyncMock()
        marker=CURRENT_JOB.set({'id':'job','generation':1})
        try:
            with self.assertRaises(ValueError):await self.service.before_send(10)
            self.service.repo.state.assert_not_awaited()
        finally:CURRENT_JOB.reset(marker)
    async def test_archive_is_queued_without_blocking_downloads(self):
        self.service.repo.enqueue_archive=AsyncMock();self.service.repo.pace=AsyncMock()
        payload=self.service.repo.encode({'name':'Test'})
        marker=CURRENT_JOB.set({'payload':payload})
        try:await self.service.archive(10,[SimpleNamespace(id=100),SimpleNamespace(id=101)])
        finally:CURRENT_JOB.reset(marker)
        self.service.repo.enqueue_archive.assert_awaited_once_with(123,10,'Test',[100,101])
        self.service.repo.pace.assert_not_awaited()
    async def test_two_workers_process_different_users_concurrently(self):
        service=self.service;started=set();both=asyncio.Event();release=asyncio.Event()
        payload=service.repo.encode({'name':'Test','links':['https://t.me/c/123/1'],'seen':[]})
        jobs=[{'id':str(uid),'uid':uid,'generation':0,'payload':payload,'position':0,'count':0,'created':0} for uid in (10,20)]
        async def claim(bot_id):return jobs.pop(0) if jobs else None
        @asynccontextmanager
        async def lock(uid):yield
        service.repo.claim=claim;service.repo.account_lock=lock
        service.repo.account=AsyncMock(return_value={'generation':0,'session':service.cipher.encrypt(b'session')})
        service.repo.state=AsyncMock();service.repo.checkpoint=AsyncMock()
        service.client=lambda session:SimpleNamespace(connect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),disconnect=AsyncMock())
        async def deliver(uid,client,link,seen):
            started.add(uid)
            if len(started)==2:both.set()
            await release.wait();return 1
        service.deliver=deliver
        tasks=[asyncio.create_task(service.work()) for _ in range(2)]
        try:
            await asyncio.wait_for(both.wait(),timeout=2)
            self.assertEqual(started,{10,20})
        finally:
            release.set();service.stopping=True
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)

    async def run_lane_case(self,lane,miss=False):
        s=self.service
        payload=s.repo.encode({'name':'Test','links':['https://t.me/c/123/1','https://t.me/c/123/2'],'seen':[]})
        job={'id':'j','uid':10,'generation':0,'payload':payload,'position':0,'count':0,'created':0}
        s.repo.claim=AsyncMock(return_value=job)
        @asynccontextmanager
        async def lock(uid):yield
        s.repo.account_lock=lock
        s.repo.account=AsyncMock(return_value={'generation':0,'session':s.cipher.encrypt(b'session')})
        s.client=lambda session:SimpleNamespace(connect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),disconnect=AsyncMock())
        async def routed(*args):s.stopping=True
        s.repo.reroute=AsyncMock(side_effect=routed)
        s.repo.checkpoint=AsyncMock();s.repo.state=AsyncMock()
        s.deliver=AsyncMock(side_effect=CacheMiss() if miss else None,return_value=1)
        await s.work(lane)
        return job

    async def test_fast_miss_routes_to_slow_without_failed_or_completed_status(self):
        job=await self.run_lane_case('fast',miss=True)
        self.service.repo.reroute.assert_awaited_once_with(job,'slow')
        self.service.repo.checkpoint.assert_not_awaited();self.service.repo.state.assert_not_awaited()

    async def test_mixed_batch_returns_to_fast_after_one_new_download(self):
        job=await self.run_lane_case('slow')
        self.service.repo.reroute.assert_awaited_once_with(job,'fast')
        self.assertEqual(self.service.repo.checkpoint.call_args.args[1:3],(1,1))
        self.service.deliver.assert_awaited_once();self.service.repo.state.assert_not_awaited()
