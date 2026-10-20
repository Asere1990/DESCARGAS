import asyncio,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from descargas_app.admin_panel import AdminPanel,country_code,person
from descargas_app.protected import require_source,ProtectedSource,MESSAGE

class AdminTests(unittest.IsolatedAsyncioTestCase):
    def panel(self):
        s=NS(bot_id=123,almacen=-1009,api=AsyncMock(),repo=NS(decode=lambda x:x,encode=lambda x:x))
        p=AdminPanel(s);p.pool=NS(fetchrow=AsyncMock(),fetch=AsyncMock(),fetchval=AsyncMock(),execute=AsyncMock());return p
    async def test_search_validates_positive_user_id_and_dispatches_detail(self):
        p=self.panel();p.detail=AsyncMock()
        with patch('descargas_app.admin_panel.authorized',return_value=True):
            await p.run(10,10,'search','7539025033')
            p.detail.assert_awaited_once_with(10,7539025033)
            p.detail.reset_mock();await p.run(10,10,'search','-100123')
            p.detail.assert_not_awaited()
            self.assertIn('/search telegram_id',p.s.api.call_args.args[1]['text'])
    async def test_dashboard_labels_recent_activity_without_claiming_open_chats(self):
        p=self.panel();p.pool.fetchval.side_effect=[100,12,3]
        with patch('descargas_app.admin_panel.authorized',return_value=True):await p.run(10,10,'datos','')
        text=p.s.api.call_args.args[1]['text']
        self.assertIn('Usuarios registrados: 100',text)
        self.assertIn('últimos 5 minutos: 12',text)
        self.assertIn('descargando ahora: 3',text)
        self.assertIn('no informa',text)

    def test_country_alias_and_html_safe_mention(self):
        self.assertEqual(country_code('Cuba'),'CU');self.assertEqual(country_code('ES'),'ES')
        self.assertEqual(country_code('España'),'ES')
        self.assertIn('&lt;',person(10,'<name>'))
    async def test_nonadmin_cannot_get_reports_or_callbacks(self):
        p=self.panel()
        with patch('descargas_app.admin_panel.authorized',return_value=False):
            self.assertTrue(await p.update({'message':{'from':{'id':10},'chat':{'id':10,'type':'private'},'text':'/usuarios'}}))
        p.pool.fetch.assert_not_awaited();self.assertFalse(p.tasks)
    async def test_admin_report_runs_in_background_and_chat_scope_is_enforced(self):
        p=self.panel();entered=asyncio.Event();finish=asyncio.Event()
        async def run(*args):entered.set();await finish.wait()
        p.run=run
        with patch('descargas_app.admin_panel.authorized',return_value=True):
            self.assertTrue(await p.update({'message':{'from':{'id':10},'chat':{'id':-1009,'type':'supergroup'},'text':'/telefono Cuba'}}))
            await asyncio.wait_for(entered.wait(),1)
            self.assertFalse(finish.is_set());finish.set();await asyncio.gather(*p.tasks)
            self.assertTrue(await p.update({'message':{'from':{'id':10},'chat':{'id':-999,'type':'supergroup'},'text':'/datos'}}))
    async def test_numeric_selection_is_bound_to_admin_and_chat(self):
        p=self.panel();p.pool.fetchrow.return_value={'payload':{'ids':[77]}};p.detail=AsyncMock()
        with patch('descargas_app.admin_panel.authorized',return_value=True):await p.run(10,-1009,'selection','1')
        p.detail.assert_awaited_once_with(-1009,77)
        self.assertEqual(p.pool.fetchrow.call_args.args[1:4],(123,10,-1009))
    def test_protected_ids_with_comments_and_exact_response(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'protegidos';path.write_text('# comment\n-100123 # group\n')
            with patch('descargas_app.protected.FILE',path):
                with self.assertRaisesRegex(ProtectedSource,'Este grupo esta protegido'):require_source(-100123)
                require_source(-100999)
