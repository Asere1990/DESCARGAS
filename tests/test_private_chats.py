import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from telethon import types,errors
from descargas_app import private_chats as p,manual_board as b,manual_gallery as gallery,group_download as exports
import test_manual_board as fixtures
import test_group_download as export_fixtures


class Client:
    def __init__(self,users,counts):self.users=users;self.counts=counts;self.calls=[];self.enumerations=0
    async def iter_dialogs(self):
        self.enumerations+=1
        for user in self.users:yield NS(entity=user)
    async def __call__(self,request):
        self.calls.append(request)
        value=self.counts.get(request.peer.user_id,0)
        if isinstance(value,Exception):raise value
        return [NS(count=value)]


class PrivateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.BoardTests.asyncSetUp(self)
        users=[types.User(id=i,access_hash=9000+i,first_name=f'Persona {i}') for i in range(40,50)]
        self.client=Client(users,{40:230,41:0,42:1500,43:1,48:8,49:9})
        @asynccontextmanager
        async def connected(*args,**kw):yield self.client
        self.patches=[patch.object(p,'authorized',return_value=True),patch.object(p,'connected',connected),
            patch('descargas_app.group_download.users_busy',AsyncMock(return_value=False))]
        for patcher in self.patches:patcher.start();self.addCleanup(patcher.stop)
        self.s.repo.pace=AsyncMock()

    async def test_list_only_private_media_chats_counts_and_resumes_next_batch(self):
        self.client.users.extend([types.User(id=12,first_name='Self'),types.User(id=7,first_name='This bot'),types.Channel(id=100,title='Group',photo=types.ChatPhotoEmpty(),date=None)])
        await p.listing(self.s,99,12)
        raw=await self.s.repo.setting(b.context_key(self.s,99));state=json.loads(raw)
        self.assertEqual([v['id'] for v in state['entries']],[40,42,43])
        self.assertEqual(len(self.client.calls),8);self.assertEqual(self.client.enumerations,1)
        text=self.s.api.call_args.args[1]['text']
        self.assertIn('1. Persona 40 (40) (230)',text)
        self.assertIn('2. Persona 42 (42) (1500)',text)
        self.assertNotIn('9000',text)
        self.assertIsInstance(self.client.calls[0].filters[0],types.InputMessagesFilterPhotoVideo)
        self.assertTrue(any(v['callback_data']=='manual:p:12' for line in self.s.api.call_args.args[1]['reply_markup']['inline_keyboard'] for v in line))
        await p.listing(self.s,99,12)
        self.assertEqual(len(self.client.calls),10);self.assertEqual(self.client.enumerations,1)
        new=json.loads(await self.s.repo.setting(b.context_key(self.s,99)))
        self.assertEqual([v['id'] for v in new['entries']],[40,42,43,48,49])

    async def test_positive_private_number_opens_peer_not_connected_account(self):
        await p.listing(self.s,99,12)
        with patch.object(b,'open_board',AsyncMock()) as opened:
            self.assertTrue(await b.select_number(self.s,99,'2'))
            opened.assert_awaited_once_with(self.s,99,12,42)

    async def test_peer_hash_bound_to_account_generation_and_verified_peer(self):
        await p.listing(self.s,99,12)
        client=NS(get_entity=AsyncMock(return_value=types.User(id=42,first_name='Pepe')))
        result=await gallery.entity_for(self.s,client,12,42,4)
        self.assertEqual(result.id,42)
        self.assertEqual(client.get_entity.call_args.args[0].access_hash,9042)
        for uid,source,generation in ((13,42,4),(12,42,5),(12,41,4)):
            with self.assertRaises(ValueError):await p.resolve(self.s,client,uid,source,generation)
        client.get_entity.return_value=types.User(id=999)
        with self.assertRaisesRegex(ValueError,'verificar'):await p.resolve(self.s,client,12,42,4)

    async def test_flood_wait_keeps_counter_cursor_and_previous_results(self):
        self.client.counts[42]=errors.FloodWaitError(request=None,capture=3)
        await p.listing(self.s,99,12)
        state=self.s.repo.decode(bytes.fromhex(await self.s.repo.setting(p.scan_key(self.s,99,12))))
        self.assertEqual(state['position'],2);self.assertEqual(state['entries'][0]['id'],40)
        self.assertIn('esperar 3 segundos',self.s.api.call_args.args[1]['text'])
        self.assertTrue(await self.s.repo.setting('link-list-wait:7:12'))

    async def test_navigation_cancels_late_private_list_publication(self):
        original=self.client.__class__.__call__
        async def counter(client,request):
            result=await original(client,request)
            await self.s.repo.set_setting(b.context_key(self.s,99),'')
            return result
        with patch.object(Client,'__call__',counter):await p.listing(self.s,99,12)
        self.s.api.assert_not_awaited()
        self.assertFalse(await self.s.repo.setting(b.context_key(self.s,99)))

    async def test_only_admins_and_user_downloads_have_priority(self):
        with patch.object(p,'authorized',return_value=False):await p.listing(self.s,99,12)
        self.assertFalse(self.client.calls);self.s.api.assert_not_awaited()
        with patch('descargas_app.group_download.users_busy',AsyncMock(return_value=True)):
            with self.assertRaisesRegex(ValueError,'descarga al DM'):await p.listing(self.s,99,12)
        self.assertFalse(self.client.calls)

    async def test_private_menu_keeps_download_buttons_without_group_management(self):
        row=await self.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE token=$1','token')
        data=self.s.repo.decode(row['payload'])
        _,keyboard=b.presentation(dict(row,source=42),data)
        actions=[v['callback_data'].split(':')[1] for line in keyboard for v in line]
        for action in ('d','t','fp','fv','fa','np','nv','na','q','s','as'):self.assertIn(action,actions)
        for action in ('ga','gr','gu','gl','gi'):self.assertNotIn(action,actions)
        with patch.object(b,'authorized',return_value=True):await b.menu(self.s,99,12)
        buttons=self.s.api.call_args.args[1]['reply_markup']['inline_keyboard']
        self.assertTrue(any(v['callback_data']=='manual:p:12' for line in buttons for v in line))

    async def test_private_export_reads_selected_album_directly_and_preserves_caption(self):
        s=export_fixtures.GroupExportTests().service()
        peer=types.User(id=42,access_hash=900,first_name='Pepe')
        items=[NS(id=10,photo=NS(id=11),document=None,media=None,message='Caption privado',grouped_id=99,file=NS(size=2))]
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),get_me=AsyncMock(return_value=NS(id=12,first_name='Admin',last_name='')),get_messages=AsyncMock(return_value=items[0]))
        s.client=lambda session:client
        job=dict(bot_id=7,uid=12,source=42,actor=99,generation=4,manual_token='token',phase='retry',retry_message=10,state='running',sending=False)
        progress=NS(start=lambda:progress,close=AsyncMock())
        with patch.object(exports,'checked',AsyncMock(return_value=True)),patch('descargas_app.manual_gallery.entity_for',AsyncMock(return_value=peer)),patch('descargas_app.service.album',AsyncMock(return_value=items)),patch('descargas_app.manual_types.select_items',AsyncMock(return_value=((42,99),items))),patch('descargas_app.export_inventory.stored',AsyncMock(return_value=False)),patch('descargas_app.manual_progress.ManualProgress',return_value=progress),patch.object(exports,'archive_items',AsyncMock(return_value=1)) as archive,patch('descargas_app.export_inventory.resolve_pending',AsyncMock()),patch.object(b,'complete',AsyncMock()) as complete,patch.object(exports,'latest_confirmation',AsyncMock()):
            await exports.step(s,job)
        s.source_items.assert_not_awaited()
        client.get_messages.assert_awaited_once_with(peer,ids=10)
        self.assertEqual(archive.call_args.args[4][0].message,'Caption privado')
        complete.assert_awaited_once_with(s,job,items,1)
        client.disconnect.assert_awaited_once()

    async def test_invalid_peer_does_not_block_other_private_chats(self):
        self.client.counts[40]=errors.PeerIdInvalidError(request=None)
        await p.listing(self.s,99,12)
        state=self.s.repo.decode(bytes.fromhex(await self.s.repo.setting(p.scan_key(self.s,99,12))))
        self.assertEqual(state['position'],8);self.assertEqual(state['unavailable'],1)
        self.assertEqual(state['entries'][0]['id'],42)
        self.assertIn('no permitió consultar: 1',self.s.api.call_args.args[1]['text'])
