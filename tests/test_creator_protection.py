import asyncio,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from telethon import types
from descargas_app import creator_access as a,creator_search as q

class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        (self.root/'cuentas.txt').write_text('12\n');(self.root/'police.txt').write_text('99\n')
        p=patch.object(a,'ROOT',self.root);p.start();self.addCleanup(p.stop)
        self.s=N(bot_id=7,repo=N(creator_registration=AsyncMock(return_value=None),creator_ids=AsyncMock(return_value=[]),pool=N(fetchval=AsyncMock(return_value=False),fetch=AsyncMock(return_value=[]),execute=AsyncMock(),fetchrow=AsyncMock(return_value=None))))
    async def test_files_and_police_changes_apply_immediately(self):
        with self.assertRaises(ValueError):await a.require_account(self.s,12,88)
        await a.require_account(self.s,12,99)
        (self.root/'police.txt').write_text('')
        with self.assertRaises(ValueError):await a.require_account(self.s,12,99)
    async def test_missing_or_malformed_files_fail_closed(self):
        (self.root/'cuentas.txt').write_text('invalid')
        with self.assertRaises(ValueError):await a.require_account(self.s,12,88)
        (self.root/'police.txt').unlink()
        with self.assertRaises(ValueError):await a.require_account(self.s,12,99)
    async def test_registered_creator_hidden_from_regular_admin(self):
        self.s.repo.creator_registration.return_value=dict(version=a.VERSION,revoked=False,generation=4,current_generation=4)
        with self.assertRaises(ValueError):await a.require_account(self.s,123,88)
        await a.require_account(self.s,123,99)
    async def test_revocation_and_session_replacement_block_police(self):
        for revoked,generation in [(True,4),(False,5)]:
            self.s.repo.creator_registration.return_value=dict(version=a.VERSION,revoked=revoked,generation=4,current_generation=generation)
            with self.assertRaisesRegex(ValueError,'revocada'):await a.require_account(self.s,123,99)
    async def test_normal_user_downloads_are_untouched(self):
        await a.require_account(self.s,12)
        self.s.repo.creator_registration.assert_not_awaited()
    async def test_protected_owner_is_detected_before_reading_messages(self):
        owner=N(id=12,participant=types.ChannelParticipantCreator(user_id=12,admin_rights=types.ChatAdminRights()))
        client=N(get_participants=AsyncMock(return_value=[owner]))
        with self.assertRaisesRegex(ValueError,'restringido'):await a.require_source(self.s,34,-100123,client=client,entity=N(creator=False),actor=88)
        self.s.repo.pool.execute.assert_awaited_once()
    async def test_unknown_owner_fails_closed_and_known_former_owner_stays_protected(self):
        client=N(get_participants=AsyncMock(side_effect=RuntimeError()))
        with self.assertRaisesRegex(ValueError,'verificar'):await a.require_source(self.s,34,-100123,client=client,entity=N(creator=False),actor=88)
        self.s.repo.pool.fetchval.return_value=1
        with self.assertRaises(ValueError):await a.require_source(self.s,34,-100123,actor=88)
    async def test_actor_scope_is_task_local_and_reset(self):
        @a.scoped('actor')
        async def action(s,uid):
            await asyncio.sleep(0);return a.ACTOR.get()
        self.assertEqual(await asyncio.gather(action(self.s,99),action(self.s,88)),[99,88]);self.assertIsNone(a.ACTOR.get())
    async def test_police_search_requires_explicit_consent(self):
        with self.assertRaisesRegex(ValueError,'autorizar'):await a.require_consent(self.s,99,123)
        self.s.repo.pool.fetchrow.return_value=dict(version=a.VERSION,revoked=False,generation=4,current_generation=4,connected=True)
        await a.require_consent(self.s,99,123)

class LinkTests(unittest.TestCase):
    def item(self,mid,text='',reply=None,photo=True):
        return N(id=mid,message=text,reply_to=N(reply_to_msg_id=reply,reply_to_peer_id=None) if reply else None,photo=N(id=mid) if photo else None,document=None,sticker=False,gif=False,grouped_id=None)
    def test_caption_and_both_reply_directions_preserve_exact_text(self):
        media=self.item(1,'La Habana ✨')
        self.assertEqual(q.related(media,None,'habana',-100123),(media,['La Habana ✨']))
        text=self.item(2,'Miami coincidencia',1,False)
        self.assertEqual(q.related(text,media,'miami',-100123),(media,['Miami coincidencia']))
        media=self.item(3,'Otro caption',2)
        self.assertEqual(q.related(media,text,'miami',-100123),(media,['Miami coincidencia']))
    def test_unrelated_text_and_cross_chat_reply_are_omitted(self):
        text=self.item(3,'Miami',photo=False)
        self.assertEqual(q.related(text,None,'miami',-100123),(None,[]))
        media=self.item(4,'',3);media.reply_to.reply_to_peer_id=types.PeerChannel(999)
        self.assertEqual(q.related(media,text,'miami',-100123),(None,[]))
    def test_non_multimedia_files_are_omitted(self):
        item=self.item(1,'miami',photo=False);item.document=N(mime_type='application/pdf')
        self.assertEqual(q.related(item,None,'miami',-100123),(None,[]))
    def test_unicode_text_split_is_lossless_and_within_telegram_limits(self):
        text='📷'*6000+'Texto completo';parts=list(q.chunks(text))
        self.assertEqual(''.join(parts),text);self.assertTrue(all(len(p.encode('utf-16-le'))//2<=2800 for p in parts))

class SearchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import json,time
        from contextlib import asynccontextmanager
        self.settings={}
        async def put(k,v):self.settings[k]=v
        async def get(k):return self.settings.get(k)
        self.s=N(bot_id=7,almacen=-1009,repo=N(setting=get,set_setting=put,encode=lambda d:json.dumps(d).encode(),decode=lambda d:json.loads(d),account=AsyncMock(return_value={'session':b'x','generation':4}),pace=AsyncMock(),pool=N(fetchval=AsyncMock(return_value=1),fetchrow=AsyncMock())),tell=AsyncMock(),api=AsyncMock(return_value={'message_id':1}))
        self.media=N(id=1,message='Miami caption',reply_to=None,photo=N(id=10),document=None,sticker=False,gif=False,grouped_id=None)
        self.reply=N(id=2,message='Miami respuesta',reply_to=N(reply_to_msg_id=1,reply_to_peer_id=None),photo=None,document=None,sticker=False,gif=False,grouped_id=None)
        self.client=N(get_messages=AsyncMock(return_value=[self.media]))
        async def dialogs():
            yield N(entity=types.User(id=123,first_name='Prueba',access_hash=42))
        async def messages(*args,**kw):
            for item in (self.reply,self.media):yield item
        self.client.iter_dialogs=dialogs;self.client.iter_messages=messages
        @asynccontextmanager
        async def connect(*args):yield self.client
        for target,value in [('descargas_app.creator_search.connected',connect),('descargas_app.creator_search.require_consent',AsyncMock()),('descargas_app.creator_search.require_source',AsyncMock()),('descargas_app.group_download.users_busy',AsyncMock(return_value=False)),('descargas_app.manual_board.cooldown',AsyncMock()),('descargas_app.service.album',AsyncMock(return_value=[self.media]))]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        self.state={'uid':123,'generation':4,'query':'miami','token':'token','expires':time.time()+1800,'dialogs':None,'dialog':0,'offset':0,'results':[],'seen':[],'scanned':0}
        await put(q.state_key(self.s,99),'token')
        await put(q.state_key(self.s,99,'token'),self.s.repo.encode(self.state).hex())
    async def test_scan_deduplicates_album_and_keeps_both_matching_texts(self):
        await q.scan(self.s,99,'token')
        state=await q.load(self.s,99,'token')
        self.assertEqual(len(state['results']),1)
        self.assertEqual(set(state['results'][0]['texts']),{'Miami caption','Miami respuesta'})
        self.assertIn('global:d:token:0',str(self.s.api.call_args_list))
    async def test_stale_button_does_not_read_messages(self):
        with self.assertRaisesRegex(ValueError,'venció'):await q.scan(self.s,99,'old-token')
        self.client.get_messages.assert_not_awaited()
    async def test_revocation_prevents_search_and_download_text_publication(self):
        with patch.object(q,'require_consent',AsyncMock(side_effect=ValueError('revocada'))):
            with self.assertRaises(ValueError):await q.scan(self.s,99,'token')
        self.s.api.assert_not_awaited()
    async def test_evidence_keeps_caption_untouched_and_does_not_repeat_reserved_send(self):
        self.s.repo.pool.fetchrow.return_value={'payload':self.s.repo.encode({'search_consent':True,'query':'Miami','entries':[{'ids':[1],'matched_texts':['Miami respuesta']} ]})}
        self.s.repo.pool.fetchval.side_effect=[1,None]
        job={'manual_token':'board','uid':123,'actor':99,'source':123}
        await q.evidence(self.s,job,[self.media],[50]);await q.evidence(self.s,job,[self.media],[50])
        self.s.api.assert_awaited_once()
        method,payload=self.s.api.call_args.args
        self.assertEqual(method,'sendMessage');self.assertIn('Miami respuesta',payload['text']);self.assertEqual(payload['reply_parameters']['message_id'],50)
        self.assertEqual(self.media.message,'Miami caption')

class CreatorApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient,TestServer
        from collections import defaultdict
        from contextlib import asynccontextmanager
        from descargas_app import mount
        @asynccontextmanager
        async def lock(*args,**kwargs):yield
        self.pool=N(execute=AsyncMock(),fetchrow=AsyncMock(return_value=dict(version=a.VERSION,revoked=False,generation=4,current_generation=4)))
        self.s=N(token='test-token',ready=True,bot_id=7,locks=defaultdict(asyncio.Lock),start=AsyncMock(),close=AsyncMock(),throttle=lambda uid:None,repo=N(pool=self.pool,account_lock=lock,account=AsyncMock(return_value={'session':b'session','generation':4}),touch_activity=AsyncMock()))
        p=patch('descargas_app.validate_init_data',return_value=123);p.start();self.addCleanup(p.stop)
        app=web.Application();mount(app,prefix='',service_factory=lambda lock:self.s)
        self.client=TestClient(TestServer(app));await self.client.start_server();self.addAsyncCleanup(self.client.close)
    async def test_authorize_requires_version_and_explicit_checkbox(self):
        for data in [{},{'consent':True,'version':'old'},{'consent':'yes','version':a.VERSION}]:
            response=await self.client.post('/api/creator-authorize',json=data)
            self.assertEqual(response.status,400)
        self.pool.execute.assert_not_awaited();self.s.repo.account.assert_not_awaited()
    async def test_signed_identity_cannot_be_replaced_with_body_uid(self):
        response=await self.client.post('/api/creator-authorize',json={'consent':True,'version':a.VERSION,'uid':999})
        self.assertEqual(response.status,200)
        self.s.repo.account.assert_awaited_once_with(123)
        sql,bot,uid,generation,*_=self.pool.execute.call_args.args
        self.assertEqual((bot,uid,generation),(7,123,4));self.assertIn('Solicitud registrada',(await response.json())['message'])
    async def test_revocation_is_for_authenticated_creator_only(self):
        self.pool.fetchrow.return_value=dict(version=a.VERSION,revoked=True,generation=4,current_generation=4)
        response=await self.client.post('/api/creator-revoke',json={'uid':999})
        self.assertEqual(response.status,200);self.assertFalse((await response.json())['authorized'])
        self.assertEqual(self.pool.execute.call_args.args[1:],(7,123))
    async def test_invalid_telegram_authentication_cannot_register(self):
        with patch('descargas_app.validate_init_data',side_effect=ValueError()):
            response=await self.client.post('/api/creator-authorize',json={'consent':True,'version':a.VERSION})
        self.assertEqual(response.status,401);self.pool.execute.assert_not_awaited()
