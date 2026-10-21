import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from telethon import types
from descargas_app import file_cache as f
from descargas_app.media_cache import deliver_cached,cache_key,CACHE_ONLY
import test_media_cache as helpers
from test_media_cache import item


def uploaded(mid=50):
    return NS(id=mid,photo=NS(id=mid+1000,access_hash=222,dc_id=2,file_reference=b'bot-owned',sizes=[NS(type='y',size=10)]),document=None)


class FileCacheTests(unittest.IsolatedAsyncioTestCase):
    def setup(self):
        return helpers.CacheTests().setup_service()

    def test_identity_survives_almacen_changes_but_not_bot_or_media_changes(self):
        key=(-100456,50)
        self.assertNotEqual(cache_key(123,-1001,key,[item()]),cache_key(123,-1002,key,[item()]))
        self.assertNotEqual(f.identity(123,key,[item()]),f.identity(124,key,[item()]))
        self.assertNotEqual(f.identity(123,key,[item()]),f.identity(123,key,[item(2)]))

    async def test_fast_delivery_uses_ids_without_almacen_or_redownload(self):
        s,_=self.setup();key,items=await s.source_items(None,'link');s.source_items.reset_mock()
        saved=f.payload(s,key,items,[uploaded()])
        s.repo.private_cache=AsyncMock(return_value=saved)
        s.api.return_value={'message_id':99}
        s.bot=NS(get_messages=AsyncMock(side_effect=RuntimeError('ALMACEN deleted')))
        s.almacen=-100999999 # Even a replacement storage group must use the same saved IDs.
        marker=CACHE_ONLY.set(True)
        try:self.assertEqual(await deliver_cached(s,2,None,'link',set()),1)
        finally:CACHE_ONLY.reset(marker)
        s.deliver_items.assert_not_awaited();s.bot.get_messages.assert_not_awaited()
        s.repo.media_cache.assert_not_awaited();s.before_send.assert_awaited_once()
        method,data=s.api.call_args.args
        self.assertEqual(method,'sendPhoto');self.assertEqual(data['photo'],saved['items'][0]['file_id'])
        self.assertEqual(data['caption'],'original')
        self.assertEqual(data['caption_entities'],[{'type':'bold','offset':0,'length':8}])
        s.repo.remember_media_route.assert_awaited_once_with(123,'link',saved['key'])

    async def test_mixed_album_order_and_captions_are_preserved(self):
        s,_=self.setup();key=(-100456,50)
        originals=[item(1,'First'),item(2,'Second')]
        originals[1].photo=None;originals[1].document=NS(id=2,attributes=[types.DocumentAttributeVideo(duration=1,w=100,h=100)])
        delivered=[uploaded(50),NS(id=51,photo=None,document=NS(id=2000,dc_id=2,access_hash=333,file_reference=b'bot',attributes=[types.DocumentAttributeVideo(duration=1,w=100,h=100)]))]
        saved=f.payload(s,key,originals,delivered)
        s.source_items.return_value=(key,originals);s.repo.private_cache=AsyncMock(return_value=saved)
        s.api.return_value=[{'message_id':100},{'message_id':101}]
        self.assertEqual(await deliver_cached(s,2,None,'link',set()),2)
        method,data=s.api.call_args.args
        self.assertEqual(method,'sendMediaGroup')
        self.assertEqual([entry['type'] for entry in data['media']],['photo','video'])
        self.assertEqual([entry['caption'] for entry in data['media']],['First','Second'])
        self.assertEqual([entry['media'] for entry in data['media']],[entry['file_id'] for entry in saved['items']])

    async def test_uncertain_file_id_send_is_never_retried_or_downloaded_again(self):
        s,_=self.setup();key,items=await s.source_items(None,'link')
        s.repo.private_cache=AsyncMock(return_value=f.payload(s,key,items,[uploaded()]))
        s.api.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):await deliver_cached(s,2,None,'link',set())
        s.api.assert_awaited_once();s.deliver_items.assert_not_awaited()
        s.repo.media_cache.assert_not_awaited()

    async def test_legacy_backup_can_be_promoted_after_storage_is_deleted(self):
        s,_=self.setup();key,items=await s.source_items(None,'link')
        s.repo.cached_file_payload.return_value={'bot_id':123,'items':[{'file_id':'old-bot-photo'}]}
        saved=await f.find(s,key,items,'old-cache-key')
        self.assertEqual(saved['items'][0]['file_id'],'old-bot-photo')
        self.assertEqual(saved['items'][0]['caption'],'original')
        s.repo.private_ready.assert_awaited_once_with(saved['key'],123,saved)

    async def test_foreign_bot_ids_are_not_reused(self):
        s,_=self.setup();key,items=await s.source_items(None,'link')
        foreign={'bot_id':999,'items':[{'file_id':'foreign'}]}
        s.repo.private_cache=AsyncMock(return_value=foreign);s.repo.cached_file_payload.return_value=foreign
        self.assertIsNone(await f.find(s,key,items,'old-cache-key'))
        s.repo.private_ready.assert_not_awaited();s.api.assert_not_awaited()

    async def test_older_export_captures_file_ids_on_first_cache_hit(self):
        s,rows=self.setup();key,items=await s.source_items(None,'link')
        rows[cache_key(123,s.almacen,key,items)]={'state':'ready','payload':{'ids':[55]}}
        s.bot=NS(get_messages=AsyncMock(return_value=[uploaded(55)]))
        self.assertEqual(await deliver_cached(s,2,None,'link',set()),1)
        s.bot.get_messages.assert_awaited_once_with(s.almacen,ids=[55])
        self.assertEqual(s.api.call_args.args[0],'sendPhoto')
        saved=await s.repo.private_cache(f.identity(123,key,items))
        self.assertTrue(f.valid(saved,123,1))
        s.bot.get_messages.reset_mock();s.almacen=-100888
        self.assertEqual(await deliver_cached(s,3,None,'link',set()),1)
        s.bot.get_messages.assert_not_awaited();s.deliver_items.assert_not_awaited()

    async def test_access_must_be_verified_even_with_saved_files(self):
        s,_=self.setup();s.source_items.side_effect=ValueError('Sin acceso')
        with self.assertRaisesRegex(ValueError,'Sin acceso'):await deliver_cached(s,2,None,'link',set())
        s.repo.private_cache.assert_not_awaited();s.api.assert_not_awaited()
