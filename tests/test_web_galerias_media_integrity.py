"""Protect R2 originals from short Telegram reads and recover verified bad MP4s."""
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, MagicMock, patch
from web_galerias.storage import PrivateStorage
from web_galerias.sync import backup_one
from web_galerias import video_diagnostic


class FakeR2:
    def __init__(self):
        self.calls=[]
    def create_multipart_upload(self,**kwargs):
        self.calls.append("create")
        return {"UploadId":"repair"}
    def upload_part(self,**kwargs):
        self.calls.append("part")
        return {"ETag":"fake"}
    def complete_multipart_upload(self,**kwargs):
        self.calls.append("complete")
    def abort_multipart_upload(self,**kwargs):
        self.calls.append("abort")


class IntegrityTests(unittest.IsolatedAsyncioTestCase):
    def store(self):
        r2=FakeR2()
        g=PrivateStorage()
        g.bucket="gallery"
        g.endpoint="https://unit-test.r2.cloudflarestorage.com"
        g.key="fake";g.secret="fake"
        g.client=r2
        return g,r2

    async def test_a_one_kib_mp4_without_moov_is_never_published(self):
        store,r2=self.store()
        async def chunks():
            yield bytes.fromhex("00000018667479706d703432")+b"mdat"+b"x"*1008
        with self.assertRaisesRegex(ValueError,"metadatos"):
            await store.upload_stream(chunks(),"folders/7/5/original.mp4","video/mp4")
        self.assertIn("abort",r2.calls)
        self.assertNotIn("complete",r2.calls)
        self.assertNotIn("part",r2.calls)

    async def test_short_telegram_download_aborts_even_if_mp4_metadata_exists(self):
        store,r2=self.store()
        async def chunks():
            yield b"ftypmoovmdat"+b"x"*1000
        with self.assertRaisesRegex(ValueError,"tamaño"):
            await store.upload_stream(chunks(),"folders/7/5/original.mp4",
                                      "video/mp4",expected_size=60*1024*1024)
        self.assertIn("abort",r2.calls)
        self.assertNotIn("complete",r2.calls)

    async def test_uses_source_document_size_and_retries_bad_r2_object(self):
        db=N(execute=AsyncMock())
        storage=N(object_size=AsyncMock(side_effect=[1024,100]),
                  upload_stream=AsyncMock(return_value=8192))
        g=N(storage=storage,db=lambda:db)
        message=N(id=5,document=N(size=8192),media=N())
        client=N(iter_download=MagicMock(return_value="bounded_stream"),
                 download_media=AsyncMock())
        item={"kind":"video","mime":"video/mp4","size":1024,"message":5}
        await backup_one(g,7,item,message,client,AsyncMock())
        self.assertEqual(client.iter_download.call_args.kwargs["file_size"],8192)
        self.assertIs(client.iter_download.call_args.args[0],message.document)
        self.assertEqual(storage.upload_stream.await_args.kwargs["expected_size"],8192)
        self.assertIn("state='ready'",db.execute.await_args.args[0])
        self.assertEqual(db.execute.await_args.args[-1],8192)

    async def test_missing_telegram_size_is_not_replaced_by_old_1kb_database_value(self):
        db=N(execute=AsyncMock())
        storage=N(object_size=AsyncMock(side_effect=[1024,100]),
                  upload_stream=AsyncMock())
        g=N(storage=storage,db=lambda:db)
        # A metadata-less message cannot certify how much media was fetched.
        message=N(id=5,document=None,media=N(),file=None)
        client=N(iter_download=MagicMock(),download_media=AsyncMock())
        item={"kind":"video","mime":"video/mp4","size":1024,"message":5}
        with self.assertRaisesRegex(ValueError,"tamaño real"):
            await backup_one(g,7,item,message,client,AsyncMock())
        client.iter_download.assert_not_called()
        storage.upload_stream.assert_not_awaited()
        db.execute.assert_not_awaited()

    async def test_only_two_proven_invalid_mp4_are_requeued_without_r2_delete(self):
        rows=[{"id":132224,"kind":"video","mime":"video/mp4",
               "size":1024,"state":"ready","file_key":"folders/7/5/original.mp4"}]
        db=N(fetch=AsyncMock(return_value=rows),execute=AsyncMock())
        g=N(db=lambda:db,storage=N())
        info={"r2_bytes":1024,"r2_mime":"video/mp4",
              "sample":{"signature":"mp4","brand":"mp42",
                        "moov_first":False,"moov_last":False,
                        "mdat_first":True,"mdat_last":True,"codecs_sampled":[]}}
        with (patch.object(video_diagnostic.asyncio,"sleep",new=AsyncMock()),
              patch.object(video_diagnostic,"_head_and_windows",return_value=info),
              patch("web_galerias.on_demand.active",return_value=True)):
            await video_diagnostic.run(g)
        statements=[call.args[0] for call in db.execute.await_args_list]
        self.assertEqual(len(statements),2)
        self.assertIn("state='preview'",statements[0])
        self.assertIn("INSERT INTO wg_media_priority",statements[1])
        self.assertTrue(all("DELETE" not in s for s in statements))
        self.assertTrue(all("wg_media" in s for s in statements))


if __name__=="__main__":
    unittest.main()
