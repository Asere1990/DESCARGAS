import base64
import unittest
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from telethon import types,functions
from descargas_app import panel_features as f
from tests import test_web_actions as actions
from tests import test_web_panel as fixtures

class FeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await actions.ActionsTests.asyncSetUp(self)
        for name,value in [('authorized',lambda actor:actor in self.allowed),('require_account',self.account_guard),('require_source',self.guard),('require_consent',AsyncMock())]:
            p=patch.object(f,name,value);p.start();self.addCleanup(p.stop)
    payload=actions.ActionsTests.payload
    group=actions.ActionsTests.group
    setup_http=fixtures.PanelTests.setup_http
    async def test_reply_preview_fetches_original_outside_loaded_page_and_checks_peer(self):
        original=fixtures.message(2,text='Original histórico');original.peer_id=types.PeerUser(55)
        wrong=fixtures.message(3,text='Otro chat');wrong.peer_id=types.PeerUser(99)
        self.client.get_messages.return_value=[original,wrong]
        items=[{'id':9,'reply':2},{'id':10,'reply':3}]
        await self.panel.features.replies(self.client,self.entity,items)
        self.assertEqual(items[0]['reply_preview']['text'],'Original histórico');self.assertNotIn('reply_preview',items[1])
        self.client.get_messages.assert_awaited_once_with(self.entity,ids=[2,3])
    async def test_context_validates_selected_message_and_loads_surrounding_window(self):
        self.msgs=[fixtures.message(i) for i in range(70,9,-1)]
        data=await self.panel.features.context(111,22,self.peer,40)
        self.assertEqual(data['focus'],40);self.assertEqual(data['next'],11)
        self.assertEqual(self.kwargs[-1],{'offset_id':40,'add_offset':-30,'limit':61})
        self.client.get_messages.return_value.peer_id=types.PeerUser(77)
        with self.assertRaisesRegex(ValueError,'pertenece'):await self.panel.features.context(111,22,self.peer,40)
    async def test_rich_metadata_keeps_utf16_offsets_reactions_and_poll_options(self):
        m=fixtures.message(9,text='😀 negrita')
        m.entities=[types.MessageEntityBold(3,7)]
        m.reactions=N(results=[N(reaction=types.ReactionEmoji('❤️'),count=2,chosen_order=0)])
        poll=types.Poll(0,types.TextWithEntities('¿Vienes?',[]),[types.PollAnswer(types.TextWithEntities('Sí',[]),b'yes')],hash=0)
        m.media=N(poll=poll,results=N(total_voters=1,results=[N(option=b'yes',voters=1,chosen=True)]))
        data=f.extra_message(m);self.assertEqual(data['entities'][0]['offset'],3);self.assertTrue(data['reactions'][0]['chosen']);self.assertEqual(data['poll']['answers'][0]['option'],'eWVz')
    async def test_telegram_reaction_is_explicit_and_deduplicated(self):
        data=self.payload('reaction',message=9,emoji='❤️')
        await self.panel.features.perform(111,data)
        request=self.client.rpc.await_args.args[0];self.assertIsInstance(request,functions.messages.SendReactionRequest);self.assertEqual(request.reaction[0].emoticon,'❤️')
        with self.assertRaisesRegex(ValueError,'ya se procesó'):await self.panel.features.perform(111,data)
        self.assertEqual(self.client.rpc.await_count,1)
    async def test_poll_creation_uses_native_api_and_does_not_allow_empty_answers(self):
        await self.panel.features.perform(111,self.payload('poll',question='¿Vienes?',answers=['Sí','No'],multiple=True))
        request=self.client.rpc.await_args.args[0];self.assertIsInstance(request.media,types.InputMediaPoll);self.assertEqual(request.media.poll.hash,0);self.assertTrue(request.media.poll.multiple_choice)
        with self.assertRaises(ValueError):await self.panel.features.perform(111,self.payload('poll',question='¿Vienes?',answers=['Sí',''],request='b'*32))
    async def test_votes_validate_actual_native_options_and_closed_polls(self):
        m=self.client.get_messages.return_value
        m.media=N(poll=types.Poll(0,types.TextWithEntities('Pregunta',[]),[types.PollAnswer(types.TextWithEntities('Sí',[]),b'y')],hash=0))
        with self.assertRaisesRegex(ValueError,'Opciones'):await self.panel.features.perform(111,self.payload('vote',message=9,options=['bg==']))
        self.client.rpc.assert_not_awaited()
        await self.panel.features.perform(111,self.payload('vote',message=9,options=['eQ==']))
        request=self.client.rpc.await_args.args[0];self.assertEqual(request.options,[b'y'])
        m.media.poll.closed=True
        with self.assertRaisesRegex(ValueError,'admite'):await self.panel.features.perform(111,self.payload('vote',message=9,options=[]))
    async def test_schedule_is_stored_natively_in_telegram_without_a_new_worker(self):
        future=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        await self.panel.actions.execute(111,self.payload(schedule=future))
        self.assertIsInstance(self.client.send_message.await_args.kwargs['schedule'],datetime)
        with self.assertRaisesRegex(ValueError,'futura'):await self.panel.actions.execute(111,self.payload(schedule='2020-01-01T10:00:00+00:00',request='b'*32))
    async def test_markdown_send_passes_entities_without_enabling_html_execution(self):
        await self.panel.actions.execute(111,self.payload(text='**Hola** <script>',format='markdown'))
        args=self.client.send_message.await_args
        self.assertEqual(args.args[1],'Hola <script>');self.assertIsInstance(args.kwargs['formatting_entities'][0],types.MessageEntityBold)
    async def test_scheduled_cancel_validates_current_chat_inventory(self):
        self.client.rpc.return_value=N(messages=[fixtures.message(5,out=True)])
        await self.panel.features.perform(111,self.payload('scheduled-delete',message=5))
        self.assertIsInstance(self.client.rpc.await_args.args[0],functions.messages.DeleteScheduledMessagesRequest)
        self.client.rpc.return_value=N(messages=[])
        with self.assertRaisesRegex(ValueError,'disponible'):await self.panel.features.perform(111,self.payload('scheduled-send',message=5,request='b'*32))
    async def test_library_handles_are_bound_to_actor_account_and_generation(self):
        handle=self.panel.pack('document',actor=111,uid=22,generation=3,id=5,hash=7,ref='1234',kind='stickers')
        doc,kind=self.panel.features.document(111,22,3,handle);self.assertEqual(doc.file_reference,b'\x12\x34')
        for actor,uid,generation in [(222,22,3),(111,33,3),(111,22,4)]:
            with self.assertRaises(ValueError):self.panel.features.document(actor,uid,generation,handle)
    async def test_global_search_applies_policy_before_returning_any_text(self):
        m=fixtures.message(9,text='Permitido');m.peer_id=types.PeerUser(55)
        hidden=fixtures.message(8,text='NO EXPONER');hidden.peer_id=types.PeerUser(77)
        self.client.rpc.return_value=N(messages=[m,hidden],users=[self.entity,types.User(77,first_name='Oculto',access_hash=9)],chats=[])
        async def guard(s,uid,source,**kwargs):
            if source==77:raise ValueError('denied')
        with patch.object(f,'require_source',guard):data=await self.panel.features.search(111,22,'texto')
        self.assertEqual(len(data['items']),1);self.assertNotIn('NO EXPONER',str(data));self.assertNotIn('access_hash',str(data))
    async def test_global_search_cursor_is_query_actor_and_generation_scoped(self):
        msgs=[]
        for i in range(100,40,-1):
            m=fixtures.message(i);m.peer_id=types.PeerUser(55);msgs.append(m)
        self.client.rpc.return_value=N(messages=msgs,users=[self.entity],chats=[],next_rate=27)
        data=await self.panel.features.search(111,22,'texto');self.assertTrue(data['next'])
        await self.panel.features.search(111,22,'texto',data['next'])
        req=self.client.rpc.await_args.args[0];self.assertEqual(req.offset_id,41);self.assertEqual(req.offset_rate,27);self.assertIsInstance(req.offset_peer,types.InputPeerUser)
        with self.assertRaisesRegex(ValueError,'búsqueda'):await self.panel.features.search(111,22,'otra',data['next'])
    async def test_topic_history_uses_native_thread_parameter(self):
        self.client.rpc.return_value=N(dialogs=[])
        await self.panel.messages(111,22,self.peer,topic=12)
        self.assertEqual(self.kwargs[-1]['reply_to'],12)
    async def test_read_receipts_use_current_account_private_dialog_not_assumptions(self):
        items=[{'id':4,'out':True},{'id':8,'out':True},{'id':9,'out':False}]
        self.client.rpc.return_value=N(dialogs=[N(read_outbox_max_id=5)])
        await self.panel.features.receipts(self.client,self.entity,items)
        self.assertEqual(items[0]['delivery'],'read');self.assertEqual(items[1]['delivery'],'sent');self.assertNotIn('delivery',items[2])
    async def test_all_new_endpoints_require_current_admin_and_source_access(self):
        await self.setup_http();self.allowed.clear()
        for op in ['context','global-search','folders','topics','scheduled','library','library-thumb']:
            response=await self.http.get('/panel/api/'+op,params={'account':22,'peer':self.peer,'message':9})
            self.assertEqual(response.status,403)
        self.client.rpc.assert_not_awaited()

    async def test_topic_cursor_uses_last_message_date_and_short_native_pages(self):
        self.group();self.entity.forum=True
        created=datetime(2020,1,1,tzinfo=timezone.utc)
        recent=datetime(2026,10,25,tzinfo=timezone.utc)
        item=types.ForumTopic(12,created,types.PeerChannel(77),'Tema',0,90,0,0,0,0,0,0,types.PeerUser(22),types.PeerNotifySettings())
        message=fixtures.message(90);message.date=recent
        self.client.rpc.return_value=N(topics=[item],messages=[message],count=10,order_by_create_date=False)
        data=await self.panel.features.topics(111,22,self.peer)
        self.assertTrue(data['next'])
        await self.panel.features.topics(111,22,self.peer,data['next'])
        request=self.client.rpc.await_args.args[0]
        self.assertEqual(request.offset_date,recent);self.assertEqual(request.offset_id,90);self.assertEqual(request.offset_topic,12)
        self.allowed.add(112)
        with self.assertRaises(ValueError):await self.panel.features.topics(112,22,self.peer,data['next'])
