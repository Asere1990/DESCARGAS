"""Caption, replies and OCR always resolve to every member of one album."""
import io
import json
import unittest
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,MagicMock,patch

from web_galerias import media_text,ocr_low_priority
from web_galerias.core import normal


class AlbumTextTests(unittest.IsolatedAsyncioTestCase):
    def _gallery(self,db=None):
        db=db or N(fetch=AsyncMock(),execute=AsyncMock(),fetchrow=AsyncMock())
        return N(db=lambda:db,storage=N(ready=True),closed=False),db

    @staticmethod
    def _album(ids=range(101,111),grouped="777000"):
        return [
           {"id":i,"message":i,"grouped":grouped,
            "label":"La foto del partido" if i==101 else "",
            "ocr_text":"Nombre: Lorena" if i==107 else ""}
           for i in ids]

    async def test_reply_perico_to_one_photo_indexes_ten_album_items(self):
        rows=self._album()
        replies=[{"reply_message":350,"words":"Perico de Facebook"}]
        db=N(
          fetch=AsyncMock(side_effect=[
            [],rows,replies
          ]),execute=AsyncMock())
        g,_=self._gallery(db)
        total=await media_text.rebuild(g,7,[103,350])
        self.assertEqual(total,10)
        sql,args=db.execute.await_args.args[0],db.execute.await_args.args[1:]
        self.assertIn("UPDATE wg_media",sql)
        self.assertIn("wg_media_text_outbox",sql)
        self.assertEqual(len(args[1]),10)
        self.assertEqual(set(args[1]),set(range(101,111)))
        self.assertIn("La foto del partido",args[2])
        self.assertIn("Perico",args[3])
        self.assertIn("Lorena",args[4])
        self.assertIn("perico",args[5])
        self.assertIn("lorena",args[5])
        self.assertEqual(normal("Perico"),"perico")

    async def test_reply_to_nested_reply_finds_parent_album_via_ancestors(self):
        db=N(fetch=AsyncMock(side_effect=[
          [{"parent_message":350}],
          [{"parent_message":103}],
          [],
          self._album(),
          [{"reply_message":350,"words":"Perico"},
           {"reply_message":351,"words":"Un segundo comentario"}]
        ]),execute=AsyncMock())
        g,_=self._gallery(db)
        await media_text.rebuild(g,7,[351])
        self.assertIn("perico",db.execute.await_args.args[-1])
        self.assertIn("segundo",db.execute.await_args.args[-1])

    async def test_explicit_reply_only_never_searches_unrelated_conversation(self):
        g,db=self._gallery()
        loose=N(id=90,message="Perico",photo=None,
                reply_to=None,document=None)
        result=await media_text.capture_reply(g,7,loose)
        self.assertIsNone(result)
        db.execute.assert_not_awaited()
        linked=N(id=91,message="Perico",reply_to=N(reply_to_msg_id=103),
                 noforwards=False,ttl_period=None,media=None)
        result=await media_text.capture_reply(g,7,linked)
        self.assertEqual(result,103)
        self.assertIn("wg_replies",db.execute.await_args.args[0])
        self.assertEqual(db.execute.await_args.args[-1],"Perico")

    async def test_dirty_work_does_not_rebuild_during_active_telegram_scans(self):
        g,db=self._gallery()
        result=await media_text.enqueue(g,7,[103,350,103])
        self.assertEqual(result,2)
        self.assertIn("wg_media_text_dirty",db.execute.await_args.args[0])
        self.assertEqual(db.execute.await_args.args[-1],[103,350])
        self.assertNotIn("UPDATE wg_media",db.execute.await_args.args[0])

    async def test_rebuild_runs_from_low_priority_queue_with_no_customer_requests(self):
        rows=[{"folder":7,"message":103,"attempts":1},
              {"folder":7,"message":350,"attempts":1}]
        db=N(fetch=AsyncMock(return_value=rows),execute=AsyncMock())
        g,_=self._gallery(db)
        with patch.object(media_text,"rebuild",new=AsyncMock(return_value=10)) as rebuild:
            count=await media_text.process_dirty(g)
        self.assertEqual(count,2)
        rebuild.assert_awaited_once_with(g,7,[103,350])
        self.assertIn("DELETE FROM wg_media_text_dirty",db.execute.await_args.args[0])

    async def test_catalog_batch_captures_comments_without_waiting_for_indexing(self):
        from web_galerias import sync
        comment=N(id=350,message="Perico",photo=None,document=None,
                  reply_to=N(reply_to_msg_id=103))
        photo=N(id=103,message="",photo=object(),document=None,
                grouped_id=777,reply_to=None)
        async def stream(**kwargs):
            for item in (comment,photo):
                yield item
        db=N(execute=AsyncMock())
        g=N(db=lambda:db)
        source={"complete":False,"scan_cursor":0,"high_water":0}
        client=N(iter_messages=lambda entity,**kw:stream(**kw))
        with patch.object(sync,"folder_active",new=AsyncMock(return_value=True)),\
             patch.object(sync,"catalog",new=AsyncMock()) as catalog,\
             patch.object(media_text,"capture_reply",new=AsyncMock(
                 side_effect=[103,None])) as capture,\
             patch.object(media_text,"enqueue",new=AsyncMock(return_value=2)) as queued,\
             patch.object(media_text,"rebuild",new=AsyncMock()) as rebuild:
            count=await sync.catalog_batch(g,7,client,"source",source,uid=88)
        self.assertEqual(count,2)
        self.assertEqual(catalog.await_count,2)
        self.assertEqual(capture.await_count,2)
        queued.assert_awaited_once()
        self.assertEqual(queued.await_args.args[0:2],(g,7))
        self.assertEqual(queued.await_args.args[2],{350,103})
        rebuild.assert_not_awaited() # Search/build never blocks live preview.

    def test_r2_payload_backups_three_independent_text_sources(self):
        item={
          "folder":7,"message":103,"grouped":"777000",
          "label":"Caption de álbum","ocr_text":"Nombre: Lorena",
          "search_caption":"Caption de álbum",
          "search_replies":"Perico",
          "search_ocr":"Nombre: Lorena",
          "search_text":"caption perico nombre lorena",
          "metadata_rev":6
        }
        value=media_text.payload(item)
        self.assertEqual(value["sources"],{
          "caption":"Caption de álbum",
          "replies":"Perico",
          "ocr":"Nombre: Lorena"})
        self.assertEqual(value["revision"],6)
        self.assertEqual(value["album"],"777000")
        self.assertEqual(media_text.object_key(7,103),
         "folders/7/103/search-metadata-v1.json")
        self.assertNotIn("telegram_id",json.dumps(value))

    async def test_r2_worker_writes_archive_without_slowing_user_search(self):
        media={"id":55,"folder":7,"message":103,"grouped":"777000",
             "label":"Caption","ocr_text":"Texto imagen",
             "search_caption":"Caption","search_replies":"Perico",
             "search_ocr":"Texto imagen","search_text":"caption perico texto imagen",
             "metadata_rev":2}
        job={"media":55,"revision":2,"attempts":1}
        db=N(fetchrow=AsyncMock(side_effect=[job,media]),execute=AsyncMock())
        client=MagicMock(put_object=MagicMock())
        store=N(ready=True,bucket="gallery",_client=lambda:client)
        g=N(db=lambda:db,storage=store)
        result=await media_text.archive_one(g)
        self.assertTrue(result)
        client.put_object.assert_called_once()
        kwargs=client.put_object.call_args.kwargs
        self.assertEqual(kwargs["Key"],media_text.object_key(7,103))
        archive=json.loads(kwargs["Body"])
        self.assertEqual(archive["sources"]["replies"],"Perico")
        self.assertEqual(archive["sources"]["ocr"],"Texto imagen")
        updates=[x.args[0] for x in db.execute.await_args_list]
        self.assertTrue(any("r2_metadata_rev" in s for s in updates))

    def test_local_ocr_has_no_model_dependency_until_explicit_enable(self):
        import inspect
        code=inspect.getsource(ocr_low_priority.run)
        self.assertIn('GALERIAS_OCR_ENABLED',code)
        self.assertIn('asyncio.sleep(30)',code)
        self.assertNotIn("requests.post",code)


if __name__=="__main__":
    unittest.main()
