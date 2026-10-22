import json
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from descargas_app import admin_home as h,account_directory as d
from descargas_app.admin_panel import AdminPanel


class HomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings={}
        async def setting(key):return self.settings.get(key)
        async def put(key,value):self.settings[key]=str(value)
        repo=NS(setting=setting,set_setting=put,encode=lambda d:json.dumps(d).encode(),decode=lambda d:json.loads(d),
                account=AsyncMock(return_value={'session':b'session','generation':1}),pace=AsyncMock())
        self.s=NS(bot_id=7,almacen=-1009,repo=repo,api=AsyncMock(return_value={}))
        self.p=AdminPanel(self.s);self.p.send=AsyncMock();self.p.pages=AsyncMock()
        self.p.pool=NS(fetchrow=AsyncMock(return_value={'users':45,'accounts':23}),fetchval=AsyncMock(return_value=1234),fetch=AsyncMock())
        self.auth=patch('descargas_app.admin_home.authorized',return_value=True);self.auth.start();self.addCleanup(self.auth.stop)
        self.panel_auth=patch('descargas_app.admin_panel.authorized',return_value=True);self.panel_auth.start();self.addCleanup(self.panel_auth.stop)

    async def test_home_shows_stored_counts_button_and_clears_group_inputs(self):
        await self.s.repo.set_setting('manual-search:7:99','old')
        await self.s.repo.set_setting('group-admin:7:99','old')
        await h.home(self.p,99)
        text=self.p.send.call_args.args[1]
        for label in ('𝐔𝐒𝐔𝐀𝐑𝐈𝐎𝐒: 45','𝐂𝐔𝐄𝐍𝐓𝐀𝐒: 23','𝐃𝐄𝐒𝐂𝐀𝐑𝐆𝐀𝐒: 1234','sesión guardada'):
            self.assertIn(label,text)
        self.assertEqual(self.p.send.call_args.args[2][0][0]['callback_data'],'stats:account_menu_list')
        self.assertEqual(self.settings['manual-search:7:99'],'');self.assertEqual(self.settings['group-admin:7:99'],'')
        sql=self.p.pool.fetchval.call_args.args[0]
        self.assertIn('descarga_user_totals',sql);self.assertNotIn('descarga_delivery_events',sql)
        self.s.repo.account.assert_not_awaited();self.s.api.assert_not_awaited()

    async def test_slow_total_is_omitted_without_losing_menu(self):
        self.p.pool.fetchval.side_effect=TimeoutError()
        await h.home(self.p,99)
        self.assertNotIn('𝐃𝐄𝐒𝐂𝐀𝐑𝐆𝐀𝐒:',self.p.send.call_args.args[1])
        self.assertIn('𝐂𝐔𝐄𝐍𝐓𝐀𝐒: 23',self.p.send.call_args.args[1])
        self.assertTrue(self.p.send.call_args.args[2])

    async def test_counts_timeout_still_leaves_working_accounts_button(self):
        self.p.pool.fetchrow.side_effect=TimeoutError()
        await h.home(self.p,99)
        self.assertIn('𝐔𝐒𝐔𝐀𝐑𝐈𝐎𝐒: No disponible',self.p.send.call_args.args[1])
        self.assertTrue(self.p.send.call_args.args[2])

    async def test_displayed_number_23_opens_its_telegram_id_not_literal_23(self):
        await h.remember(self.p,99,[{'number':23,'uid':1990314140}])
        with patch('descargas_app.manual_board.menu',AsyncMock()) as menu:
            self.assertTrue(await h.select(self.p,99,'23'))
            menu.assert_awaited_once_with(self.s,99,1990314140)
        self.s.repo.account.assert_awaited_once_with(1990314140)
        self.assertFalse(self.settings[h.key(self.s,99)])
        self.p.pool.fetch.assert_not_awaited()

    async def test_account_choices_are_actor_bound_expire_and_do_not_fall_through(self):
        await h.remember(self.p,99,[{'number':23,'uid':100}])
        self.assertFalse(await h.select(self.p,98,'23'))
        self.assertTrue(await h.select(self.p,99,'24'))
        state=json.loads(self.settings[h.key(self.s,99)]);state['expires']=0
        self.settings[h.key(self.s,99)]=json.dumps(state)
        self.assertTrue(await h.select(self.p,99,'23'))
        self.assertIn('venció',self.p.send.call_args.args[1]);self.s.repo.account.assert_not_awaited()

    async def test_disconnected_displayed_account_is_not_opened(self):
        await h.remember(self.p,99,[{'number':23,'uid':100}])
        self.s.repo.account.return_value={'session':None}
        with patch('descargas_app.manual_board.menu',AsyncMock()) as menu:
            self.assertTrue(await h.select(self.p,99,'23'));menu.assert_not_awaited()
        self.assertIn('ya no tiene sesión',self.p.send.call_args.args[1])

    async def test_no_argument_menu_and_explicit_account_menu_both_work(self):
        with patch.object(h,'home',AsyncMock()) as home,patch('descargas_app.manual_board.menu',AsyncMock()) as menu:
            await self.p.run(99,99,'menu','');home.assert_awaited_once_with(self.p,99)
            await self.p.run(99,99,'menu','1990314140');menu.assert_awaited_once_with(self.s,99,1990314140)

    async def test_selection_dispatch_preserves_group_number_flow_after_account_is_opened(self):
        await h.remember(self.p,99,[{'number':23,'uid':100}])
        with patch('descargas_app.manual_board.menu',AsyncMock()) as menu:
            await self.p.run(99,99,'selection','23');menu.assert_awaited_once_with(self.s,99,100)
        with patch('descargas_app.manual_board.select_number',AsyncMock(return_value=True)) as group:
            await self.p.run(99,99,'selection','21');group.assert_awaited_once_with(self.s,99,'21')

    async def test_account_list_keeps_global_page_numbers_and_persists_exact_snapshot(self):
        self.p.pool.fetch.return_value=[{'number':123,'uid':987654321,'profile':self.s.repo.encode({'username':'test','name':'Name'}),'name':None,'country':'CU','total':200}]
        with patch('descargas_app.moderation.authorized',return_value=True):
            await d.accounts(self.p,99,'101-200',selectable=True)
        state=json.loads(self.settings[h.key(self.s,99)])
        numbers=self.s.repo.decode(bytes.fromhex(state['snapshot']))['numbers']
        self.assertEqual(numbers,{'123':987654321})
        self.assertIn('Escribe el número',self.p.pages.call_args.args[1])

    async def test_nonadmin_cannot_read_home_or_select_and_group_chat_is_rejected(self):
        with patch.object(h,'authorized',return_value=False):
            await h.home(self.p,99);self.assertFalse(await h.select(self.p,99,'23'))
        self.p.pool.fetchrow.assert_not_awaited()
        await self.p.run(99,-1009,'menu','')
        self.assertIn('DM del bot',self.p.send.call_args.args[1])

    async def test_late_account_listing_cannot_replace_a_newer_group_menu(self):
        token=await h.begin(self.p,99)
        self.assertTrue(await h.select(self.p,99,'23'))
        self.assertIn('preparando',self.p.send.call_args.args[1])
        await h.clear(self.s,99)
        self.assertFalse(await h.remember(self.p,99,[{'number':23,'uid':100}],token=token))
        self.assertFalse(self.settings[h.key(self.s,99)])
        old=await h.begin(self.p,99)
        new=await h.begin(self.p,99)
        self.assertFalse(await h.remember(self.p,99,[{'number':23,'uid':100}],token=old))
        self.assertTrue(await h.remember(self.p,99,[{'number':23,'uid':200}],token=new))
