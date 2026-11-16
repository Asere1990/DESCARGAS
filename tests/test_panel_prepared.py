import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from telethon import types
from descargas_app import panel_prepared as prepared
from descargas_app import panel_purchases


class DownloadIterator:
    def __init__(self, payload):
        self.payload = payload
        self.done = False
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.done:
            raise StopAsyncIteration
        self.done = True
        return self.payload

    async def close(self):
        self.closed = True


class PreparedDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.content = b'1234567890' * 100
        document = types.Document(
            id=55, access_hash=80, file_reference=b'x',
            date=datetime.now(timezone.utc), mime_type='video/mp4',
            size=len(self.content), thumbs=[], dc_id=2,
            attributes=[types.DocumentAttributeVideo(duration=60, w=48, h=32)])
        self.message = N(media=types.MessageMediaDocument(document=document),
                         document=document, photo=None, ttl_period=None)
        self.iterators = []

        def new_iterator(*args, **kwargs):
            result = DownloadIterator(self.content)
            self.iterators.append(result)
            return result

        self.client = N(flood_sleep_threshold=0, iter_download=new_iterator)
        self.source = types.User(id=55, first_name='Canal', access_hash=77)
        self.repo = N(
            account=AsyncMock(return_value={'generation':3,'session':b'encrypted-session'}),
            setting=AsyncMock(return_value='0'))
        self.s = N(repo=self.repo, bot_id=7)

        def unpack(token, kind):
            if kind == 'peer':
                if token != 'peer-token':
                    raise ValueError('Invalid peer')
                return {'kind':'user', 'id':55, 'hash':77,
                        'actor':111, 'uid':22, 'generation':3}
            if kind == 'purchase' and token == 'paid-receipt':
                return {'purpose':'purchase'}
            raise ValueError('Invalid receipt')

        self.panel = N(s=self.s, unpack=unpack,
                       target=AsyncMock(return_value=self.source))
        self.cache = prepared.PreparedDownloads(self.panel)

        @asynccontextmanager
        async def parallel(service, uid):
            self.assertIs(service, self.s)
            self.assertEqual(uid,22)
            yield self.client

        self.guard = AsyncMock()
        self.source_guard = AsyncMock()
        self.resolver = AsyncMock(return_value=self.message)
        patches = [
            patch.object(prepared, 'connected_parallel', parallel),
            patch.object(prepared, 'authorized', return_value=True),
            patch.object(prepared, 'require_account', self.guard),
            patch.object(prepared, 'require_source', self.source_guard),
            patch.object(panel_purchases, 'resolve', self.resolver),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        await self.cache.start()
        self.addAsyncCleanup(self.cache.close)

    def input(self):
        return dict(account=22,peer='peer-token',
                    purchase='paid-receipt',index=0)

    async def finished(self):
        result = await self.cache.begin(111,'0',self.input())
        await self.cache.jobs[result['job']]['task']
        return result['job']

    async def test_priority_staging_progress_and_private_download_file(self):
        id = await self.finished()
        result = await self.cache.status(111,'0',id)
        self.assertEqual(result['state'],'ready')
        self.assertEqual(result['progress'],100)
        self.assertEqual(result['received'],len(self.content))
        self.assertEqual(result['name'],'telegram-0.mp4')
        file = self.cache.jobs[id]['file']
        self.assertEqual(Path(file).read_bytes(),self.content)
        self.assertTrue(self.iterators[0].closed)
        response = await self.cache.serve(N(query={'token':result['file_token']}))
        self.assertIsInstance(response,web.FileResponse)
        self.assertIn('attachment',response.headers['Content-Disposition'])
        self.assertIn('telegram-0.mp4',response.headers['Content-Disposition'])
        self.assertEqual(response.headers['Access-Control-Allow-Origin'],'https://web.telegram.org')
        self.resolver.assert_awaited_once()
        self.source_guard.assert_awaited()
        await self.cache.close()
        self.assertFalse(Path(file).exists())

    async def test_other_actor_cannot_poll_or_retrieve_without_token(self):
        id = await self.finished()
        result = await self.cache.status(111,'0',id)
        with self.assertRaises(ValueError):
            await self.cache.status(222,'0',id)
        with self.assertRaises(web.HTTPNotFound):
            await self.cache.serve(N(query={'token':'not-the-real-token'}))
        with self.assertRaises(web.HTTPNotFound):
            await self.cache.serve(N(query={}))
        self.assertTrue(result['file_token'])

    async def test_detaching_account_revokes_download_without_mutating_session(self):
        id = await self.finished()
        result = await self.cache.status(111,'0',id)
        self.repo.account.return_value={'generation':4,'session':b'encrypted-session'}
        with self.assertRaises(ValueError):
            await self.cache.status(111,'0',id)
        with self.assertRaises(web.HTTPNotFound):
            await self.cache.serve(N(query={'token':result['file_token']}))

    async def test_same_file_reuses_one_job_and_another_account_job_is_rejected(self):
        one = await self.cache.begin(111,'0',self.input())
        same = await self.cache.begin(111,'0',self.input())
        self.assertEqual(one['job'],same['job'])
        other = self.input()
        other['index']=1
        with self.assertRaisesRegex(ValueError,'Ya hay un archivo'):
            await self.cache.begin(111,'0',other)
        await self.cache.jobs[one['job']]['task']
        self.assertEqual(self.resolver.await_count,1)

    async def test_not_purchased_never_creates_downloadable_file(self):
        self.resolver.side_effect=ValueError('Compra no disponible para esta cuenta.')
        id = await self.finished()
        result = await self.cache.status(111,'0',id)
        self.assertEqual(result['state'],'error')
        self.assertNotIn('file_token',result)
        self.assertEqual(self.cache.jobs[id]['file'],None)

    def test_video_filename_fallback_and_sanitization(self):
        self.assertEqual(prepared.safe_filename(
            {'name':'archivo','mime':'video/mp4'},5),'telegram-5.mp4')
        self.assertEqual(prepared.safe_filename(
            {'name':'video maravilloso','mime':'video/quicktime'},7),
            'video_maravilloso.mov')


if __name__ == '__main__':
    unittest.main()
