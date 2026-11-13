"""Indexed group/channel searches use isolated PostgreSQL and no Telegram network."""
import json
import os
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from descargas_app.pg_store import Repository
from descargas_app import panel_background_search as search
from descargas_app import panel_entity_index as index

URL=os.environ.get('TEST_DATABASE_URL','')


@unittest.skipUnless(URL,'Local test Postgres required')
class EntityIndexTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url=urlparse(URL)
        if url.hostname not in ('localhost','127.0.0.1') or url.path!='/descargas_test':
            raise RuntimeError('Refusing to touch a non-test database')
        self.repo=Repository(URL,Fernet(Fernet.generate_key()))
        await self.repo.start()
        await self.repo.pool.execute(
            'DELETE FROM descarga_panel_entity_index WHERE bot_id=99001')
        await self.repo.pool.execute(
            'DELETE FROM descarga_panel_entity_scans WHERE bot_id=99001')
        await self.repo.pool.execute(
            'DELETE FROM descarga_panel_search_jobs WHERE bot_id=99001')
        await self.repo.save_account(99002,'test-session-99002',{'name':'Cuenta prueba'})
        self.account={'id':'99002','name':'Cuenta activa 99002','generation':0,
                      'username':'', 'country':'','status':{'state':'active'}}

        def pack(purpose,**kwargs):
            value={'purpose':purpose,'bot':99001,**kwargs}
            return self.repo.cipher.encrypt(json.dumps(value).encode()).decode()
        def unpack(token,purpose):
            data=json.loads(self.repo.cipher.decrypt(token.encode()))
            if data.get('purpose')!=purpose or data.get('bot')!=99001:
                raise ValueError('Invalid private handle')
            return data

        self.p=N(
            s=N(repo=self.repo,bot_id=99001,ready=True),
            pack=pack,unpack=unpack,
            accounts=AsyncMock(return_value={'items':[self.account],'next':None}),
            chats=AsyncMock(),features=N(overview_search=AsyncMock()),
        )
        self.job=search.BackgroundPanelSearch(self.p)
        self.guard=AsyncMock()
        for target,attribute,value in (
            (search,'authorized',lambda actor:actor==99003),
            (search,'require_account',self.guard),
            (search,'require_source',self.guard),
            (index,'require_source',self.guard),
        ):
            hook=patch.object(target,attribute,value)
            hook.start();self.addCleanup(hook.stop)

        self.group=self.dialog(-1000000000777,'group','Grupo CUBA Fotos',777)
        self.channel=self.dialog(-1000000000888,'channel','Canal Cubano',888)
        self.private={'id':'4411','type':'private','name':'Chat personal',
                      'handle':pack('peer',actor=99003,uid=99002,generation=0,
                                    kind='user',id=4411,hash=72)}
        self.p.chats.return_value={'items':[self.group,self.channel,self.private],
                                   'next':None,'generation':0}

    def dialog(self,source,category,title,peerid):
        return {'id':str(source),'type':category,'name':title,'username':'',
                'handle':self.p.pack('peer',actor=99003,uid=99002,generation=0,
                                      kind='channel',id=peerid,hash=500+peerid)}

    async def asyncTearDown(self):
        await self.repo.pool.execute(
            'DELETE FROM descarga_panel_search_jobs WHERE bot_id=99001')
        await self.repo.pool.execute(
            'DELETE FROM descarga_panel_entity_index WHERE bot_id=99001')
        await self.repo.pool.execute(
            'DELETE FROM descarga_panel_entity_scans WHERE bot_id=99001')
        await self.repo.close()

    async def seed(self):
        first=(await self.job.begin(99003,'0','groups','cuba'))['job']
        self.assertEqual(first['cached'],0)
        claimed=await self.job.claim()
        await self.job.run_claimed(claimed)
        done=(await self.job.status(99003,'0'))['job']
        self.assertEqual(done['state'],'complete')
        self.assertEqual(done['found'],1)
        self.assertEqual(done['completed'],1)
        self.p.chats.reset_mock()

    async def test_private_chats_are_not_saved_and_names_are_encrypted(self):
        await self.seed()
        rows=await self.repo.pool.fetch(
            'SELECT source,category,payload FROM descarga_panel_entity_index WHERE bot_id=99001')
        self.assertEqual({int(r['source']) for r in rows},
                         {-1000000000777,-1000000000888})
        self.assertEqual({r['category'] for r in rows},{'group','channel'})
        self.assertTrue(all(b'CUBA' not in bytes(r['payload']) for r in rows))
        self.assertEqual(await self.repo.pool.fetchval(
            'SELECT count(*) FROM descarga_panel_entity_scans WHERE bot_id=99001'),1)

    async def test_repeated_name_and_exact_id_queries_use_local_index(self):
        await self.seed()
        for mode,term,expected in (
            ('group_id','-1000000000777',self.group),
            ('groups','fotos',self.group),
            ('channel_id','-1000000000888',self.channel),
            ('channels','cubano',self.channel),
        ):
            started=(await self.job.begin(99003,'0',mode,term))['job']
            self.assertEqual(started['cached'],1)
            self.assertEqual(started['completed'],1)
            self.assertEqual(started['found'],1)
            result=await self.job.results(99003,'0',started['id'])
            self.assertEqual(result['items'][0]['hit']['chat']['id'],expected['id'])
            chat=result['items'][0]['hit']['chat']
            handle=self.p.unpack(chat['handle'],'peer')
            self.assertEqual(handle['actor'],99003)
            self.assertEqual(handle['uid'],99002)
        self.p.chats.assert_not_awaited()

    async def test_index_invalidation_after_session_rotation_and_ttl(self):
        await self.seed()
        await self.repo.save_account(99002,'new-test-session-99002',{'name':'Cuenta nueva'})
        nextjob=(await self.job.begin(99003,'0','group_id','-1000000000777'))['job']
        self.assertEqual(nextjob['cached'],0)
        self.assertEqual(nextjob['completed'],0)
        self.p.chats.assert_not_awaited()
        await self.job.run_claimed(await self.job.claim())
        self.assertEqual(self.p.chats.await_count,1)
        self.p.chats.reset_mock()
        await self.repo.pool.execute(
            'UPDATE descarga_panel_entity_scans SET completed_at=0 WHERE bot_id=99001')
        stale=(await self.job.begin(99003,'0','groups','cuba'))['job']
        self.assertEqual(stale['cached'],0)
        self.assertEqual(stale['completed'],0)

    async def test_exact_group_id_does_not_match_other_group_or_private_chat(self):
        await self.seed()
        miss=(await self.job.begin(99003,'0','group_id','-1000000000888'))['job']
        self.assertEqual(miss['cached'],1)
        self.assertEqual(miss['found'],0)
        self.p.chats.assert_not_awaited()


if __name__=='__main__':
    unittest.main()
