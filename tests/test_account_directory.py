import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from telethon import types
from descargas_app import account_directory as d
from descargas_app.admin_panel import AdminPanel


class RangeTests(unittest.TestCase):
    def test_ranges_are_inclusive_and_capped(self):
        self.assertEqual(d.page_range(''),(1,100))
        self.assertEqual(d.page_range('245-344'),(245,344))
        self.assertEqual(d.page_range('1345',True),(1345,1345))
        for value in ['0-99','1-101','344-245','245','-1-10','1-2; DROP TABLE x']:
            with self.assertRaises(ValueError):d.page_range(value)


class DirectoryTests(unittest.IsolatedAsyncioTestCase):
    def panel(self):
        repo=NS(decode=lambda x:x,pace=AsyncMock())
        service=NS(bot_id=7,almacen=-1009,repo=repo,api=AsyncMock())
        panel=AdminPanel(service);panel.send=AsyncMock();panel.pages=AsyncMock()
        panel.pool=NS(fetch=AsyncMock(),fetchval=AsyncMock(return_value=2000))
        return panel

    async def test_account_range_total_and_safe_details_without_user_login(self):
        p=self.panel()
        p.pool.fetch.return_value=[{'uid':12,'profile':{'phone':'+5355555555','phone_country':'CU'},'name':{'name':'Old'},'country':'CU','number':245,'total':2000}]
        p.s.api.return_value={'first_name':'<Juan>','username':'juan'}
        with patch('descargas_app.moderation.authorized',return_value=True):
            await d.accounts(p,99,'245-344')
        self.assertEqual(p.pool.fetch.call_args.args[1:],(7,245,344))
        title,entries=p.pages.call_args.args[1:]
        self.assertIn('Total: 2000',title);self.assertIn('/cuentas 345-444',title)
        for value in ['245.','tg://user?id=12','&lt;Juan&gt;','@juan','+5355555555','Cuba']:
            self.assertIn(value,entries[0])
        self.assertEqual(p.s.api.call_args.args,('getChat',{'chat_id':12}))

    async def test_account_single_index_and_cached_username(self):
        p=self.panel();p.pool.fetch.return_value=[{'uid':12,'profile':{},'name':None,'country':None,'number':1345,'total':2000}]
        p.s.api.return_value={'first_name':'A'}
        with patch('descargas_app.moderation.authorized',return_value=True):
            await d.accounts(p,99,'1345',single=True)
            await d.accounts(p,99,'1345',single=True)
        self.assertEqual(p.pool.fetch.call_args.args[2:],(1345,1345))
        p.s.api.assert_awaited_once()
        self.assertIn('Sin username',p.pages.call_args.args[2][0])

    async def test_empty_range_reports_total_without_profile_queries(self):
        p=self.panel();p.pool.fetch.return_value=[]
        with patch('descargas_app.moderation.authorized',return_value=True):await d.accounts(p,99,'2101-2200')
        self.assertIn('Total: 2000',p.pages.call_args.args[1]);p.s.api.assert_not_awaited()

    async def test_new_commands_are_admin_and_dm_only(self):
        p=self.panel()
        for command in ['/cuentas','/cuenta 1345','/canales 12 101-200']:
            with patch('descargas_app.admin_panel.authorized',return_value=False):
                self.assertTrue(await p.update({'message':{'text':command,'from':{'id':99},'chat':{'id':99,'type':'private'}}}))
            self.assertFalse(p.tasks)
        with patch('descargas_app.admin_panel.authorized',return_value=True):
            await p.run(99,-1009,'cuentas','')
        p.pool.fetch.assert_not_awaited()

    def destination_service(self):
        saved={}
        async def setting(key):return saved.get(key)
        async def put(key,value):saved[key]=value
        repo=NS(account=AsyncMock(return_value={'session':b'x','generation':4}),
            setting=setting,set_setting=AsyncMock(side_effect=put),pool=NS(fetchval=AsyncMock(return_value=0)),
            encode=lambda value:json.dumps(value).encode(),decode=lambda value:json.loads(value),pace=AsyncMock())
        return NS(repo=repo,bot_id=7,tell=AsyncMock()),saved

    async def test_destinations_paged_snapshot_is_complete_and_reused(self):
        s,saved=self.destination_service();calls=[]
        async def dialogs():
            calls.append(1)
            for i in range(205):
                yield NS(entity=types.Channel(id=i+123,title=f'Destino {i}',photo=types.ChatPhotoEmpty(),date=None,broadcast=i%2==0))
            yield NS(entity=types.Channel(id=999,title='Fuera',photo=types.ChatPhotoEmpty(),date=None,left=True))
        @asynccontextmanager
        async def connection(*args):yield NS(iter_dialogs=dialogs)
        with patch('descargas_app.moderation.authorized',return_value=True),patch('descargas_app.account_chat.connected',connection):
            await d.destinations(s,99,'12')
            text='\n'.join(c.args[1] for c in s.tell.await_args_list)
            self.assertIn('Total: 205',text);self.assertIn('100.',text);self.assertNotIn('101.',text)
            s.tell.reset_mock()
            await d.destinations(s,99,'12 101-200')
        text='\n'.join(c.args[1] for c in s.tell.await_args_list)
        self.assertIn('101.',text);self.assertIn('200.',text);self.assertNotIn('201.',text)
        self.assertEqual(calls,[1])
        data=json.loads(next(iter(saved.values())))
        self.assertEqual(len(data['ids']),205)
        snapshot=s.repo.decode(bytes.fromhex(data['snapshot']))['entries']
        self.assertEqual(data['ids'][100],snapshot[100]['id'])

    async def test_incomplete_destination_scan_never_publishes_a_total(self):
        s,saved=self.destination_service()
        async def dialogs():
            yield NS(entity=types.Channel(id=123,title='A',photo=types.ChatPhotoEmpty(),date=None))
            raise TimeoutError()
        @asynccontextmanager
        async def connection(*args):yield NS(iter_dialogs=dialogs)
        with patch('descargas_app.moderation.authorized',return_value=True),patch('descargas_app.account_chat.connected',connection):
            await d.destinations(s,99,'12')
        self.assertEqual(saved,{})
        self.assertIn('No se pudo completar',s.tell.call_args.args[1])
