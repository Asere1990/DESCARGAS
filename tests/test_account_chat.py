import json
import time
import unittest
from contextlib import asynccontextmanager
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from descargas_app import account_chat as c

class PermissionTests(unittest.TestCase):
    def member(self,**kwargs):
        return NS(is_admin=False,is_creator=False,has_left=False,post_messages=False,participant=NS(**kwargs))

    def test_union_of_default_and_personal_restrictions(self):
        p=c.effective_permissions(NS(broadcast=False),self.member(banned_rights=NS(send_videos=True)),NS(send_photos=True))
        self.assertTrue(p['text']);self.assertTrue(p['links'])
        self.assertFalse(p['photos']);self.assertFalse(p['videos'])
        self.assertTrue(p['documents'])

    def test_text_links_and_previews_are_different(self):
        p=c.effective_permissions(NS(),self.member(),NS(embed_links=True))
        self.assertTrue(p['text']);self.assertTrue(p['links']);self.assertFalse(p['previews'])

    def test_expired_restriction_does_not_block(self):
        rights=NS(send_messages=True,until_date=datetime.now(timezone.utc)-timedelta(days=1))
        p=c.effective_permissions(NS(),self.member(banned_rights=rights),None)
        self.assertTrue(p['text']);self.assertTrue(p['photos'])

    def test_restricted_member_is_not_mistaken_for_removed_member(self):
        p=c.effective_permissions(NS(),self.member(left=False,banned_rights=NS(send_photos=True)),None)
        self.assertTrue(p['text']);self.assertFalse(p['photos'])

    def test_channel_member_cannot_post_but_publishing_admin_can(self):
        entity=NS(broadcast=True)
        p=self.member()
        self.assertFalse(c.effective_permissions(entity,p,None)['text'])
        p.is_admin=True
        self.assertFalse(c.effective_permissions(entity,p,None)['text'])
        p.post_messages=True
        self.assertTrue(c.effective_permissions(entity,p,None)['text'])

    def test_group_admin_ignores_member_defaults(self):
        p=self.member();p.is_admin=True
        rights=NS(send_messages=True,send_media=True)
        self.assertTrue(c.effective_permissions(NS(),p,rights)['photos'])

    def test_left_and_all_message_restrictions_prevent_every_send(self):
        member=self.member(left=True)
        p=c.effective_permissions(NS(),member,None)
        self.assertFalse(p['text']);self.assertFalse(p['photos'])
        p=c.effective_permissions(NS(),self.member(),NS(send_messages=True))
        self.assertFalse(p['text']);self.assertFalse(p['photos'])


class AccountChatTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        return NS(bot_id=7,tell=AsyncMock(),repo=NS(setting=AsyncMock(),set_setting=AsyncMock(),pool=NS(fetchrow=AsyncMock(return_value={'update_id':55}),execute=AsyncMock())))

    @asynccontextmanager
    async def connection(self,service,uid):
        self.selected_uid=uid
        yield self.client

    async def test_selected_number_uses_correct_account_scoped_mapping(self):
        s=self.service();self.client=object()
        s.repo.setting.return_value=json.dumps({'ids':[-22,-1000000000123],'expires':time.time()+60})
        perms={'role':'Miembro',**{k:True for k in ['text','links','previews','photos','videos','documents','audio','voices','stickers','gifs','polls']}}
        with patch.object(c,'authorized',return_value=True),patch.object(c,'connected',self.connection),patch.object(c,'inspect_permissions',AsyncMock(return_value=(NS(title='Prueba'),perms))) as inspect:
            await c.show_permissions(s,99,'12,2')
        inspect.assert_awaited_once_with(self.client,-1000000000123)
        self.assertEqual(self.selected_uid,12)
        s.repo.setting.assert_awaited_once_with('account_groups:7:99:12')
        self.assertIn('Enviar fotos: ✅ Sí',s.tell.call_args.args[1])

    async def test_expired_list_requires_new_list_without_reading_session(self):
        s=self.service();s.repo.setting.return_value=json.dumps({'ids':[-22],'expires':time.time()-1})
        with patch.object(c,'authorized',return_value=True),patch.object(c,'connected',AsyncMock()) as connect:
            await c.show_permissions(s,99,'12,1')
        connect.assert_not_called()
        self.assertIn('/grupos 12',s.tell.call_args.args[1])

    async def test_send_preserves_multiline_and_literal_markup(self):
        s=self.service();self.client=NS(send_message=AsyncMock(return_value=NS(id=44)))
        entity=NS(title='Destino')
        with patch.object(c,'authorized',return_value=True),patch.object(c,'connected',self.connection),patch.object(c,'inspect_permissions',AsyncMock(return_value=(entity,{'text':True,'previews':False}))):
            await c.send_text(s,99,'12 -22 Hola **literal**\nhttps://ejemplo.com',55)
        self.client.send_message.assert_awaited_once_with(entity,'Hola **literal**\nhttps://ejemplo.com',parse_mode=None,link_preview=False)
        self.assertEqual(self.selected_uid,12)
        self.assertIn("state='sent'",s.repo.pool.execute.call_args.args[0])

    async def test_duplicate_update_never_sends_twice(self):
        s=self.service();s.repo.pool.fetchrow.return_value=None
        self.client=NS(send_message=AsyncMock())
        with patch.object(c,'authorized',return_value=True),patch.object(c,'connected',self.connection),patch.object(c,'inspect_permissions',AsyncMock(return_value=(NS(title='Destino'),{'text':True,'previews':True}))):
            await c.send_text(s,99,'12 -22 Hola',55)
        self.client.send_message.assert_not_awaited()

    async def test_permission_denial_prevents_send(self):
        s=self.service();self.client=NS(send_message=AsyncMock())
        with patch.object(c,'authorized',return_value=True),patch.object(c,'connected',self.connection),patch.object(c,'inspect_permissions',AsyncMock(return_value=(NS(),{'text':False}))):
            await c.send_text(s,99,'12 -22 Hola',55)
        self.client.send_message.assert_not_awaited()
        s.repo.pool.fetchrow.assert_not_awaited()

    async def test_ambiguous_network_send_is_recorded_for_review(self):
        s=self.service();self.client=NS(send_message=AsyncMock(side_effect=TimeoutError()))
        with patch.object(c,'authorized',return_value=True),patch.object(c,'connected',self.connection),patch.object(c,'inspect_permissions',AsyncMock(return_value=(NS(),{'text':True,'previews':True}))):
            await c.send_text(s,99,'12 -22 Hola',55)
        self.client.send_message.assert_awaited_once()
        self.assertEqual(s.repo.pool.execute.call_args.args[-1],'review')
        self.assertIn('podría haberse publicado',s.tell.call_args.args[1])

    async def test_unauthorized_admin_cannot_send_or_read_account(self):
        s=self.service()
        with patch.object(c,'authorized',return_value=False),patch.object(c,'connected',AsyncMock()) as connect:
            await c.send_text(s,99,'12 -22 Hola',55)
            await c.show_permissions(s,99,'12,1')
        connect.assert_not_called();s.repo.setting.assert_not_awaited()

    async def test_selection_and_chat_routes_keep_old_numeric_routes(self):
        from descargas_app.admin_panel import AdminPanel
        s=self.service();s.almacen=-22;s.api=AsyncMock()
        panel=AdminPanel(s);panel.run=AsyncMock()
        with patch('descargas_app.admin_panel.authorized',return_value=True):
            for text in ['12,2','/chat 12 -22 Hola','3']:
                await panel.update({'update_id':55,'message':{'text':text,'from':{'id':99},'chat':{'type':'private','id':99}}})
                import asyncio
                await asyncio.gather(*panel.tasks)
        self.assertEqual([call.args[2] for call in panel.run.await_args_list],['account_permissions','chat','selection'])
