"""Only local Postgres is used; never contact Telegram or a production database."""
import asyncio
import os
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from urllib.parse import urlparse
from cryptography.fernet import Fernet
from aiohttp import web

from descargas_app.pg_store import Repository
from descargas_app import panel_background_search as search

URL=os.environ.get('TEST_DATABASE_URL','')


class SearchMatchingTests(unittest.TestCase):
    def test_modes_and_exact_entity_ids(self):
        channel={'type':'channel','id':'-10044221','name':'Música México','username':'musica','bot':False}
        group={'type':'group','id':'-10088991','name':'Grupo México','bot':False}
        bot={'type':'private','id':'42','name':'Soporte Bot','username':'soportebot','bot':True}
        self.assertTrue(search.entity_matches(channel,'channels','musica'))
        self.assertTrue(search.entity_matches(channel,'channel_id','44221'))
        self.assertFalse(search.entity_matches(channel,'channel_id','4422'))
        self.assertFalse(search.entity_matches(channel,'groups',''))
        self.assertTrue(search.entity_matches(group,'groups','mexico'))
        self.assertTrue(search.entity_matches(group,'group_id','88991'))
        self.assertTrue(search.entity_matches(bot,'bots','@soportebot'))
        self.assertFalse(search.entity_matches({'type':'private','bot':False},'bots',''))
        self.assertFalse(search.entity_matches(channel,'group_id','44221'))

    def test_metadata_lookup_is_read_only_and_normalized(self):
        account={'id':'123456','name':'José García','username':'Jose_22',
                 'phone':'+1 (305) 555-8922','country':'España','country_code':'ES'}
        self.assertTrue(search.matches(account,'name','jose garcia'))
        self.assertTrue(search.matches(account,'id','345'))
        self.assertTrue(search.matches(account,'username','@JOSE_22'))
        self.assertTrue(search.matches(account,'phone','3055558922'))
        self.assertTrue(search.matches(account,'country','espana'))
        self.assertFalse(search.matches(account,'phone','abc'))
        self.assertFalse(search.matches(account,'country','cuba'))


@unittest.skipUnless(URL,'Local isolated PostgreSQL test database required')
class BackgroundSearchDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed=urlparse(URL)
        if parsed.hostname not in ('localhost','127.0.0.1') or parsed.path!='/descargas_test':
            raise RuntimeError('Search tests refuse production databases')
        self.repo=Repository(URL,Fernet(Fernet.generate_key()))
        await self.repo.start()
        await self.repo.pool.execute('TRUNCATE descarga_panel_search_jobs CASCADE')
        await self.repo.save_account(22,'dummy-telethon-session',{'name':'Prueba'})
        self.account={'id':'22','name':'Activa Test','username':'user22','phone':'+1 305 555 8888',
                      'country':'Estados Unidos','country_code':'US','generation':0,
                      'status':{'state':'active'}}
        self.p=N(s=N(repo=self.repo,bot_id=7,ready=True),
                 accounts=AsyncMock(return_value={'items':[self.account],'next':None}),
                 chats=AsyncMock(),features=N(overview_search=AsyncMock()),
                 unpack=lambda cursor,purpose: {'cursor':cursor,'purpose':purpose})
        self.job=search.BackgroundPanelSearch(self.p)
        self.allowed=patch.object(search,'authorized',lambda actor:actor==111)
        self.perm=patch.object(search,'require_account',new_callable=AsyncMock)
        self.source=patch.object(search,'require_source',new_callable=AsyncMock)
        for p in (self.allowed,self.perm,self.source):
            p.start();self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.repo.pool.execute('TRUNCATE descarga_panel_search_jobs CASCADE')
        await self.repo.close()

    async def test_job_continues_without_an_open_browser_and_results_are_encrypted(self):
        self.p.chats.side_effect=[
            {'items':[{'id':'-10022','type':'group','name':'Cuba Test','username':'cuba','handle':'opaque'}],
             'next':'page-two','generation':0},
            {'items':[{'id':'-10023','type':'group','name':'Cuba Fotos','username':'cuba2','handle':'opaque2'}],
             'next':None,'generation':0}
        ]
        started=await self.job.begin(111,'0','groups','cuba')
        self.assertEqual(started['job']['total'],1)
        await self.job.start(None)
        try:
            for _ in range(80):
                state=(await self.job.status(111,'0'))['job']
                if state['state']=='complete':break
                await asyncio.sleep(.1)
            self.assertEqual(state['state'],'complete')
            self.assertEqual(state['completed'],1)
            self.assertEqual(state['found'],2)
        finally:await self.job.stop(None)
        rows=await self.repo.pool.fetch('SELECT payload FROM descarga_panel_search_hits')
        self.assertEqual(len(rows),2)
        for row in rows:self.assertNotIn(b'Cuba',bytes(row['payload']))
        # A newly created panel object must recover both progress and results.
        restored=search.BackgroundPanelSearch(self.p)
        snapshot=await restored.status(111,'0')
        self.assertEqual(snapshot['job']['id'],started['job']['id'])
        result=await restored.results(111,'0',started['job']['id'])
        self.assertEqual([r['hit']['chat']['name'] for r in result['items']],['Cuba Test','Cuba Fotos'])
        self.assertIsNone(result['next'])

    async def test_pause_does_not_revoke_session_and_resume_uses_same_job(self):
        self.p.chats.return_value={'items':[],'next':None,'generation':0}
        start=(await self.job.begin(111,'0','channels',''))['job']
        paused=(await self.job.control(111,'0',start['id'],'pause'))['job']
        self.assertEqual(paused['state'],'paused')
        self.assertIsNone(await self.job.claim())
        self.assertIsNotNone((await self.repo.account(22))['session'])
        resumed=(await self.job.control(111,'0',start['id'],'resume'))['job']
        self.assertEqual(resumed['state'],'queued')
        claimed=await self.job.claim()
        await self.job.run_claimed(claimed)
        done=(await self.job.status(111,'0'))['job']
        self.assertEqual(done['state'],'complete')
        self.assertEqual(done['completed'],1)

    async def test_pause_inflight_leaves_an_atomic_checkpoint_for_resume(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def read_page(*args):
            entered.set()
            await release.wait()
            return {'items':[{'id':'-1004','type':'group','name':'Cuba','handle':'x'}],
                    'next':None,'generation':0}
        self.p.chats.side_effect=read_page
        begin=(await self.job.begin(111,'0','groups','cuba'))['job']
        claimed=await self.job.claim()
        running=asyncio.create_task(self.job.run_claimed(claimed))
        await asyncio.wait_for(entered.wait(),2)
        await self.job.control(111,'0',begin['id'],'pause')
        release.set()
        await asyncio.wait_for(running,3)
        snapshot=(await self.job.status(111,'0'))['job']
        self.assertEqual(snapshot['state'],'paused')
        self.assertEqual(snapshot['completed'],0)
        self.assertEqual(snapshot['found'],0)
        await self.job.control(111,'0',begin['id'],'resume')
        claimed=await self.job.claim();await self.job.run_claimed(claimed)
        snapshot=(await self.job.status(111,'0'))['job']
        self.assertEqual(snapshot['state'],'complete')
        self.assertEqual(snapshot['found'],1)

    async def test_expired_lease_is_recovered_by_a_new_web_instance(self):
        self.p.chats.return_value={'items':[],'next':None,'generation':0}
        begin=(await self.job.begin(111,'0','groups',''))['job']
        original=await self.job.claim()
        self.assertEqual(original['owner'],self.job.owner)
        await self.repo.pool.execute('UPDATE descarga_panel_search_jobs SET lease_until=0 WHERE id=$1',begin['id'])
        next_process=search.BackgroundPanelSearch(self.p)
        claimed=await next_process.claim()
        self.assertEqual(claimed['owner'],next_process.owner)
        await next_process.run_claimed(claimed)
        done=(await next_process.status(111,'0'))['job']
        self.assertEqual(done['state'],'complete')
        self.assertEqual(done['completed'],1)
        self.assertEqual(self.p.chats.await_count,1)

    async def test_search_is_scoped_to_owner_and_current_panel_version(self):
        started=(await self.job.begin(111,'0','name','Activa'))['job']
        with self.assertRaises(web.HTTPForbidden):
            await self.job.status(222,'0',started['id'])
        with self.assertRaises(web.HTTPForbidden):
            await self.job.status(111,'stale-panel-version',started['id'])
        self.assertEqual((await self.job.status(111,'0',started['id']))['job']['state'],'queued')
