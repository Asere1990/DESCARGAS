import io
import unittest
import zipfile
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from telethon import types
from tests import test_web_panel as fixtures


class BatchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.PanelTests.asyncSetUp
    setup_http=fixtures.PanelTests.setup_http
    async def prepare(self,protected=False):
        self.payloads={9:b'photo-bytes',10:b'video-bytes'};self.closed=[];self.started=[]
        rows={}
        for mid,body in self.payloads.items():
            m=fixtures.message(mid,N(ttl_seconds=None));m.peer_id=types.PeerUser(55)
            if mid==9:m.photo=N(sizes=[N(size=len(body),w=100,h=100)])
            else:m.document=N(size=len(body),mime_type='video/mp4',attributes=[types.DocumentAttributeVideo(duration=1,w=16,h=16),types.DocumentAttributeFilename('../../video.mp4')])
            m.media.mid=mid;m.noforwards=protected;rows[mid]=m
        async def get_messages(entity,ids):return [rows[i] for i in ids] if isinstance(ids,list) else rows[ids]
        self.client.get_messages=AsyncMock(side_effect=get_messages)
        owner=self
        class Transfer:
            def __init__(self,media):self.mid=media.mid;self.done=False
            def __aiter__(self):return self
            async def __anext__(self):
                if self.done:raise StopAsyncIteration
                self.done=True;return owner.payloads[self.mid]
            async def close(self):owner.closed.append(self.mid)
        def transfer(media,**kwargs):self.started.append(media.mid);return Transfer(media)
        self.client.iter_download=transfer
        await self.setup_http()
    async def send(self,**changes):
        data={'account':'22','peer':self.peer,'messages':'9,10,9','csrf':self.csrf};data.update(changes)
        return await self.http.post('/panel/api/batch-download',data=data,headers={'Origin':'http://test'})
    async def test_streamed_zip_contains_exact_selected_media_once_and_closes_transfers(self):
        await self.prepare();r=await self.send();self.assertEqual(r.status,200)
        self.assertIn('attachment',r.headers['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(await r.read())) as archive:
            self.assertEqual(len(archive.namelist()),2)
            self.assertEqual([archive.read(n) for n in archive.namelist()],list(self.payloads.values()))
            self.assertTrue(all('/' not in n for n in archive.namelist()))
        self.assertEqual(self.closed,[9,10]);self.assertEqual(self.started,[9,10])
        self.client.download_media.assert_not_awaited()
    async def test_noforwards_media_downloads_with_existing_account_and_source_guards(self):
        await self.prepare(protected=True);r=await self.send();self.assertEqual(r.status,200)
        with zipfile.ZipFile(io.BytesIO(await r.read())) as archive:
            self.assertEqual(len(archive.namelist()),2)
        self.assertEqual(self.started,[9,10]);self.guard.assert_awaited()
    async def test_reviewer_limit_is_enforced_on_server_before_any_download(self):
        await self.prepare()
        with patch('descargas_app.creator_access.reviewer',return_value=True):
            r=await self.send(messages=','.join(str(i) for i in range(1,12)))
        self.assertEqual(r.status,409);self.assertEqual(self.started,[])
        self.assertIn('10',await r.text())
    async def test_batch_requires_csrf_origin_and_current_source_access(self):
        await self.prepare();r=await self.send(csrf='wrong');self.assertEqual(r.status,403)
        self.guard.side_effect=ValueError('Acceso retirado')
        r=await self.send();self.assertNotEqual(r.status,200);self.assertEqual(self.started,[])
    async def test_wrong_chat_message_cannot_be_extracted_in_a_batch(self):
        await self.prepare();m=await self.client.get_messages(self.entity,ids=10);m.peer_id=types.PeerUser(99)
        r=await self.send();self.assertEqual(r.status,409);self.assertEqual(self.started,[])
    async def test_revoked_access_aborts_archive_before_downloading_the_next_file(self):
        from aiohttp import ClientPayloadError
        await self.prepare();calls=0
        async def guard(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls>=4:raise ValueError('Acceso retirado')
        self.guard.side_effect=guard
        r=await self.send()
        with self.assertRaises(ClientPayloadError):await r.read()
        self.assertEqual(self.started,[9]);self.assertEqual(self.closed,[9])
