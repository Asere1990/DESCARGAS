import asyncio
import base64
import struct
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
from descargas_app.backup import file_id,mount_backup
from descargas_app.progress import Progress
from descargas_app.service import MAX_FILE,MAX_ALBUM

class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_is_bounded_and_best_effort(self):
        service=NS(api=AsyncMock(side_effect=RuntimeError('Offline')),tasks=[])
        p=Progress(service,1,[100,100]).start()
        p.update(0,100,100);p.update(0,50,100,upload=True)
        self.assertIn('37%',p.text())
        self.assertIn('Puedes hacer otras cosas',p.text())
        p.phase='sending';self.assertIn('Enviando',p.text())
        await asyncio.sleep(0);await p.close()
        self.assertIsNone(p.message)
    async def test_bridge_rejects_missing_or_wrong_secret(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient,TestServer
        from unittest.mock import patch
        app=web.Application();repo=NS(backup_claim=AsyncMock())
        mount_backup(app,NS(ready=True,repo=repo,bot_id=123))
        with patch.dict('os.environ',{'DESCARGAS_BACKUP_KEY':'test-only-key'}):
            async with TestClient(TestServer(app)) as client:
                for header in ({},{'Authorization':'Bearer wrong'}):
                    response=await client.post('/internal/backups/claim',json={},headers=header)
                    self.assertEqual(response.status,401)
        repo.backup_claim.assert_not_awaited()

    async def test_bridge_streams_only_leased_stored_media(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient,TestServer
        from unittest.mock import patch
        class Iterator:
            def __init__(self):self.parts=iter([b'abc',b'def']);self.closed=False
            def __aiter__(self):
                # Telethon resets its request iterator on each __aiter__ call.
                self.parts=iter([b'abc',b'def'])
                return self
            async def __anext__(self):
                try:return next(self.parts)
                except StopIteration:raise StopAsyncIteration
            async def close(self):self.closed=True
        iterator=Iterator()
        payload={'items':[{'kind':'document','size':6,'bot_message_id':99,'location':{'id':1,'access_hash':2,'reference':'','thumb':''}}]}
        repo=NS(backup_leased=AsyncMock(return_value={'payload':payload}),decode=lambda x:x)
        def download(source,**kwargs):
            self.assertEqual(source.file_reference,b'fresh-reference')
            return iterator
        bot=NS(iter_download=download,get_messages=AsyncMock(return_value=NS(photo=None,document=NS(id=1,access_hash=2,file_reference=b'fresh-reference'))))
        app=web.Application();mount_backup(app,NS(ready=True,repo=repo,bot_id=123,backup_bot=bot))
        with patch.dict('os.environ',{'DESCARGAS_BACKUP_KEY':'secret'}):
            async with TestClient(TestServer(app)) as client:
                response=await client.post('/internal/backups/file',json={'key':'a'*64,'lease':'b'*32,'index':0},headers={'Authorization':'Bearer secret'})
                self.assertEqual(response.status,200);self.assertEqual(await response.read(),b'abcdef')
                self.assertTrue(iterator.closed)
                repo.backup_leased.return_value=None
                response=await client.post('/internal/backups/file',json={'key':'a'*64,'lease':'c'*32,'index':0},headers={'Authorization':'Bearer secret'})
                self.assertEqual(response.status,409)

class FileIdTests(unittest.TestCase):
    def decode(self,value):
        encoded=base64.urlsafe_b64decode(value+'='*(-len(value)%4));out=bytearray();i=0
        while i<len(encoded):
            if encoded[i]==0:out.extend(b'\0'*encoded[i+1]);i+=2
            else:out.append(encoded[i]);i+=1
        return out
    def test_document_contains_delivering_bot_reference(self):
        reference=b'BOT-OWNED-REFERENCE'
        m=NS(photo=None,document=NS(dc_id=2,id=111,access_hash=222,file_reference=reference,attributes=[]))
        raw=self.decode(file_id(m));kind,dc=struct.unpack('<ii',raw[:8])
        self.assertEqual(kind,5|(1<<25));self.assertEqual(dc,2)
        self.assertIn(reference,raw);self.assertEqual(raw[-2:],bytes([30,4]))
        self.assertEqual(len(raw),8+len(reference)+1+16+2)
    def test_modern_photo_uses_biggest_progressive_size(self):
        m=NS(photo=NS(dc_id=4,id=111,access_hash=222,file_reference=b'ref',sizes=[
            NS(type='s',size=10),NS(type='y',sizes=[50,100])]))
        raw=self.decode(file_id(m))
        self.assertEqual(struct.unpack('<qiiii',raw[-26:-2]),(0,1,2,ord('y'),0))
    def test_limits(self):
        self.assertEqual(MAX_FILE,500*1024**2);self.assertEqual(MAX_ALBUM,1024**3)
