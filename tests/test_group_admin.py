import asyncio
import json
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from telethon import types,functions,errors
from descargas_app import group_admin as g,manual_board as b
from descargas_app.admin_panel import AdminPanel
import test_manual_board as fixtures


class Client:
    def __init__(self,own,target):
        self.calls=[]
        self.get_permissions=AsyncMock(side_effect=lambda entity,user:own if user=='me' else target)
        self.get_entity=AsyncMock(return_value=types.User(id=42,first_name='Test'))
    async def __call__(self,request):self.calls.append(request);return True


class GroupAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.BoardTests.asyncSetUp(self)
        self.pool.db.execute("CREATE TABLE descarga_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.own=NS(is_creator=False,is_admin=True,has_left=False,participant=NS(admin_rights=types.ChatAdminRights(add_admins=True,ban_users=True,delete_messages=True),can_edit=True))
        self.target=NS(is_creator=False,is_admin=False,has_left=False,participant=NS(banned_rights=None))
        self.client=Client(self.own,self.target)
        self.entity=types.Channel(id=123,title='Grupo',photo=types.ChatPhotoEmpty(),date=None,megagroup=True,default_banned_rights=types.ChatBannedRights(until_date=0,send_photos=True))
        @asynccontextmanager
        async def connected(*a,**kw):yield self.client
        self.patches=[patch.object(g,'authorized',return_value=True),patch.object(g,'connected',connected),patch.object(g,'entity_for',AsyncMock(return_value=self.entity))]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        await g.bind(self.s,99,await self.row())
        self.state=await g.load(self.s,99,g.key)
    async def row(self):return await self.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE token=$1','token')

    def test_comma_selection_rejects_malformed_and_out_of_range(self):
        self.assertEqual(g.parse_numbers('1, 3,4,1',4),[1,3,4])
        for value in ('','0','1 3','1,','-1','1,5','1,2 y 3'):
            with self.assertRaises(ValueError):g.parse_numbers(value,4)

    def test_default_legacy_aggregate_expansion_preserves_other_permissions(self):
        original=types.ChatBannedRights(until_date=0,send_messages=True,send_media=True,invite_users=True)
        result=g.defaults_copy(original);result.send_photos=False
        self.assertTrue(original.send_messages)
        self.assertTrue(result.send_plain);self.assertTrue(result.send_videos);self.assertTrue(result.invite_users)
        self.assertFalse(result.send_photos);self.assertFalse(result.send_messages);self.assertFalse(result.send_media)

    def test_owner_admin_and_member_header_and_channel_flag_filter(self):
        member=g.entity_snapshot(self.entity)
        self.assertEqual(member['role'],'Miembro regular')
        self.entity.creator=True
        self.assertEqual(g.entity_snapshot(self.entity)['role'],'Propietario')
        self.entity.creator=False;self.entity.admin_rights=types.ChatAdminRights(delete_messages=True)
        self.assertEqual(g.entity_snapshot(self.entity)['role'],'Administrador')
        self.assertIn('❌',g.header(g.entity_snapshot(self.entity)))
        self.entity.broadcast=True
        flags={f for f,_ in g.applicable(self.entity)}
        self.assertIn('post_messages',flags);self.assertNotIn('anonymous',flags)

    async def test_promote_lists_only_grantable_rights_then_applies_selected_and_tag(self):
        await g.execute(self.s,99,self.state,'add_id',42,100)
        draft=await g.load(self.s,99,g.pending_key)
        self.assertEqual({f for f,_ in draft['choices']},{'add_admins','ban_users','delete_messages'})
        await g.input_text(self.s,99,'1,3',101)
        self.assertFalse(self.client.calls)
        await g.input_text(self.s,99,'Ayudante',102)
        request=self.client.calls[0]
        self.assertIsInstance(request,functions.channels.EditAdminRequest)
        self.assertTrue(request.admin_rights.delete_messages);self.assertTrue(request.admin_rights.add_admins)
        self.assertFalse(request.admin_rights.ban_users);self.assertEqual(request.rank,'Ayudante')
        self.assertFalse(await g.load(self.s,99,g.pending_key))

    async def test_fresh_permission_loss_prevents_privilege_escalation(self):
        await g.execute(self.s,99,self.state,'add_id',42,100)
        await g.input_text(self.s,99,'1',101)
        self.own.participant.admin_rights.delete_messages=False
        with self.assertRaisesRegex(ValueError,'cambiaron'):await g.input_text(self.s,99,'-',102)
        self.assertFalse(self.client.calls)

    async def test_cannot_promote_without_add_admins_or_demote_owner_or_uneditable_admin(self):
        self.own.participant.admin_rights.add_admins=False
        with self.assertRaisesRegex(ValueError,'no tiene permiso'):await g.execute(self.s,99,self.state,'add_id',42,1)
        self.own.participant.admin_rights.add_admins=True
        self.target.is_creator=True
        with self.assertRaisesRegex(ValueError,'propietario'):await g.execute(self.s,99,self.state,'remove_id',42,2)
        self.target.is_creator=False;self.target.is_admin=True;self.target.participant.can_edit=False
        with self.assertRaisesRegex(ValueError,'no puede editar'):await g.execute(self.s,99,self.state,'remove_id',42,3)
        self.assertFalse(self.client.calls)

    async def test_demote_uses_empty_admin_rights(self):
        self.target.is_admin=True;self.target.participant.can_edit=True
        await g.execute(self.s,99,self.state,'remove_id',42,20)
        request=self.client.calls[0]
        self.assertIsInstance(request,functions.channels.EditAdminRequest)
        self.assertFalse(request.admin_rights.add_admins);self.assertFalse(request.admin_rights.delete_messages)

    async def test_default_toggle_preserves_others_and_duplicate_update_never_toggles_twice(self):
        state=dict(self.state,selected=['send_photos'])
        await g.execute(self.s,99,state,'defaults',None,55)
        request=self.client.calls[0]
        self.assertIsInstance(request,functions.messages.EditChatDefaultBannedRightsRequest)
        self.assertFalse(request.banned_rights.send_photos)
        self.assertFalse(request.banned_rights.send_videos)
        with self.assertRaisesRegex(ValueError,'ya se procesó'):await g.execute(self.s,99,state,'defaults',None,55)
        self.assertEqual(len(self.client.calls),1)

    async def test_ban_and_free_are_group_rpc_and_respect_individual_ban(self):
        await g.command(self.s,99,'/ban 42',20)
        self.assertIsInstance(self.client.calls[0],functions.channels.EditBannedRequest)
        self.assertTrue(self.client.calls[0].banned_rights.view_messages)
        self.target.participant.banned_rights=types.ChatBannedRights(until_date=0,view_messages=True)
        with self.assertRaisesRegex(ValueError,'/unban'):await g.command(self.s,99,'/free 42',21)
        await g.command(self.s,99,'/unban 42',22)
        self.assertFalse(self.client.calls[-1].banned_rights.view_messages)
        await g.command(self.s,99,'/restringir 42',23)
        self.assertTrue(self.client.calls[-1].banned_rights.send_messages)

    async def test_info_reports_restriction_separately_from_ban(self):
        self.target.participant.banned_rights=types.ChatBannedRights(until_date=0,send_photos=True)
        await g.execute(self.s,99,self.state,'info_id',42,25)
        text=self.s.tell.call_args.args[1]
        self.assertIn('Baneado: No',text);self.assertIn('Restringido: Sí',text)
        self.assertIn('Miembro actual: Sí',text);self.assertIn('/free 42',text)
        self.assertFalse(self.client.calls)

    async def test_stale_generation_expiry_closed_or_wrong_actor_never_mutates(self):
        for state in (dict(self.state,expires=0),dict(self.state,generation=10)):
            with self.assertRaises(ValueError):await g.execute(self.s,99,state,'ban',42,31)
        with self.assertRaises(ValueError):await g.execute(self.s,98,self.state,'ban',42,32)
        await self.pool.execute('UPDATE descarga_manual_boards SET closed=TRUE')
        with self.assertRaises(ValueError):await g.execute(self.s,99,self.state,'ban',42,33)
        self.assertFalse(self.client.calls)

    async def test_cleared_context_leaves_global_commands_untouched(self):
        await g.clear(self.s,99)
        self.assertFalse(await g.command(self.s,99,'/ban 42',1))
        self.assertFalse(await g.command(self.s,99,'/free 42',2))
        self.assertFalse(self.client.calls)

    async def test_no_self_mutation_and_busy_download_not_interrupted(self):
        with self.assertRaisesRegex(ValueError,'propia cuenta'):await g.execute(self.s,99,self.state,'ban',12,50)
        await self.pool.execute('UPDATE descarga_manual_boards SET busy=TRUE')
        with self.assertRaisesRegex(ValueError,'descarga'):await g.execute(self.s,99,self.state,'ban',42,51)
        self.assertFalse(self.client.calls)

    async def test_group_buttons_are_admin_private_and_stale_message_fenced(self):
        query={'data':'manual:ga:token:0','message':{'chat':{'id':99},'message_id':50}}
        with patch.object(g,'authorized',return_value=False):
            with self.assertRaisesRegex(ValueError,'Solo administradores'):await g.callback(self.s,99,query)
        query['message']['message_id']=49
        with self.assertRaises(ValueError):await g.callback(self.s,99,query)
        self.assertFalse(self.client.calls)

    async def test_panel_routes_comma_input_in_background_before_old_account_selection(self):
        panel=AdminPanel(self.s);panel.run=AsyncMock()
        await g.prompt(self.s,99,self.state,'grant',choices=[['ban_users','Expulsar']])
        with patch('descargas_app.admin_panel.authorized',return_value=True):
            self.assertTrue(await panel.update({'update_id':9,'message':{'from':{'id':99},'chat':{'id':99,'type':'private'},'text':'1,3,4'}}))
            await asyncio.gather(*panel.tasks)
        self.assertEqual(panel.run.call_args.args[2:4],('group_input','1,3,4'))

    async def test_panel_does_not_capture_global_ban_without_group_context(self):
        panel=AdminPanel(self.s)
        await g.clear(self.s,99)
        with patch('descargas_app.admin_panel.authorized',return_value=True):
            self.assertFalse(await panel.update({'message':{'from':{'id':99},'chat':{'id':99,'type':'private'},'text':'/ban 42'}}))
        self.assertFalse(panel.tasks)

    async def test_board_permissions_do_not_change_download_buttons_or_saved_messages(self):
        row=await self.row();data=self.s.repo.decode(row['payload'])
        data['account_rights']=g.entity_snapshot(self.entity)
        text,keyboard=b.presentation(row,data)
        actions=[v['callback_data'].split(':')[1] for line in keyboard for v in line]
        for action in ('d','t','ga','gr','gu','gi','gl'):self.assertIn(action,actions)
        self.assertIn('Rol: Miembro regular',text)
        _,keyboard=b.presentation(dict(row,source=row['uid']),data)
        self.assertFalse(any(v['callback_data'].startswith('manual:ga:') for line in keyboard for v in line))

    async def test_group_info_refreshes_header_and_numbers_only_relevant_defaults(self):
        await g.execute(self.s,99,self.state,'group_info',None,70)
        text=self.s.tell.call_args.args[1]
        self.assertIn('PERMISOS DE LOS MIEMBROS',text)
        self.assertIn('2. Enviar fotos: ❌',text)
        draft=await g.load(self.s,99,g.pending_key)
        self.assertEqual(draft['stage'],'defaults')
        await g.input_text(self.s,99,'2',71)
        self.assertFalse(self.client.calls[-1].banned_rights.send_photos)
        data=self.s.repo.decode((await self.row())['payload'])
        self.assertEqual(data['account_rights']['role'],'Administrador')

    async def test_expired_restriction_not_reported_as_active(self):
        self.target.participant.banned_rights=types.ChatBannedRights(until_date=1,view_messages=True)
        self.assertIsNone(g.active_bans(self.target))
        await g.execute(self.s,99,self.state,'info_id',42,80)
        self.assertIn('Baneado: No',self.s.tell.call_args.args[1])

    async def test_changed_draft_and_telegram_timeout_do_not_repeat_mutation(self):
        await g.execute(self.s,99,self.state,'add_id',42,90)
        await g.input_text(self.s,99,'1',91)
        old=await g.load(self.s,99,g.pending_key)
        await g.prompt(self.s,99,self.state,'info_id')
        with self.assertRaisesRegex(ValueError,'selección de permisos cambió'):
            await g.execute(self.s,99,dict(old,rank=''),'promote',42,92)
        self.assertFalse(self.client.calls)
        class TimeoutClient(Client):
            async def __call__(self,request):
                self.calls.append(request)
                raise TimeoutError()
        self.client=TimeoutClient(self.own,self.target)
        with self.assertRaisesRegex(ValueError,'no confirmó'):
            await g.execute(self.s,99,self.state,'ban',42,93)
        with self.assertRaisesRegex(ValueError,'ya se procesó'):
            await g.execute(self.s,99,self.state,'ban',42,93)
        self.assertEqual(len(self.client.calls),1)

    async def test_unknown_id_lookup_is_bounded_and_username_can_resolve_bot(self):
        self.client.get_entity.side_effect=ValueError('not cached')
        calls=[]
        async def participants(entity,**kw):
            calls.append(kw)
            for uid in (1,2):yield types.User(id=uid)
        self.client.iter_participants=participants
        with self.assertRaisesRegex(ValueError,'@username'):
            await g.target_user(self.s,self.client,self.entity,1234)
        self.assertEqual(calls,[{'limit':200}])
        self.client.get_entity.side_effect=None
        self.client.get_entity.return_value=types.User(id=42,bot=True,first_name='Bot')
        result=await g.target_user(self.s,self.client,self.entity,'@ExampleBot')
        self.assertEqual(result.id,42);self.assertTrue(result.bot)

    async def test_admin_directory_filters_admins_and_lists_owner_accounts_and_bots(self):
        owner=types.User(id=12,access_hash=1200,first_name='Dueño',username='Owner')
        owner.participant=types.ChannelParticipantCreator(user_id=12,admin_rights=types.ChatAdminRights())
        bot=types.User(id=42,access_hash=424242,first_name='Bot Admin',username='HelperBot',bot=True)
        person=types.User(id=43,access_hash=434343,first_name='Carlos')
        filters=[]
        async def participants(entity,**kwargs):
            filters.append(kwargs)
            for user in (person,bot,owner,bot):yield user
        self.client.iter_participants=participants
        await g.execute(self.s,99,self.state,'admins',None,1)
        self.assertIsInstance(filters[0]['filter'],types.ChannelParticipantsAdmins)
        draft=await g.load(self.s,99,g.pending_key)
        users=self.s.repo.decode(bytes.fromhex(draft['admins']))['users']
        self.assertEqual([u['id'] for u in users],[12,42,43])
        text=self.s.tell.call_args.args[1]
        self.assertIn('ADMINISTRADORES · 3',text)
        self.assertIn('1. ⭐️ Dueño',text);self.assertIn('2. 👨🏻‍💻 Bot Admin · Bot',text)
        self.assertIn('Sin username',text);self.assertNotIn('424242',text)
        self.assertFalse(self.client.calls)

    async def test_admin_number_fetches_fresh_bot_identity_and_permissions_then_allows_another_number(self):
        user=types.User(id=42,access_hash=4444,first_name='Ayudante',username='HelperBot',bot=True)
        self.client.get_entity.return_value=user
        async def participants(entity,**kwargs):yield user
        self.client.iter_participants=participants
        await g.execute(self.s,99,self.state,'admins',None,1)
        self.target.is_admin=True;self.target.participant.admin_rights=types.ChatAdminRights(delete_messages=True)
        await g.input_text(self.s,99,'1',2)
        text=self.s.tell.call_args.args[1]
        for value in ('Nombre en Telegram: Ayudante','Telegram ID: 42','Username: @HelperBot','Tipo: Bot','Rol: Administrador','Eliminar mensajes'):
            self.assertIn(value,text)
        peer=self.client.get_entity.call_args.args[0]
        self.assertIsInstance(peer,types.InputUser);self.assertEqual(peer.access_hash,4444)
        self.assertEqual((await g.load(self.s,99,g.pending_key))['stage'],'admins')
        self.target.is_admin=False
        await g.input_text(self.s,99,'1',3)
        self.assertIn('Rol: Miembro regular',self.s.tell.call_args.args[1])
        self.assertFalse(self.client.calls)

    async def test_admin_list_selection_rejects_bad_numbers_and_closed_group(self):
        user=types.User(id=42,access_hash=4444,first_name='Admin')
        async def participants(entity,**kwargs):yield user
        self.client.iter_participants=participants
        await g.execute(self.s,99,self.state,'admins',None,1)
        for update,value in enumerate(('0','2','1,2','-1','hello'),10):
            with self.assertRaisesRegex(ValueError,'número'):await g.input_text(self.s,99,value,update)
        await g.clear(self.s,99)
        self.assertFalse(await g.input_text(self.s,99,'1',3))
        self.assertFalse(self.client.calls)

    async def test_admin_directory_pages_keep_global_numbers_and_telegram_text_limits(self):
        async def participants(entity,**kwargs):
            for i in range(80):
                yield types.User(id=1000+i,access_hash=9000+i,first_name=f'Admin {i:03d} '+('X'*120))
        self.client.iter_participants=participants
        await g.execute(self.s,99,self.state,'admins',None,1)
        pages=[c.args[1] for c in self.s.tell.call_args_list]
        self.assertGreater(len(pages),1)
        self.assertTrue(all(len(text.encode('utf-16-le'))//2<=3500 for text in pages))
        self.assertIn('80. 👨🏻‍💻 Admin 079',''.join(pages))
        draft=await g.load(self.s,99,g.pending_key)
        self.assertEqual(len(self.s.repo.decode(bytes.fromhex(draft['admins']))['users']),80)

    async def test_menu_destination_icons_keep_original_number_selection(self):
        from descargas_app.account_chat import selection_key
        users=[{'id':-100123,'name':'Propietario','kind':'Grupo','role_icon':'⭐️'},
               {'id':-100124,'name':'Administrador','kind':'Grupo','role_icon':'👨🏻‍💻'},
               {'id':-100125,'name':'Miembro','kind':'Grupo','role_icon':''}]
        await self.s.repo.set_setting(selection_key(self.s,99,12),json.dumps({'roles_version':1,'expires':time.time()+100,'generation':4,'snapshot':self.s.repo.encode({'entries':users}).hex()}))
        await b.destinations(self.s,99,12,False)
        text=self.s.api.call_args.args[1]['text']
        self.assertIn('1. ⭐️ Propietario',text);self.assertIn('2. 👨🏻‍💻 Administrador',text)
        self.assertIn('3. Miembro',text)
        self.assertIsNone(await g.load(self.s,99,g.key))

    async def test_duplicate_permission_input_cannot_be_reinterpreted_as_admin_tag(self):
        await g.execute(self.s,99,self.state,'add_id',42,100)
        await g.input_text(self.s,99,'1,3',101)
        self.assertEqual((await g.load(self.s,99,g.pending_key))['stage'],'rank')
        await g.input_text(self.s,99,'1,3',101)
        self.assertFalse(self.client.calls)
        await g.input_text(self.s,99,'Ayudante',102)
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(self.client.calls[0].rank,'Ayudante')
