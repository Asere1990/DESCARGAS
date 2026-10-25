import io
import json
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from aiohttp import FormData
from telethon import types,functions
from tests import test_web_panel as fixtures
message=fixtures.message

class Client(N):
    async def __call__(self,request):return await self.rpc(request)

class ActionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.PanelTests.asyncSetUp(self)
        for key,value in [('authorized',lambda actor:actor in self.allowed),('require_account',self.account_guard),('require_source',self.guard)]:
            patcher=patch('descargas_app.web_actions.'+key,value);patcher.start();self.addCleanup(patcher.stop)
        self.client=Client(**vars(self.client));self.client.rpc=AsyncMock(return_value=N())
        self.client.send_message=AsyncMock(return_value=N(id=42))
        self.client.send_file=AsyncMock(return_value=N(id=43))
        self.client.edit_message=AsyncMock();self.client.delete_messages=AsyncMock();self.client.pin_message=AsyncMock();self.client.unpin_message=AsyncMock();self.client.send_read_acknowledge=AsyncMock();self.client.forward_messages=AsyncMock(return_value=N(id=44))
        self.receipts=set()
        async def claim(sql,key,value):
            if key in self.receipts:return None
            self.receipts.add(key);self.settings[key]=value;return key
        self.pool.fetchval=AsyncMock(side_effect=claim)
        m=message(9,out=True);m.peer_id=types.PeerUser(55)
        self.client.get_messages.return_value=m
    async def setup_http(self):return await fixtures.PanelTests.setup_http(self)
    def payload(self,action='send',**kw):return {'action':action,'account':'22','peer':self.peer,'request':'a'*32,'text':'Hola',**kw}
    def group(self,admin=True,grant=True):
        entity=types.Channel(id=77,title='Grupo de prueba',photo=types.ChatPhotoEmpty(),date=None,access_hash=123,megagroup=True)
        self.entity=entity;self.client.get_entity.return_value=entity
        self.peer=self.panel.pack('peer',actor=111,uid=22,generation=3,**__import__('descargas_app.web_panel',fromlist=['peer_data']).peer_data(entity,22))
        own=N(is_creator=False,is_admin=admin,has_left=False,participant=N(admin_rights=types.ChatAdminRights(add_admins=grant,ban_users=True,delete_messages=True,change_info=True,pin_messages=True,invite_users=True),can_edit=True))
        target=N(is_creator=False,is_admin=False,has_left=False,participant=N())
        async def permissions(e,user=None):return own if user=='me' else N() if user is None else target
        self.client.get_permissions=AsyncMock(side_effect=permissions)
        self.own=own;self.target_member=target
        return entity
    async def test_send_is_explicit_scoped_and_duplicate_request_cannot_send_twice(self):
        result=await self.panel.actions.execute(111,self.payload())
        self.assertEqual(result['message'],42);self.client.send_message.assert_awaited_once_with(self.entity,'Hola',parse_mode=None,reply_to=None,link_preview=True)
        with self.assertRaisesRegex(ValueError,'ya se procesó'):await self.panel.actions.execute(111,self.payload())
        self.assertEqual(self.client.send_message.await_count,1)
        self.assertEqual(json.loads(self.settings[next(iter(self.receipts))])['state'],'done')
    async def test_send_uncertainty_is_held_for_review_without_automatic_replay(self):
        self.client.send_message.side_effect=ConnectionError()
        with self.assertRaisesRegex(ValueError,'confirmar'):await self.panel.actions.execute(111,self.payload())
        self.assertEqual(json.loads(self.settings[next(iter(self.receipts))])['state'],'review')
        with self.assertRaisesRegex(ValueError,'ya se procesó'):await self.panel.actions.execute(111,self.payload())
        self.assertEqual(self.client.send_message.await_count,1)
    async def test_action_api_requires_origin_csrf_and_current_admin_role(self):
        await self.setup_http()
        r=await self.http.post('/panel/api/action',headers={'Origin':'http://test'},json=self.payload());self.assertEqual(r.status,403)
        headers={'Origin':'http://test','X-Panel-CSRF':self.csrf}
        r=await self.http.post('/panel/api/action',headers=headers,json=self.payload());self.assertEqual(r.status,200)
        self.allowed.clear();r=await self.http.post('/panel/api/action',headers=headers,json=self.payload(request='b'*32));self.assertEqual(r.status,403)
        self.assertEqual(self.client.send_message.await_count,1)
    async def test_reply_and_delete_verify_message_peer_before_any_mutation(self):
        self.client.get_messages.return_value.peer_id=types.PeerUser(99)
        with self.assertRaisesRegex(ValueError,'pertenece'):await self.panel.actions.execute(111,self.payload(reply=9))
        with self.assertRaisesRegex(ValueError,'pertenece'):await self.panel.actions.execute(111,self.payload('delete',message=9))
        self.client.send_message.assert_not_awaited();self.client.delete_messages.assert_not_awaited();self.assertFalse(self.receipts)
    async def test_edit_delete_pin_and_read_call_only_selected_message(self):
        for i,action in enumerate(['edit','delete','pin','unpin','read']):
            await self.panel.actions.execute(111,self.payload(action,message=9,request=str(i)*32))
        self.client.edit_message.assert_awaited_once_with(self.entity,9,'Hola',parse_mode=None,link_preview=True)
        self.client.delete_messages.assert_awaited_once_with(self.entity,[9],revoke=True)
        self.client.pin_message.assert_awaited_once_with(self.entity,9,notify=False)
        self.client.unpin_message.assert_awaited_once_with(self.entity,9)
        self.client.send_read_acknowledge.assert_awaited_once_with(self.entity,max_id=9)
    async def test_member_without_publish_permissions_cannot_post_in_channel(self):
        self.group(admin=False);self.entity.broadcast=True
        with self.assertRaisesRegex(ValueError,'no permite'):await self.panel.actions.execute(111,self.payload())
        self.client.send_message.assert_not_awaited();self.assertFalse(self.receipts)
    async def test_admin_can_grant_only_own_rights_and_never_change_owner(self):
        self.group();user=types.User(id=88,access_hash=2,first_name='Miembro')
        async def resolve(value):return self.entity if isinstance(value,types.InputPeerChannel) else user
        self.client.get_entity.side_effect=resolve
        with self.assertRaisesRegex(ValueError,'otorgar'):await self.panel.actions.execute(111,self.payload('promote',target='@miembro',rights=['post_messages']))
        self.assertFalse(self.receipts)
        await self.panel.actions.execute(111,self.payload('promote',target='@miembro',rights=['delete_messages'],rank='Equipo'))
        rpc=self.client.rpc.call_args.args[0];self.assertIsInstance(rpc,functions.channels.EditAdminRequest);self.assertTrue(rpc.admin_rights.delete_messages);self.assertFalse(bool(rpc.admin_rights.add_admins));self.assertEqual(rpc.rank,'Equipo')
        self.target_member.is_creator=True
        with self.assertRaisesRegex(ValueError,'propietario'):await self.panel.actions.execute(111,self.payload('demote',target='@miembro',request='b'*32))
    async def test_admin_without_add_admins_cannot_promote(self):
        self.group(grant=False);user=types.User(id=88,first_name='Miembro')
        self.client.get_entity.side_effect=lambda v:self.entity if isinstance(v,types.InputPeerChannel) else user
        with self.assertRaisesRegex(ValueError,'permiso'):await self.panel.actions.execute(111,self.payload('promote',target='@miembro',rights=['delete_messages']))
        self.client.rpc.assert_not_awaited()
    async def test_protected_messages_remain_viewable_but_cannot_be_forwarded(self):
        self.client.get_messages.return_value.noforwards=True
        with self.assertRaisesRegex(ValueError,'protege'):await self.panel.actions.execute(111,self.payload('forward',message=9,destination=self.peer))
        self.client.forward_messages.assert_not_awaited()
    async def test_upload_streams_a_file_larger_than_legacy_miniapp_body_limit(self):
        await self.setup_http()
        data=FormData();data.add_field('data',json.dumps(self.payload('upload',kind='document')))
        payload=b'x'*(700*1024);data.add_field('file',io.BytesIO(payload),filename='prueba.bin',content_type='application/octet-stream')
        read=[]
        async def send(entity,file,**kwargs):read.append(file.read());return N(id=43)
        self.client.send_file.side_effect=send
        r=await self.http.post('/panel/api/upload',headers={'Origin':'http://test','X-Panel-CSRF':self.csrf},data=data)
        self.assertEqual(r.status,200,await r.text());self.assertEqual(read,[payload]);self.assertEqual(self.client.send_file.await_count,1)
    async def test_moderation_checks_permissions_before_rpc(self):
        self.group();user=types.User(id=88,first_name='Miembro')
        self.client.get_entity.side_effect=lambda v:self.entity if isinstance(v,types.InputPeerChannel) else user
        await self.panel.actions.execute(111,self.payload('ban',target='@miembro'))
        rpc=self.client.rpc.call_args.args[0];self.assertIsInstance(rpc,functions.channels.EditBannedRequest);self.assertTrue(rpc.banned_rights.view_messages)
        self.target_member.is_admin=True
        with self.assertRaisesRegex(ValueError,'Retira primero'):await self.panel.actions.execute(111,self.payload('restrict',target='@miembro',request='b'*32))
    async def test_inspector_exposes_real_permissions_without_mutations(self):
        self.group();result=await self.panel.actions.inspect(111,22,self.peer)
        self.assertTrue(result['rights']['add_admins']);self.assertTrue(result['rights']['delete_messages']);self.assertNotIn('post_messages',[r['flag'] for r in result['grant']]);self.assertFalse(self.receipts)
