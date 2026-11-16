"""Dedicated Telegram BOT feed: safety, idempotency, forward/album and routing tests."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
from aiohttp import web
from web_galerias import bot_feed as feed


class NewBotMediaTests(unittest.TestCase):
    def test_forwarded_photo_and_album_keep_caption_and_group(self):
        update={
            "update_id":100,
            "message":{
                "message_id":345, "date":1700000000,
                "chat":{"id":-1001234567890,"type":"supergroup"},
                "forward_origin":{"type":"channel","message_id":33},
                "media_group_id":"album_ABC",
                "caption":"Texto original #fiesta",
                "photo":[
                    {"file_id":"small","file_size":8000,"width":90,"height":90},
                    {"file_id":"large","file_size":120000,"width":800,"height":800}
                ]
            }
        }
        got=feed.media_from_update(update)
        self.assertEqual(got["file_id"],"large")
        self.assertEqual(got["thumb_file_id"],"small")
        self.assertEqual(got["group_id"],-1001234567890)
        self.assertEqual(got["caption"],"Texto original #fiesta")
        self.assertEqual(got["grouped"],"album_ABC")
        self.assertEqual(got["kind"],"photo")
        self.assertEqual(got["source_type"],"photo")

    def test_forwarded_telegram_video_under_20mb(self):
        update={"channel_post":{
            "message_id":78,"date":1700000001,
            "chat":{"id":-1001234567890,"type":"channel"},
            "caption":"Video original","video":{
                "file_id":"clip_file","file_size":19900000,
                "width":1920,"height":1080,
                "thumbnail":{"file_id":"cover"}
            }}}
        got=feed.media_from_update(update)
        self.assertEqual(got["file_id"],"clip_file")
        self.assertEqual(got["thumb_file_id"],"cover")
        self.assertEqual(got["kind"],"video")
        self.assertEqual(got["caption"],"Video original")
        self.assertLess(got["file_size"],feed.MAX_FILE_BYTES)

    def test_accept_photo_and_video_documents_and_gifs(self):
        base={"message_id":2,"date":1700000001,
              "chat":{"id":-1004567,"type":"supergroup"}}
        for content,expected in [
            ({"document":{"file_id":"doc","mime_type":"image/jpeg",
                          "file_name":"foto.jpg","file_size":1024}},"photo"),
            ({"document":{"file_id":"doc","mime_type":"video/mp4",
                          "file_name":"movie.mp4","file_size":2048}},"video"),
            ({"animation":{"file_id":"gif","file_size":1200}},"video"),
        ]:
            with self.subTest(content=content):
                got=feed.media_from_update({"message":dict(base,**content)})
                self.assertIsNotNone(got)
                self.assertEqual(got["kind"],expected)

    def test_reject_text_dm_executable_audio_and_invalid_group(self):
        for content in [
            {"text":"solo texto"},
            {"document":{"file_id":"doc","mime_type":"application/zip"}},
            {"voice":{"file_id":"audio"}},
        ]:
            update={"message":{"message_id":2,"date":1700000001,
                     "chat":{"id":-1001,"type":"supergroup"},**content}}
            self.assertIsNone(feed.media_from_update(update))
        self.assertIsNone(feed.media_from_update({"message":{
            "message_id":2,"date":1700000001,
            "chat":{"id":1234,"type":"private"},
            "photo":[{"file_id":"a","file_size":100}]
        }}))

    def test_object_keys_do_not_use_filename_or_original_sync_peers(self):
        item={"group_id":-1001234567890,"message_id":987,
              "mime":"video/mp4","kind":"video","filename":"secret user file.mp4"}
        original,thumbnail=feed.storage_keys(item)
        self.assertEqual(original,"botfeed/1001234567890/987/original.mp4")
        self.assertEqual(thumbnail,"botfeed/1001234567890/987/thumb.jpg")
        self.assertNotIn("secret",original)

    def test_strict_group_validation(self):
        for bad in (0,1,-1,True,"not an id",None):
            with self.subTest(bad=bad),self.assertRaises(web.HTTPBadRequest):
                feed.group_id(bad)
        self.assertEqual(feed.group_id("-1001234567890"),-1001234567890)

    def test_file_paths_cannot_escape_telegram_hostname(self):
        for path in ("https://evil.example","../../secret","/etc/passwd",
                     "file name.jpg","\\etc\\pw","photos/../../secret"):
            with self.subTest(path=path),self.assertRaises(ValueError):
                feed._file_url("token",path)
        self.assertEqual(
            feed._file_url("token","photos/file_17.jpg"),
            "https://api.telegram.org/file/bottoken/photos/file_17.jpg")


class RouteChangesTests(unittest.IsolatedAsyncioTestCase):
    def make_gallery(self):
        db=SimpleNamespace(
            execute=AsyncMock(),
            fetchval=AsyncMock(return_value=9),
            fetchrow=AsyncMock(return_value=None),
            fetch=AsyncMock(return_value=[]))
        gallery=SimpleNamespace(
            db=lambda:db,
            log=AsyncMock(),
            _bot_feed_schema_ready=True)
        return gallery,db

    async def test_binding_checks_bot_membership_and_writes_only_new_route_table(self):
        gallery,db=self.make_gallery()
        with patch.object(feed,"token",return_value="123:BOT"),patch.object(
            feed,"verify_membership",new_callable=AsyncMock
        ) as verify:
            result=await feed.route_operation(gallery,1,"admin-bot-feed-set",{
                "folder":9,"group_id":-1001234567890})
        self.assertTrue(result["ok"])
        verify.assert_awaited_once()
        self.assertEqual(db.execute.await_count,1)
        sql=db.execute.await_args.args[0]
        self.assertIn("wg_bot_feed_routes",sql)
        self.assertIn("ON CONFLICT(folder)",sql)
        self.assertNotIn("UPDATE wg_folders",sql)
        self.assertNotIn("descarga_accounts",sql)
        self.assertEqual(db.execute.await_args.args[2],-1001234567890)

    async def test_repeated_mapping_preserves_revision_and_pending_work(self):
        gallery,db=self.make_gallery()
        db.fetchrow.return_value={"group_id":-100123}
        with patch.object(feed,"token",return_value="123:BOT"),patch.object(
            feed,"verify_membership",new_callable=AsyncMock):
            result=await feed.route_operation(gallery,1,"admin-bot-feed-set",{
                "folder":9,"group_id":-100123})
        self.assertTrue(result["unchanged"])
        db.execute.assert_not_awaited()

    async def test_group_cannot_be_bound_twice(self):
        gallery,db=self.make_gallery()
        db.execute.side_effect=asyncpg.UniqueViolationError("duplicate")
        with patch.object(feed,"token",return_value="123:BOT"),patch.object(
            feed,"verify_membership",new_callable=AsyncMock):
            with self.assertRaises(web.HTTPConflict):
                await feed.route_operation(gallery,1,"admin-bot-feed-set",{
                    "folder":9,"group_id":-100123})
        self.assertIn("UNIQUE",feed.SCHEMA)
        self.assertIn("group_id BIGINT NOT NULL UNIQUE",feed.SCHEMA)

    async def test_remove_only_mapping_preserves_all_media(self):
        gallery,db=self.make_gallery()
        result=await feed.route_operation(gallery,1,"admin-bot-feed-remove",{
            "folder":9})
        self.assertTrue(result["saved_media_preserved"])
        self.assertIn("DELETE FROM wg_bot_feed_routes",db.fetchval.await_args.args[0])
        self.assertNotIn("wg_media",db.fetchval.await_args.args[0])
        db.execute.assert_not_awaited()

    async def test_no_token_does_not_mutate_any_routing(self):
        gallery,db=self.make_gallery()
        with patch.object(feed,"token",return_value=""):
            with self.assertRaises(web.HTTPServiceUnavailable):
                await feed.route_operation(gallery,1,"admin-bot-feed-set",{
                    "folder":9,"group_id":-100123})
        db.execute.assert_not_awaited()


class DownloadProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reject_oversize_before_requesting_telegram(self):
        gallery=SimpleNamespace(db=lambda:SimpleNamespace(
            fetchval=AsyncMock(return_value=1),
            execute=AsyncMock()))
        storage=SimpleNamespace(object_size=AsyncMock())
        gallery.storage=storage
        item={"folder":8,"group_id":-1001,"revision":"x",
              "message_id":3,"file_size":feed.MAX_FILE_BYTES+1}
        with patch.object(feed,"route_current",new_callable=AsyncMock,
                          return_value=True),patch.object(
            feed,"file_info",new_callable=AsyncMock) as lookup:
            await feed.process_item(gallery,None,"token",item)
        lookup.assert_not_awaited()
        storage.object_size.assert_not_awaited()

    async def test_removed_route_aborts_before_telegram(self):
        gallery=SimpleNamespace(db=lambda:SimpleNamespace(
            execute=AsyncMock()))
        item={"folder":8,"group_id":-1001,"revision":"old",
              "message_id":3}
        with patch.object(feed,"route_current",new_callable=AsyncMock,
                          return_value=False),patch.object(
            feed,"file_info",new_callable=AsyncMock) as lookup:
            await feed.process_item(gallery,None,"token",item)
        lookup.assert_not_awaited()
        self.assertIn("skipped",gallery.db().execute.await_args.args[0])


if __name__=="__main__":
    unittest.main()
