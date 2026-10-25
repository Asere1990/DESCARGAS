import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,Mock,patch
from telethon import types
from descargas_app import panel_delivery as d

class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setup_service(self):
        repo=N(encode=lambda value:json.dumps(value).encode(),decode=lambda value:json.loads(value),
               set_setting=AsyncMock(),media_cache=AsyncMock(return_value={'state':'ready','payload':json.dumps({'ids':[101,102,103]})}))
        s=N(repo=repo,bot_id=7,almacen=-100123,tell=AsyncMock(),api=AsyncMock(return_value=[{'message_id':1},{'message_id':2}]))
        messages=[N(id=i,grouped_id=999,peer_id=types.PeerChannel(55),photo=N(id=1000+i),document=None,file=N(size=20),reply_to_msg_id=None,replies=None) for i in [1,2,3]]
        client=N(get_me=AsyncMock(return_value=N(first_name='Cuenta',last_name='Prueba')),get_messages=AsyncMock(return_value=messages[1:]))
        @asynccontextmanager
        async def account(*args,**kwargs):yield client,4
        panel=N(account=account,target=AsyncMock(return_value=N()))
        job={'key':'panel-delivery:7:test','uid':22,'actor':88,'source':-1000000000055,'generation':4,'handle':'scoped-peer','ids':[2,3],'state':'running','panel_delivery':True,'bot_id':7}
        return s,panel,client,messages,job
    async def test_reviewer_archives_full_album_once_and_copies_only_selected_to_own_dm(self):
        s,panel,client,messages,job=self.setup_service()
        archive=AsyncMock(return_value=3)
        with patch('descargas_app.web_panel.TelegramPanel',return_value=panel),patch.object(d,'check',AsyncMock(return_value=True)),patch.object(d,'require_source',AsyncMock()),patch.object(d,'reviewer',return_value=True),patch('descargas_app.service.album',AsyncMock(return_value=messages)),patch('descargas_app.group_download.archive_items',archive):
            await d.process(s,job)
        archive.assert_awaited_once();self.assertEqual(archive.call_args.args[4],messages)
        copies=[args.args[1] for args in s.api.await_args_list if args.args[0]=='copyMessages']
        self.assertEqual(len(copies),1);self.assertEqual(copies[0]['chat_id'],88)
        self.assertEqual(copies[0]['message_ids'],[102,103]);self.assertTrue(copies[0]['remove_caption'])
        self.assertIn('https://t.me/c/123/101',s.tell.call_args.args[1])
    async def test_admin_duplicate_only_reports_existing_archive_without_dm_copy(self):
        s,panel,client,messages,job=self.setup_service()
        with patch('descargas_app.web_panel.TelegramPanel',return_value=panel),patch.object(d,'check',AsyncMock(return_value=True)),patch.object(d,'require_source',AsyncMock()),patch.object(d,'reviewer',return_value=False),patch('descargas_app.service.album',AsyncMock(return_value=messages)),patch('descargas_app.group_download.archive_items',AsyncMock(return_value=0)):
            await d.process(s,job)
        self.assertFalse(any(a.args[0]=='copyMessages' for a in s.api.await_args_list))
        self.assertIn('Ya se descargó',s.tell.call_args.args[1])
        self.assertIn('https://t.me/c/123/101',s.tell.call_args.args[1])
    async def test_uncertain_archive_is_never_copied_to_dm(self):
        s,panel,client,messages,job=self.setup_service();s.repo.media_cache.return_value={'state':'publishing'}
        with patch('descargas_app.web_panel.TelegramPanel',return_value=panel),patch.object(d,'check',AsyncMock(return_value=True)),patch.object(d,'require_source',AsyncMock()),patch.object(d,'reviewer',return_value=True),patch('descargas_app.service.album',AsyncMock(return_value=messages)),patch('descargas_app.group_download.archive_items',AsyncMock(return_value=0)):
            with self.assertRaisesRegex(ValueError,'confirmada'):await d.process(s,job)
        s.api.assert_not_awaited()
    async def test_enqueue_enforces_reviewer_limit_before_opening_account(self):
        s,panel,client,messages,job=self.setup_service();panel.s=s
        with patch.object(d,'reviewer',return_value=True):
            with self.assertRaises(ValueError):await d.enqueue(panel,88,{'account':22,'messages':list(range(1,12))})
        s.repo.set_setting.assert_not_awaited()
    async def test_completed_jobs_leave_pending_namespace(self):
        s,panel,client,messages,job=self.setup_service();s.repo.pool=N(execute=AsyncMock())
        await d.finish(s,job)
        self.assertEqual(s.repo.set_setting.call_args.args[0],'panel-delivery-history:7:test')
        self.assertIn('DELETE',s.repo.pool.execute.call_args.args[0])
