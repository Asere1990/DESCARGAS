import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest
from telethon import types
from descargas_app.media_cache import deliver_cached,archive_once,CURRENT_MEDIA,cache_key


def item(id=1,text='original'):
    return SimpleNamespace(photo=SimpleNamespace(id=id),document=None,file=SimpleNamespace(size=10),
        message=text,entities=[types.MessageEntityBold(offset=0,length=len(text))])


class CacheTests(unittest.IsolatedAsyncioTestCase):
    def setup_service(self):
        lock=asyncio.Lock();rows={}
        @asynccontextmanager
        async def locked(key):
            async with lock:yield
        async def lookup(key):return rows.get(key)
        async def publishing(key):
            if key in rows:raise AssertionError('Duplicate publication')
            rows[key]={'state':'publishing','payload':None}
        async def ready(key,payload):rows[key]={'state':'ready','payload':payload}
        repo=SimpleNamespace(media_lock=locked,media_cache=AsyncMock(side_effect=lookup),
            media_publishing=AsyncMock(side_effect=publishing),media_ready=AsyncMock(side_effect=ready),
            encode=lambda x:x,decode=lambda x:x,pace=AsyncMock())
        service=SimpleNamespace(repo=repo,bot_id=123,almacen=-100123,
            source_items=AsyncMock(return_value=((-100456,50),[item()])),
            before_send=AsyncMock(),api=AsyncMock(return_value=[{'message_id':99}]))
        async def first(uid,client,key,items,seen):
            await asyncio.sleep(0.02)
            await archive_once(service,CURRENT_MEDIA.get(),uid,'First user',[SimpleNamespace(id=10)])
            seen.add(key)
            return 1
        service.deliver_items=AsyncMock(side_effect=first)
        return service,rows

    async def test_parallel_requests_store_once_then_reuse_without_download(self):
        s,rows=self.setup_service()
        self.assertEqual(await asyncio.gather(deliver_cached(s,1,None,'link',set()),
            deliver_cached(s,2,None,'link',set())),[1,1])
        self.assertEqual(s.deliver_items.await_count,1)
        self.assertEqual(s.repo.media_publishing.await_count,1)
        self.assertEqual(s.source_items.await_count,2)
        copies=[c.args[1] for c in s.api.call_args_list if c.args[0]=='copyMessages']
        self.assertEqual([c['chat_id'] for c in copies],[-100123,2])
        self.assertEqual(copies[1]['from_chat_id'],-100123)
        self.assertTrue(copies[1]['remove_caption'])
        captions=[c.args[1] for c in s.api.call_args_list if c.args[0]=='editMessageCaption']
        self.assertEqual(captions[-1]['caption'],'original')
        self.assertNotIn('First user',captions[-1]['caption'])

    async def test_denied_source_never_reads_shared_cache(self):
        s,_=self.setup_service()
        s.source_items.side_effect=ValueError('La cuenta no tiene acceso al contenido completo.')
        with self.assertRaises(ValueError):await deliver_cached(s,2,None,'link',set())
        s.repo.media_cache.assert_not_awaited();s.api.assert_not_awaited()

    async def test_uncertain_archive_is_not_published_again(self):
        s,rows=self.setup_service()
        identity=cache_key(123,s.almacen,(-100456,50),[item()])
        rows[identity]={'state':'publishing','payload':None}
        with self.assertRaises(ValueError):await deliver_cached(s,2,None,'link',set())
        s.deliver_items.assert_not_awaited();s.api.assert_not_awaited()

    async def test_changed_content_gets_new_version(self):
        s,_=self.setup_service()
        self.assertNotEqual(cache_key(123,s.almacen,(-100456,50),[item(1)]),
            cache_key(123,s.almacen,(-100456,50),[item(2)]))

    async def test_failed_archive_keeps_uncertain_marker(self):
        s,rows=self.setup_service();s.api.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):await deliver_cached(s,1,None,'link',set())
        self.assertEqual(next(iter(rows.values()))['state'],'publishing')
