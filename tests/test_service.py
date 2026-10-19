import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from urllib.parse import urlencode
from cryptography.fernet import Fernet
from descargas_app.security import validate_init_data
from descargas_app.service import Service,parse_link,prepare_media_for_bot
from aiohttp.test_utils import TestClient,TestServer
from aiohttp import web
from descargas_app import mount

TOKEN='123:test_token'
def signed(uid=10,age=0):
    values={'user':json.dumps({'id':uid}),'auth_date':str(int(time.time())-age),'query_id':'q'}
    check='\n'.join(f'{k}={v}' for k,v in sorted(values.items()))
    secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    values['hash']=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    return urlencode(values)

class Security(unittest.TestCase):
    def test_valid_and_tampered(self):
        self.assertEqual(validate_init_data(signed(),TOKEN),10)
        for raw in [signed().replace('q','z',1),signed(age=4000),signed()+'&user=x','']:
            with self.assertRaises(Exception):validate_init_data(raw,TOKEN)
    def test_future_and_wrong_token(self):
        for raw,token in [(signed(age=-100),TOKEN),(signed(),'wrong')]:
            with self.assertRaises(Exception):validate_init_data(raw,token)
    def test_link_validation(self):
        self.assertEqual(parse_link('https://t.me/c/123/5'),(-100123,5))
        for link in ['http://t.me/test/1','https://evil.com/c/1/2','https://t.me/+invitation']:
            with self.assertRaises(ValueError):parse_link(link)

class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'TOKEN':TOKEN,'DESCARGAS_SESSION_KEY':Fernet.generate_key().decode(),'DESCARGAS_DATA_DIR':self.temp.name,'TELEGRAM_API_ID':'123','HASH':'a'*32})
        self.env.start();self.s=Service()
    async def asyncTearDown(self):
        await self.s.close();self.env.stop();self.temp.cleanup()
    def save(self,uid,value):
        self.s.db.execute('INSERT INTO users VALUES (?,?)',(uid,self.s.cipher.encrypt(value.encode())));self.s.db.commit()
    async def test_encrypted_session_isolation(self):
        self.save(10,'session-A');self.save(20,'session-B')
        self.assertEqual(self.s.session(10),'session-A');self.assertEqual(self.s.session(20),'session-B');self.assertEqual(self.s.session(30),'')
        raw=(Path(self.temp.name)/'sessions.sqlite3').read_bytes()
        self.assertNotIn(b'session-A',raw)
    async def test_wrong_account_rejected(self):
        client=SimpleNamespace(sign_in=AsyncMock(),get_me=AsyncMock(return_value=SimpleNamespace(id=20)),log_out=AsyncMock(),disconnect=AsyncMock())
        self.s.pending[10]={'client':client,'phone':'+15555555555','hash':'not-a-secret','created':time.monotonic()}
        with self.assertRaises(ValueError):await self.s.verify(10,{'code':'11111'})
        self.assertEqual(self.s.session(10),'');client.log_out.assert_awaited_once()
    async def test_consent_required_before_connect(self):
        with self.assertRaises(ValueError):await self.s.send_code(10,{'phone':'+15555555555'})
        self.assertFalse(self.s.pending)
    async def test_disconnect_only_own_account(self):
        self.save(10,'session-A');self.save(20,'session-B')
        client=SimpleNamespace(connect=AsyncMock(),log_out=AsyncMock(),disconnect=AsyncMock())
        self.s.client=lambda value:client
        await self.s.disconnect(10)
        self.assertEqual(self.s.session(10),'');self.assertEqual(self.s.session(20),'session-B')
        self.assertEqual(self.s.epochs[10],1)
    async def test_nonprivate_messages_ignored(self):
        self.s.tell=AsyncMock()
        await self.s.update({'update_id':1,'message':{'chat':{'type':'group'},'from':{'id':10},'text':'/start'}})
        self.s.tell.assert_not_awaited()
    async def test_contact_cannot_impersonate_other_user(self):
        await self.s.update({'update_id':1,'message':{'chat':{'type':'private'},'from':{'id':10},'contact':{'user_id':20,'phone_number':'123'}}})
        self.assertFalse(self.s.contacts)
    async def test_queue_uses_sender_identity_only(self):
        self.save(10,'session-A');self.s.tell=AsyncMock()
        update={'update_id':7,'message':{'chat':{'type':'private'},'from':{'id':10},'text':'https://t.me/testchannel/1'}}
        await self.s.update(update);await self.s.update(update)
        self.assertEqual(self.s.queue.qsize(),1)
        uid,links,epoch=self.s.queue.get_nowait();self.s.queue.task_done()
        self.assertEqual(uid,10)
    async def test_identity_verified_before_saving(self):
        client=SimpleNamespace(sign_in=AsyncMock(),get_me=AsyncMock(return_value=SimpleNamespace(id=10)),disconnect=AsyncMock(),session=SimpleNamespace(save=lambda:'authorized-A'))
        self.s.pending[10]={'client':client,'phone':'+15555555555','hash':'h','created':time.monotonic()};self.s.tell=AsyncMock()
        result=await self.s.verify(10,{'code':'11111'})
        self.assertTrue(result['connected']);self.assertEqual(self.s.session(10),'authorized-A');self.assertNotIn(10,self.s.pending)
    async def test_api_authorization(self):
        app=web.Application();service=mount(app)
        service.start=AsyncMock();service.ready=True
        client=TestClient(TestServer(app));await client.start_server()
        try:
            r=await client.post('/descargas/api/status',json={});self.assertEqual(r.status,401)
            r=await client.post('/descargas/api/status',json={},headers={'X-Telegram-Init-Data':signed()});self.assertEqual(r.status,200)
            self.assertFalse((await r.json())['connected'])
        finally:await client.close()
