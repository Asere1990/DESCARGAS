import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,Mock,patch
from telethon import errors,types
from tests import test_web_panel as fixtures
from descargas_app import panel_accounts as overview,creator_access as policy,moderation,protected

class OverviewTests(fixtures.PanelTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for name,value in [('authorized',lambda actor:actor in self.allowed),('require_account',self.account_guard)]:
            p=patch.object(overview,name,value);p.start();self.addCleanup(p.stop)
        @asynccontextmanager
        async def lock(uid,wait=False):
            self.assertEqual(uid,22);self.assertFalse(wait)
            yield
        self.repo.account_lock=lock
        self.s.get_session=AsyncMock(return_value='same-saved-session')
        self.client.connect=AsyncMock();self.client.disconnect=AsyncMock()
        self.s.client=Mock(return_value=self.client)
        p=patch.object(overview,'identity',self.client.get_me);p.start();self.addCleanup(p.stop)
        self.repo.erase_session=AsyncMock();self.repo.save_account=AsyncMock()
    async def test_status_checks_saved_identity_and_caches_without_modifying_sessions(self):
        result=await self.panel.overview.check(111,22)
        self.assertEqual(result['state'],'active')
        self.s.client.assert_called_once_with('same-saved-session')
        self.client.disconnect.assert_awaited_once()
        self.assertEqual(await self.panel.overview.check(111,22),result)
        self.assertEqual(self.client.get_me.await_count,1)
        self.repo.erase_session.assert_not_called();self.repo.save_account.assert_not_called()
        self.assertNotIn('same-saved-session',json.dumps(result))
    async def test_only_explicit_revocation_gets_revoked_label(self):
        for response,state in [(None,'invalid'),(errors.SessionRevokedError(None),'revoked'),(errors.SessionExpiredError(None),'expired'),(errors.AuthKeyUnregisteredError(None),'invalid'),(errors.FloodWaitError(None,30),'unknown'),(TimeoutError(),'unknown')]:
            with self.subTest(state=state):
                self.settings.clear();self.client.get_me.reset_mock()
                self.client.get_me.side_effect=response if isinstance(response,Exception) else None
                self.client.get_me.return_value=response if not isinstance(response,Exception) else None
                result=await self.panel.overview.check(111,22)
                self.assertEqual(result['state'],state)
                self.assertEqual(bool(self.settings),state!='unknown')
        self.repo.erase_session.assert_not_called();self.repo.save_account.assert_not_called()
    async def test_status_never_opens_second_connection_when_live_client_can_be_borrowed(self):
        @asynccontextmanager
        async def borrow(actor,uid):yield self.client,3
        self.panel.live.borrow=borrow
        result=await self.panel.overview.check(111,22)
        self.assertEqual(result['state'],'active');self.s.client.assert_not_called();self.client.disconnect.assert_not_awaited()
    async def test_status_ignores_evidence_from_session_replaced_during_probe(self):
        self.repo.account.side_effect=[{'session':b'a','generation':3},{'session':b'b','generation':3}]
        result=await self.panel.overview.check(111,22)
        self.assertEqual(result['state'],'unknown');self.assertFalse(self.settings)
    async def test_reviewer_list_filters_nonactive_accounts_before_returning_identity(self):
        self.pool.fetch.return_value=[{'uid':uid,'profile':json.dumps({'name':'Private '+str(uid)}),'generation':3,'session_tag':'tag'} for uid in [22,23,24]]
        self.panel.overview.cached=AsyncMock(side_effect=[{'state':'active','checked':1},{'state':'revoked','checked':1},{'state':'invalid','checked':1}])
        with patch('descargas_app.web_panel.reviewer',return_value=True):
            data=await self.panel.accounts(111)
        self.assertEqual([a['id'] for a in data['items']],['22'])
        self.assertTrue(data['reviewer']);self.assertNotIn('Private 23',json.dumps(data));self.assertNotIn('total',data)

    async def test_overview_metadata_does_not_open_account_connections(self):
        self.pool.fetch.return_value=[{'uid':22,'profile':json.dumps({'name':'Prueba','phone':'5351234567','username':'demo'}),'generation':3,'session_tag':'tag','country':'CU'}]
        data=await self.panel.accounts(111);item=data['items'][0]
        self.assertEqual(item['id'],'22');self.assertEqual(item['username'],'demo');self.assertEqual(item['country'],'Cuba');self.assertTrue(item['phone'].startswith('+53'))
        self.assertEqual(item['status']['state'],'unknown');self.s.client.assert_not_called()
    async def test_cross_account_search_cursors_and_click_targets_remain_scoped(self):
        hit={'id':'55','name':'Grupo Miami','type':'group','handle':self.peer}
        self.panel.chats=AsyncMock(return_value={'items':[hit,{'name':'Otro'}],'next':None})
        self.panel.features.search=AsyncMock(return_value={'items':[{'chat':hit,'message':{'id':42,'text':'Miami'}}],'next':None})
        first=await self.panel.features.overview_search(111,22,'Miami')
        self.assertEqual(first['items'],[{'chat':hit,'message':None}]);self.assertIsNotNone(first['next'])
        second=await self.panel.features.overview_search(111,22,'Miami',first['next'])
        self.assertEqual(second['items'][0]['message']['id'],42);self.assertIsNone(second['next'])
        for actor,uid,query in [(112,22,'Miami'),(111,33,'Miami'),(111,22,'Habana')]:
            with self.assertRaises(ValueError):await self.panel.features.overview_search(actor,uid,query,first['next'])
    async def test_saved_chat_is_exact_selected_account_and_roles_are_from_entities(self):
        data=await self.panel.saved_chat(111,22)
        self.assertTrue(data['saved']);self.assertEqual(data['id'],'22')
        self.assertEqual(self.panel.unpack(data['handle'],'peer')['kind'],'self')
        own=types.Channel(id=200,title='Propio',creator=True,photo=types.ChatPhotoEmpty(),date=None,access_hash=9)
        admin=types.Channel(id=201,title='Admin',admin_rights=types.ChatAdminRights(delete_messages=True),photo=types.ChatPhotoEmpty(),date=None,access_hash=10)
        from datetime import datetime,timezone
        self.rows=[N(entity=e,id=-1000000000000-e.id,message=None,date=datetime.now(timezone.utc),unread_count=0,pinned=False) for e in [own,admin]]
        data=await self.panel.chats(111,22)
        self.assertTrue(data['items'][0]['owner']);self.assertFalse(data['items'][0]['admin'])
        self.assertTrue(data['items'][1]['admin']);self.assertFalse(data['items'][1]['owner'])

class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_query_keeps_revocation_error_instead_of_suppressing_it(self):
        client=AsyncMock(side_effect=errors.SessionRevokedError(None))
        with self.assertRaises(errors.SessionRevokedError):await overview.identity(client)
        from telethon import functions
        self.assertIsInstance(client.call_args.args[0],functions.users.GetUsersRequest)

class ReviewerPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name)
        for name,value in [('admins.txt','99\n'),('permisos.txt','88\n'),('cuentas.txt','12\n'),('protegidos','-100123\n'),('bots_protegidos','@Hidden_bot\n456\n')]:
            (root/name).write_text(value)
        for target,value in [('descargas_app.creator_access.ROOT',root),('descargas_app.moderation.ADMIN_FILE',root/'admins.txt'),('descargas_app.protected.FILE',root/'protegidos'),('descargas_app.protected.BOTS_FILE',root/'bots_protegidos')]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        self.s=N(bot_id=7,repo=N(creator_registration=AsyncMock(return_value=None),creator_ids=AsyncMock(return_value=[]),pool=N(fetchval=AsyncMock(return_value=1))))
    async def test_roles_do_not_depend_on_removed_police_file(self):
        self.assertTrue(moderation.authorized(99));self.assertTrue(moderation.authorized(88));self.assertFalse(moderation.authorized(77))
        self.assertTrue(policy.police(99));self.assertFalse(policy.police(88))
    async def test_reviewers_cannot_open_admin_accounts_or_explicitly_hidden_accounts(self):
        for uid in (12,99):
            with self.assertRaises(ValueError):await policy.require_account(self.s,uid,88)
            await policy.require_account(self.s,uid,99)
        await policy.require_account(self.s,34,88)
    async def test_unlisted_group_allowed_without_owner_queries(self):
        self.s.repo.pool.fetchval.return_value=1
        client=N(get_participants=AsyncMock(side_effect=RuntimeError()))
        await policy.require_source(self.s,34,-100999,client=client,entity=N(creator=False),actor=88)
        self.s.repo.pool.fetchval.assert_not_awaited();client.get_participants.assert_not_awaited()
    async def test_source_summary_only_hides_explicit_protected_groups(self):
        self.s.repo.pool.fetch=AsyncMock()
        rows=await policy.visible_sources(self.s,88,[{'source':-100123},{'source':-100999},{'source':-100888},{'source':99},{'source':12}])
        self.assertEqual(rows,[{'source':-100999},{'source':-100888}]);self.s.repo.pool.fetch.assert_not_awaited()

    async def test_hidden_sources_and_bots_are_blocked_even_for_direct_handles(self):
        for source,entity in [(-100123,None),(456,None),(789,N(bot=True,username='Hidden_bot'))]:
            with self.assertRaises(ValueError):await policy.require_source(self.s,34,source,entity=entity,actor=88)
            await policy.require_source(self.s,34,source,entity=entity,actor=99)
        await policy.require_source(self.s,34,123,entity=N(bot=False),actor=88)
