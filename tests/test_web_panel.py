import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timezone
import json
from pathlib import Path
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from cryptography.fernet import Fernet
from telethon import types
from descargas_app import web_panel as p
from descargas_app.creator_access import ACTOR


def message(mid,media=None,text='Hola',out=False):
    return N(id=mid,date=datetime(2026,10,25,tzinfo=timezone.utc),out=out,message=text,media=media,
      photo=None,document=None,ttl_period=None,sender=None,sender_id=55,reply_to=None,grouped_id=None,noforwards=False)

class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings={};self.allowed={111};self.rows=[];self.msgs=[];self.kwargs=[]
        async def setting(k):return self.settings.get(k)
        async def put(k,v):self.settings[k]=v
        self.pool=N(fetch=AsyncMock(return_value=[]))
        self.repo=N(setting=setting,set_setting=put,pool=self.pool,decode=lambda data:json.loads(data),account=AsyncMock(return_value={'generation':3,'session':b'session'}))
        self.s=N(bot_id=7,ready=True,repo=self.repo,cipher=Fernet(Fernet.generate_key()),url='http://test/')
        self.entity=types.User(id=55,access_hash=456,first_name='María',last_name='Prueba')
        async def dialogs(**kwargs):
            self.kwargs.append(kwargs)
            for d in self.rows:yield d
        async def messages(entity,**kwargs):
            self.kwargs.append(kwargs)
            for m in self.msgs:yield m
        self.client=N(iter_dialogs=dialogs,iter_messages=messages,get_me=AsyncMock(return_value=types.User(id=22,first_name='Cuenta propia')),get_entity=AsyncMock(return_value=self.entity),get_messages=AsyncMock(return_value=message(9)),download_media=AsyncMock(return_value=b'JPEG'),download_profile_photo=AsyncMock(return_value=b'AVATAR'))
        @asynccontextmanager
        async def connect(s,uid):yield self.client
        self.guard=AsyncMock();self.account_guard=AsyncMock()
        for target,value in [('authorized',lambda actor:actor in self.allowed),('connected',connect),('require_account',self.account_guard),('require_source',self.guard),('require_entity',lambda *args:None)]:
            patcher=patch.object(p,target,value);patcher.start();self.addCleanup(patcher.stop)
        self.panel=p.TelegramPanel(self.s)
        self.peer=self.panel.pack('peer',actor=111,uid=22,generation=3,**p.peer_data(self.entity,22))
    async def setup_http(self):
        app=web.Application();self.panel=p.mount_panel(app,self.s);self.http=TestClient(TestServer(app));await self.http.start_server();self.addAsyncCleanup(self.http.close)
        code=(await p.access_link(self.s,111)).split('#')[1]
        r=await self.http.post('/panel/api/login',headers={'Origin':'http://test'},json={'token':code});self.assertEqual(r.status,200)
        r=await self.http.get('/panel/api/me');me=await r.json();self.csrf=me['csrf'];return self.http
    async def test_access_is_admin_only_stable_across_restart_and_revocable(self):
        link=await p.access_link(self.s,111);code=link.split('#')[1]
        value=self.panel.unpack(code,'access');self.assertEqual(value['actor'],111);self.assertNotIn('session',value)
        self.assertEqual(p.TelegramPanel(self.s).unpack(code,'access'),value)
        with self.assertRaises(ValueError):await p.access_link(self.s,222)
        with self.assertRaises(ValueError):self.panel.unpack(code[:-2]+'zz','access')
        await self.setup_http();r=await self.http.post('/panel/api/revoke',headers={'Origin':'http://test','X-Panel-CSRF':self.csrf},json={});self.assertEqual(r.status,200)
        r=await self.http.get('/panel/api/accounts');self.assertEqual(r.status,401)
        r=await self.http.post('/panel/api/login',headers={'Origin':'http://test'},json={'token':code});self.assertEqual(r.status,401)
    async def test_origin_csrf_cookie_and_role_are_checked_on_every_request(self):
        await self.setup_http()
        code=(await p.access_link(self.s,111)).split('#')[1]
        r=await self.http.post('/panel/api/login',headers={'Origin':'http://evil'},json={'token':code});self.assertEqual(r.status,403)
        r=await self.http.post('/panel/api/revoke',headers={'Origin':'http://test'},json={});self.assertEqual(r.status,403)
        self.allowed.clear();r=await self.http.get('/panel/api/accounts');self.assertEqual(r.status,403)
        r=await self.http.get('/panel');self.assertEqual(r.status,200);self.assertEqual(r.headers['Cache-Control'],'no-store')
        self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
    async def test_accounts_filter_policy_without_opening_user_sessions(self):
        self.pool.fetch.return_value=[{'uid':22,'profile':json.dumps({'name':'Permitida'})},{'uid':33,'profile':json.dumps({'name':'Oculta'})}]
        async def check(s,uid,actor):
            if uid==33:raise ValueError('denied')
        self.account_guard.side_effect=check
        result=await self.panel.accounts(111)
        self.assertEqual([v['id'] for v in result['items']],['22']);self.assertNotIn('session',str(result))
    async def test_dialogs_include_all_kinds_and_scope_handles_without_access_hash_leaks(self):
        entities=[self.entity,types.Chat(id=100,title='Grupo',photo=types.ChatPhotoEmpty(),participants_count=1,date=datetime.now(timezone.utc),version=1),types.Channel(id=200,title='Canal',photo=types.ChatPhotoEmpty(),date=datetime.now(timezone.utc),broadcast=True,access_hash=789)]
        self.rows=[N(entity=e,id=p.source_id(p.peer_data(e,22)),message=message(5),date=datetime.now(timezone.utc),unread_count=2,pinned=False) for e in entities]
        result=await self.panel.chats(111,22)
        self.assertEqual([r['type'] for r in result['items']],['private','group','channel'])
        self.assertNotIn('access_hash',json.dumps(result));self.assertEqual(ACTOR.get(),None)
        self.guard.assert_any_await(self.s,22,55,client=self.client,entity=self.entity,actor=111)
        with self.assertRaises(ValueError):await self.panel.target(112,22,3,self.client,result['items'][0]['handle'])
        with self.assertRaises(ValueError):await self.panel.target(111,22,4,self.client,result['items'][0]['handle'])
    async def test_messages_paginate_search_and_keep_texts_separate_from_gallery(self):
        self.msgs=[message(i,text='Mensaje '+str(i)) for i in range(100,39,-1)]
        result=await self.panel.messages(111,22,self.peer)
        self.assertEqual(result['items'][0]['id'],41);self.assertEqual(result['items'][-1]['id'],100);self.assertEqual(result['next'],41)
        result=await self.panel.messages(111,22,self.peer,before=41,gallery=True,query='viaje')
        self.assertEqual(result['items'],[]);self.assertEqual(self.kwargs[-1]['offset_id'],41);self.assertEqual(self.kwargs[-1]['search'],'viaje')
        self.assertIsInstance(self.kwargs[-1]['filter'],types.InputMessagesFilterPhotoVideo)
    async def test_source_denial_applies_to_messages_thumbnails_avatars_and_full_files(self):
        self.guard.side_effect=ValueError('Origen protegido')
        await self.setup_http()
        for op in ['messages','gallery','avatar','thumb','media']:
            r=await self.http.get('/panel/api/'+op,params={'account':'22','peer':self.peer,'message':'9'});self.assertEqual(r.status,403)
        self.client.get_messages.assert_not_awaited();self.client.download_media.assert_not_awaited();self.client.download_profile_photo.assert_not_awaited()
    async def test_temporary_media_remains_unavailable(self):
        m=message(9);m.ttl_period=30;self.assertIsNone(p.message_data(m))
        m=message(9,N(ttl_seconds=10));self.assertIsNone(p.media_info(m))
    async def test_byte_ranges_stream_only_requested_bytes_and_close_telegram_iterator(self):
        payload=b'0123456789abcdefghijklmnop'
        m=message(9,N(ttl_seconds=None));m.noforwards=True;m.document=N(size=len(payload),mime_type='video/mp4',attributes=[types.DocumentAttributeVideo(duration=2,w=16,h=16)],thumbs=[]);self.client.get_messages.return_value=m
        class Download:
            def __init__(self):self.closed=False
            def __aiter__(self):return self
            async def __anext__(self):
                if getattr(self,'done',False):raise StopAsyncIteration
                self.done=True;return payload
            async def close(self):self.closed=True
        transfer=Download();self.client.iter_download=lambda *args,**kwargs:transfer
        await self.setup_http();r=await self.http.get('/panel/api/media',params={'account':'22','peer':self.peer,'message':'9','download':'1'},headers={'Range':'bytes=5-12'})
        self.assertIn('attachment',r.headers['Content-Disposition']);self.assertEqual(r.status,206);self.assertEqual(r.headers['Content-Range'],'bytes 5-12/26');self.assertEqual(await r.read(),payload[5:13]);self.assertTrue(transfer.closed)
    async def test_changed_privacy_during_thumbnail_download_prevents_returning_bytes(self):
        m=message(9,N(ttl_seconds=None));m.photo=N(sizes=[]);self.client.get_messages.return_value=m
        async def download(*args,**kwargs):self.allowed.clear();return b'PRIVATE'
        self.client.download_media.side_effect=download
        await self.setup_http();r=await self.http.get('/panel/api/thumb',params={'account':'22','peer':self.peer,'message':'9'});self.assertEqual(r.status,403);self.assertNotIn(b'PRIVATE',await r.read())

    async def test_account_names_use_directory_and_current_telegram_profile_without_user_sessions(self):
        self.pool.fetch.return_value=[{'uid':22,'profile':json.dumps({'phone':'12025550123'}),'name':json.dumps({'name':'Nombre del directorio'})}]
        result=await self.panel.accounts(111)
        self.assertEqual(result['items'][0]['name'],'Nombre del directorio')
        self.pool.fetchrow=AsyncMock(return_value=self.pool.fetch.return_value[0])
        self.s.api=AsyncMock(return_value={'id':22,'first_name':'Ana','last_name':'García','username':'ana_demo','photo':{'small_file_id':'private-photo-id'}})
        current=await self.panel.account_profile(111,22)
        self.assertEqual(current['name'],'Ana García');self.assertEqual(current['username'],'ana_demo')
        again=await self.panel.account_profile(111,22);self.assertEqual(current,again)
        self.s.api.assert_awaited_once_with('getChat',{'chat_id':22});self.client.get_me.assert_not_awaited()
        self.assertEqual(p.account_label({'name':'5122070504'}),'Cuenta de Telegram')
        self.assertEqual(p.account_label({'name':'Nombre no disponible'},{'name':'Nombre conocido'}),'Nombre conocido')
        self.assertEqual(p.account_label({'username':'equipo'}),'@equipo')
    async def test_profile_endpoint_never_exposes_photo_file_ids_and_rechecks_access_for_cached_photos(self):
        self.pool.fetchrow=AsyncMock(return_value={'profile':None,'name':None})
        self.s.api=AsyncMock(return_value={'id':22,'first_name':'Ana','photo':{'small_file_id':'private-photo-id'}})
        await self.setup_http()
        r=await self.http.get('/panel/api/account-profile',params={'account':'22'});data=await r.json()
        self.assertEqual(data,{'name':'Ana','username':'','avatar':True});self.assertNotIn('private-photo-id',json.dumps(data))
        self.panel.profiles[(111,22)]['image']=b'cached-avatar'
        r=await self.http.get('/panel/api/account-avatar',params={'account':'22'});self.assertEqual(r.status,200);self.assertEqual(await r.read(),b'cached-avatar')
        self.allowed.clear();r=await self.http.get('/panel/api/account-avatar',params={'account':'22'});self.assertEqual(r.status,403)
    async def test_avatar_is_small_jpeg_and_bot_download_token_stays_server_side(self):
        import io
        from PIL import Image
        image=Image.new('RGB',(640,480),'blue');raw=io.BytesIO();image.save(raw,format='JPEG')
        class Content:
            async def iter_chunked(self,size):yield raw.getvalue()
        class Response:
            status=200;content=Content()
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        self.pool.fetchrow=AsyncMock(return_value={'profile':None,'name':None})
        self.s.token='fixture-token'
        self.s.api=AsyncMock(side_effect=[{'id':22,'first_name':'Ana','photo':{'small_file_id':'photo-id'}},{'file_path':'photos/file_1.jpg'}])
        class Http:
            def get(self,*args,**kwargs):return Response()
        self.s.http=Http()
        response=await self.panel.account_avatar(111,22)
        self.assertEqual(response.content_type,'image/jpeg')
        with Image.open(io.BytesIO(response.body)) as thumbnail:self.assertEqual(thumbnail.size,(96,72))
        self.assertNotIn(b'fixture-token',response.body);self.assertEqual(self.s.api.await_count,2)
        self.client.get_me.assert_not_awaited()
    async def test_unavailable_profile_keeps_the_known_name_and_initials_fallback(self):
        self.pool.fetchrow=AsyncMock(return_value={'profile':None,'name':json.dumps({'name':'Nombre conocido'})})
        self.s.api=AsyncMock(side_effect=RuntimeError('Telegram unavailable'))
        profile=await self.panel.account_profile(111,22)
        self.assertEqual(profile['name'],'Nombre conocido');self.assertIsNone(profile['file'])
        response=await self.panel.account_avatar(111,22);self.assertEqual(response.status,204)

    async def test_protected_group_photos_are_visible_in_chat_and_gallery(self):
        self.entity.noforwards=True
        m=message(9,N(ttl_seconds=None),text='Foto visible')
        m.photo=N(sizes=[N(size=32)])
        self.msgs=[m]
        for gallery in [False,True]:
            result=await self.panel.messages(111,22,self.peer,gallery=gallery)
            self.assertEqual(len(result['items']),1)
            self.assertEqual(result['items'][0]['media']['type'],'photo')
            self.assertTrue(result['items'][0]['protected'])
            self.assertEqual(result['items'][0]['text'],'Foto visible')
        self.client.get_messages.return_value=m
        await self.setup_http()
        r=await self.http.get('/panel/api/thumb',params={'account':'22','peer':self.peer,'message':'9'})
        self.assertEqual(r.status,200);self.assertEqual(await r.read(),b'JPEG')
        self.assertEqual(r.headers['Cache-Control'],'no-store')
        self.client.download_media.assert_awaited_once_with(m,file=bytes,thumb='m')
        class Transfer:
            def __aiter__(self):return self
            async def __anext__(self):
                if getattr(self,'done',False):raise StopAsyncIteration
                self.done=True;return b'x'*32
            async def close(self):pass
        self.client.iter_download=lambda *args,**kwargs:Transfer()
        r=await self.http.get('/panel/api/media',params={'account':'22','peer':self.peer,'message':'9','download':'1'})
        self.assertEqual(r.status,200);self.assertEqual(await r.read(),b'x'*32)
        self.assertIn('attachment',r.headers['Content-Disposition'])
    async def test_standalone_protected_message_preserves_media_for_viewing(self):
        m=message(8,N(ttl_seconds=None));m.noforwards=True
        m.document=N(size=100,mime_type='video/mp4',attributes=[types.DocumentAttributeVideo(duration=2,w=16,h=16)])
        result=p.message_data(m)
        self.assertTrue(result['protected']);self.assertEqual(result['media']['type'],'video')
        self.assertFalse(p.message_data(message(7))['protected'])

    async def test_safari_suffix_ranges_and_quicktime_video_are_streamed_inline(self):
        payload=b'0123456789abcdefghijklmnop'
        m=message(9,N(ttl_seconds=None));m.document=N(size=len(payload),mime_type='video/quicktime',attributes=[types.DocumentAttributeVideo(duration=2,w=16,h=16)],thumbs=[])
        self.client.get_messages.return_value=m
        transfers=[]
        class Download:
            def __init__(self):self.done=False;self.closed=False
            def __aiter__(self):return self
            async def __anext__(self):
                if self.done:raise StopAsyncIteration
                self.done=True;return payload
            async def close(self):self.closed=True
        def stream(*args,**kwargs):
            transfer=Download();transfers.append(transfer);return transfer
        self.client.iter_download=stream
        await self.setup_http()
        for value,expected in [('bytes=0-1',payload[:2]),('bytes=-6',payload[-6:]),('bytes=20-',payload[20:])]:
            r=await self.http.get('/panel/api/media',params={'account':'22','peer':self.peer,'message':'9'},headers={'Range':value})
            self.assertEqual(r.status,206);self.assertEqual(await r.read(),expected)
            self.assertEqual(r.headers['Content-Type'],'video/quicktime');self.assertTrue(r.headers['Content-Disposition'].startswith('inline'))
        self.assertTrue(all(t.closed for t in transfers))
        for value in ['bytes=-0','bytes=999-','bytes=-','bytes=1-2,4-5']:
            r=await self.http.get('/panel/api/media',params={'account':'22','peer':self.peer,'message':'9'},headers={'Range':value})
            self.assertEqual(r.status,416)
