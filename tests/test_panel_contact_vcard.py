"""La ficha vCard de ADD debe abrirse sin hoja de compartir ni sesiones Telethon."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from descargas_app import web_panel


class ContactCardTests(unittest.TestCase):
    def test_vcard_is_valid_utf8_with_name_phone_and_telegram_user(self):
        raw=web_panel.contact_vcard_content(
            717122686, 'Joc Krlos', '+34 624 80 77 64', '')
        card=raw.decode('utf-8')
        self.assertTrue(card.startswith('BEGIN:VCARD\r\nVERSION:3.0\r\n'))
        self.assertTrue(card.endswith('END:VCARD\r\n'))
        self.assertIn('FN:Joc Krlos\r\n',card)
        self.assertIn('TEL;TYPE=CELL:+34624807764\r\n',card)
        self.assertIn('NOTE:Telegram ID: 717122686\r\n',card)

    def test_invalid_phone_rejected_and_contact_fields_escaped(self):
        with self.assertRaises(ValueError):
            web_panel.contact_vcard_content(12,'Sin teléfono','No disponible')
        raw=web_panel.contact_vcard_content(
            12,'Nombre,\nBEGIN:VCARD','+1 (213) 410-6305','@cuenta')
        card=raw.decode()
        self.assertIn('FN:Nombre\\,\\nBEGIN:VCARD\r\n',card)
        self.assertIn('NOTE:Telegram ID: 12\\nTelegram: @cuenta\r\n',card)
        self.assertEqual(card.split('\r\n').count('BEGIN:VCARD'),1)


class AccountExportTests(unittest.IsolatedAsyncioTestCase):
    def make_panel(self):
        panel=object.__new__(web_panel.TelegramPanel)
        row={'profile':json.dumps({
            'name':'Joc Krlos','phone':'+34624807764','username':''
        }), 'name':None}
        repo=SimpleNamespace(
            pool=SimpleNamespace(fetchrow=AsyncMock(return_value=row)),
            decode=json.loads
        )
        panel.s=SimpleNamespace(repo=repo,bot_id=77,ready=True)
        panel.rate=lambda *_:None
        panel.session=AsyncMock(return_value={'actor':111,'csrf':'token'})
        return panel,repo

    async def test_admin_get_returns_direct_inline_contact_without_opening_telegram(self):
        panel,repo=self.make_panel()
        with patch.object(web_panel,'authorized',return_value=True), \
             patch.object(web_panel,'require_account',new_callable=AsyncMock) as allowed:
            response=await panel.contact_vcard_response(111,717122686)
        self.assertEqual(response.status,200)
        self.assertEqual(response.content_type,'text/vcard')
        self.assertEqual(response.charset,'utf-8')
        self.assertIn('inline;',response.headers['Content-Disposition'])
        self.assertIn('.vcf',response.headers['Content-Disposition'])
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertIn(b'BEGIN:VCARD\r\nVERSION:3.0',response.body)
        self.assertIn(b'FN:Joc Krlos\r\n',response.body)
        sql,uid,bot_id=repo.pool.fetchrow.await_args.args
        self.assertTrue(sql.strip().startswith('SELECT'))
        self.assertEqual((uid,bot_id),(717122686,77))
        allowed.assert_awaited_once_with(panel.s,717122686,111)

    async def test_unauthed_actor_cannot_export_contact(self):
        panel,repo=self.make_panel()
        with patch.object(web_panel,'authorized',return_value=False):
            with self.assertRaises(web.HTTPForbidden):
                await panel.contact_vcard_response(999,717122686)
        repo.pool.fetchrow.assert_not_awaited()

    async def test_vcard_route_requires_existing_panel_session(self):
        panel,repo=self.make_panel()
        request=SimpleNamespace(
            match_info={'op':'contact.vcf'},method='GET',headers={},
            query={'account':'717122686'},remote='127.0.0.1',
            path='/panel/api/contact.vcf'
        )
        panel.contact_vcard_response=AsyncMock(return_value=web.Response(
            body=b'BEGIN:VCARD\r\n',content_type='text/vcard'))
        response=await panel.handle(request)
        self.assertEqual(response.status,200)
        panel.session.assert_awaited_once()
        panel.contact_vcard_response.assert_awaited_once_with(111,717122686)
        panel.session=AsyncMock(side_effect=ValueError('expired'))
        response=await panel.handle(request)
        self.assertEqual(response.status,401)
