"""Pruebas de copia masiva segura: parada, reanudación, idempotencia y multipart."""
import asyncio
import io
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from web_galerias.sync import (
    PauseGuard, PauseRequested, backup_one, folder_active, source_free,
    sync_folder, media_kind, find_dialog, fair_round, sync_limits, preview_one,
    archive_candidates, choose_account,
    inspect_source, catalog, refresh_telegram_counts
)
from web_galerias.storage import PrivateStorage
from web_galerias.admin import admin_op
from web_galerias.media_types import classify
from web_galerias.gif_backup import backup_gif


class FakeStore:
    def __init__(self):
        self.execute=AsyncMock()
        self.fetchval=AsyncMock()
        self.fetchrow=AsyncMock()


def gallery(store=None):
    db=store or FakeStore()
    obj=SimpleNamespace(
        closed=False,
        db=lambda:db,
        storage=SimpleNamespace(object_size=AsyncMock(),upload=AsyncMock(),
                                upload_stream=AsyncMock(),ready=True),
        s=SimpleNamespace(),
        log=AsyncMock())
    return obj,db


class SafeSync(unittest.IsolatedAsyncioTestCase):
    async def test_cached_peer_prevents_repeated_dialog_scan(self):
        from telethon import types
        g,db=gallery()
        db.fetchval=AsyncMock(return_value=b"encrypted-handle")
        g.s.repo=SimpleNamespace(decode=lambda value:{
            "kind":"channel","id":771234567,"hash":987654321})
        client=SimpleNamespace(iter_dialogs=MagicMock())
        result=await find_dialog(g,client,8,123456,-1000771234567)
        self.assertIsInstance(result,types.InputPeerChannel)
        self.assertEqual(result.channel_id,771234567)
        client.iter_dialogs.assert_not_called()

    async def test_remove_folder_never_starts_telegram_session(self):
        g,db=gallery()
        row={"id":7,"peer":-100998877,"source_uid":None}
        with patch("web_galerias.sync.folder_active",new=AsyncMock(return_value=False)):
            with patch("web_galerias.sync.choose_account",new=AsyncMock()) as chooser:
                await sync_folder(g,row)
                chooser.assert_not_awaited()

    async def test_pause_guard_stops_if_folder_removed(self):
        g,db=gallery()
        db.fetchval=AsyncMock(return_value=False)
        with patch.dict(os.environ,{"GALERIAS_SYNC_ENABLED":"1"}):
            guard=PauseGuard(g,7539025033,7)
            with self.assertRaises(PauseRequested):await guard()

    async def test_pause_guard_yields_to_main_downloads(self):
        g,db=gallery()
        db.fetchval=AsyncMock(side_effect=[True,True])
        with patch.dict(os.environ,{"GALERIAS_SYNC_ENABLED":"1"}):
            guard=PauseGuard(g,7539025033,7)
            with self.assertRaises(PauseRequested):await guard()

    async def test_disabled_global_sync_never_fetches_telegram(self):
        g,db=gallery()
        db.fetchval=AsyncMock(return_value=True)
        with patch.dict(os.environ,{"GALERIAS_SYNC_ENABLED":"0"}):
            self.assertFalse(await source_free(g,7539025033,7))

    async def test_already_backed_file_marks_ready_without_redownload(self):
        g,db=gallery()
        # Existing R2 original and thumbnail: resume from durable object keys.
        g.storage.object_size.side_effect=[512,120]
        client=SimpleNamespace(download_media=AsyncMock(),iter_download=MagicMock())
        item={"kind":"photo","mime":"image/jpeg","size":512,"message":202}
        message=SimpleNamespace(id=202,noforwards=False)
        guard=AsyncMock()
        await backup_one(g,7,item,message,client,guard)
        client.download_media.assert_not_awaited()
        client.iter_download.assert_not_called()
        db.execute.assert_awaited_once()
        args=db.execute.await_args.args
        self.assertIn("state='ready'",args[0])
        self.assertEqual(args[1:3],(7,202))
        self.assertEqual(args[3],"folders/7/202/original.jpg")

    async def test_no_overwrite_on_pause(self):
        g,db=gallery()
        guard=AsyncMock(side_effect=PauseRequested())
        item={"kind":"photo","mime":"image/jpeg","size":512,"message":202}
        with self.assertRaises(PauseRequested):
            await backup_one(g,7,item,SimpleNamespace(id=202),None,guard)
        g.storage.object_size.assert_not_awaited()
        db.execute.assert_not_awaited()

    async def test_pause_works_during_video_multipart(self):
        storage=PrivateStorage()
        storage.bucket="galeria"
        storage.endpoint="https://unit-test.r2.cloudflarestorage.com"
        storage.key="fake";storage.secret="fake"
        calls=[]
        class FakeClient:
            def create_multipart_upload(self,**kwargs):
                calls.append("create")
                return {"UploadId":"upload1"}
            def upload_part(self,**kwargs):
                calls.append(("part",len(kwargs["Body"])))
                return {"ETag":'"tag1"'}
            def abort_multipart_upload(self,**kwargs):
                calls.append("abort")
            def complete_multipart_upload(self,**kwargs):
                calls.append("complete")
        storage.client=FakeClient()
        async def chunks():
            yield b"x"*(8*1024*1024)
            yield b"y"*(3*1024*1024)
        count=0
        async def check():
            nonlocal count
            count+=1
            if count>=3:raise PauseRequested()
        with self.assertRaises(PauseRequested):
            await storage.upload_stream(chunks(),"folders/7/202/original.mp4","video/mp4",check)
        self.assertIn("abort",calls)
        self.assertNotIn("complete",calls)

    async def test_completed_video_upload_is_full_size(self):
        storage=PrivateStorage()
        storage.bucket="galeria";storage.endpoint="https://unit-test.r2.cloudflarestorage.com"
        storage.key="fake";storage.secret="fake"
        calls=[]
        class FakeClient:
            def create_multipart_upload(self,**kwargs):return {"UploadId":"id"}
            def upload_part(self,**kwargs):
                calls.append(("part",len(kwargs["Body"])))
                return {"ETag":f'part{len(calls)}'}
            def complete_multipart_upload(self,**kwargs):
                calls.append(("complete",kwargs["MultipartUpload"]["Parts"]))
        storage.client=FakeClient()
        storage.object_size=AsyncMock(return_value=9*1024*1024)
        async def chunks():
            yield b"x"*(8*1024*1024)
            yield b"y"*(1*1024*1024)
        size=await storage.upload_stream(chunks(),"folders/7/202/original.mp4","video/mp4")
        self.assertEqual(size,9*1024*1024)
        self.assertEqual([x[1] for x in calls if x[0]=="part"],[8*1024*1024,1*1024*1024])
        self.assertEqual(len(calls[-1][1]),2)

    async def test_admin_hide_pauses_without_r2_deletion(self):
        g,db=gallery()
        with patch("web_galerias.admin.is_admin",return_value=True):
            result=await admin_op(g,7539025033,"admin-folder-remove",{"folder":7})
        self.assertTrue(result["ok"])
        sql=db.execute.await_args.args[0]
        self.assertIn("active=FALSE",sql)
        self.assertNotIn("DELETE FROM wg_media",sql)
        self.assertFalse(hasattr(g.storage,"delete"))

    async def test_admin_readdition_keeps_folder_and_retries_missing(self):
        g,db=gallery()
        db.fetchrow.return_value={"id":7}
        with patch("web_galerias.admin.is_admin",return_value=True):
            result=await admin_op(g,7539025033,"admin-folder",
                  {"peer":-100998877,"name":"Trabajo"})
        self.assertEqual(result["id"],7)
        executed=[x.args[0] for x in db.execute.await_args_list]
        self.assertTrue(any("state='missing'" in x for x in executed))
        self.assertFalse(any("DELETE" in x for x in executed))

    async def test_thumbnail_first_exposes_preview_before_original_backup(self):
        from pathlib import Path
        from PIL import Image
        g,db=gallery()
        g.storage.object_size.side_effect=[None,731]
        async def fake_download(message,*,file,thumb):
            image=Image.new("RGB",(20,20),(120,40,60))
            image.save(str(file)+".jpg","JPEG")
            return str(file)+".jpg"
        client=SimpleNamespace(download_media=AsyncMock(side_effect=fake_download))
        message=SimpleNamespace(id=202,photo=SimpleNamespace(
            sizes=[SimpleNamespace(w=320)]))
        # R2 HEAD returns the exact saved JPEG length.
        async def object_size(key):
            if object_size.calls==0:
                object_size.calls+=1
                return None
            return object_size.upload_bytes
        object_size.calls=0
        object_size.upload_bytes=0
        async def upload(path,key,mime):
            object_size.upload_bytes=Path(path).stat().st_size
        g.storage.object_size=AsyncMock(side_effect=object_size)
        g.storage.upload=AsyncMock(side_effect=upload)
        await preview_one(g,7,{"kind":"photo"},message,client,AsyncMock())
        self.assertIn("state='preview'",db.execute.await_args.args[0])
        self.assertEqual(db.execute.await_args.args[-1],"folders/7/202/thumb.jpg")
        client.download_media.assert_awaited_once()
        g.storage.upload.assert_awaited_once()

    async def test_existing_preview_is_not_downloaded_again(self):
        g,db=gallery()
        g.storage.object_size.return_value=72
        client=SimpleNamespace(download_media=AsyncMock())
        await preview_one(g,7,{"kind":"video"},SimpleNamespace(id=202),
                          client,AsyncMock())
        client.download_media.assert_not_awaited()
        self.assertEqual(db.execute.await_args.args[-1],"folders/7/202/thumb.jpg")

    async def test_user_catalog_has_preview_without_original_view_link(self):
        from web_galerias.core import GalleryCore
        sample=[
            {"id":101,"kind":"video","state":"preview","thumb_key":"thumb",
             "mime":"video/mp4","filename":"demo.mp4","date":0,
             "width":0,"height":0,"size":111},
            {"id":102,"kind":"photo","state":"ready","thumb_key":"thumb",
             "mime":"image/jpeg","filename":"demo.jpg","date":0,
             "width":0,"height":0,"size":222}]
        g=GalleryCore(SimpleNamespace())
        g.db=lambda:SimpleNamespace(fetch=AsyncMock(return_value=sample))
        g.permitted=AsyncMock(return_value=True)
        result=await g.list_items(1234,7,token=lambda uid,item,kind:"safe")
        self.assertEqual(len(result["items"]),2)
        self.assertEqual(result["items"][0]["available"],False)
        self.assertIsNone(result["items"][0]["view"])
        self.assertIn("/thumb?",result["items"][0]["thumb"])
        self.assertTrue(result["items"][1]["available"])
        self.assertIn("/view?",result["items"][1]["view"])
        self.assertNotIn("thumb_key",result["items"][0])

    def doc(self,mime,filename=None,animated=False,sticker=False):
        from telethon import types
        attributes=[]
        if filename:
            attributes.append(types.DocumentAttributeFilename(file_name=filename))
        if animated:
            attributes.append(types.DocumentAttributeAnimated())
        if sticker:
            attributes.append(types.DocumentAttributeSticker(alt="",stickerset=types.InputStickerSetEmpty()))
        return SimpleNamespace(photo=None,document=SimpleNamespace(
            mime_type=mime,size=1512,attributes=attributes))

    def test_document_photos_videos_and_gifs_are_classified(self):
        samples=[
            (self.doc("image/png","album.png"),("photo","image/png","photo_file")),
            (self.doc("image/jpeg","foto.jpg"),("photo","image/jpeg","photo_file")),
            (self.doc("application/octet-stream","foto.webp"),("photo","image/webp","photo_file")),
            (self.doc("video/mp4","clip.mp4"),("video","video/mp4","video_file")),
            (self.doc("application/octet-stream","clip.mov"),("video","video/quicktime","video_file")),
            (self.doc("image/gif","anim.gif"),("video","image/gif","gif")),
            (self.doc("video/mp4","animation.mp4",animated=True),("video","video/mp4","gif")),
            (self.doc("image/svg+xml"),(None,None,None)),
            (self.doc("image/webp","sticker.webp",sticker=True),(None,None,None)),
        ]
        for m,expected in samples:
            with self.subTest(expected=expected):self.assertEqual(classify(m),expected)
        self.assertEqual(classify(SimpleNamespace(photo=object(),document=None)),
                         ("photo","image/jpeg","photo"))
        self.assertEqual(classify(SimpleNamespace(photo=None,document=None)),
                         (None,None,None))

    async def test_catalog_ignores_conversations_and_only_indexes_media_captions(self):
        g,db=gallery()
        text=SimpleNamespace(photo=None,document=None,message="texto privado",
                             reply_to=SimpleNamespace(reply_to_msg_id=9),id=100)
        await catalog(g,7,text)
        db.execute.assert_not_awaited()
        image=SimpleNamespace(photo=object(),document=None,message="📍 Caption del álbum",
                              noforwards=False,ttl_period=None,media=None,date=None,
                              grouped_id=999,reply_to=None,id=101)
        await catalog(g,7,image)
        args=db.execute.await_args.args
        self.assertIn("INSERT INTO wg_media",args[0])
        self.assertNotIn("wg_replies",args[0])
        self.assertIn("caption del album",args[11])
        self.assertEqual(args[13],"999")
        self.assertEqual(args[-1],"photo")

    async def test_legacy_missing_needs_three_checks_not_one(self):
        g,db=gallery()
        item={"state":"pending","message":202,"kind":"photo",
              "mime":"image/jpeg","source_checks":0,"source_type":"photo"}
        self.assertFalse(await inspect_source(g,7,item,None))
        args=db.execute.await_args.args
        self.assertEqual(args[4],"pending")
        self.assertEqual(args[5],"message_absent_pending_recheck")
        self.assertEqual(args[6],1)
        self.assertNotIn("state='missing'",args[0])
        db.execute.reset_mock()
        item["source_checks"]=2
        self.assertFalse(await inspect_source(g,7,item,None))
        args=db.execute.await_args.args
        self.assertEqual(args[4],"missing")
        self.assertEqual(args[5],"message_absent_after_rechecks")
        self.assertEqual(args[6],3)

    async def test_protected_media_is_skipped_not_marked_missing(self):
        g,db=gallery()
        item={"state":"pending","message":202,"kind":"photo",
              "mime":"image/jpeg","source_checks":0,"source_type":"photo"}
        m=SimpleNamespace(noforwards=True,photo=None,document=None)
        self.assertFalse(await inspect_source(g,7,item,m))
        self.assertIn("state='skipped'",db.execute.await_args.args[0])
        self.assertNotIn("state='missing'",db.execute.await_args.args[0])

    async def test_new_file_type_reclassifies_instead_of_missing(self):
        g,db=gallery()
        item={"state":"pending","message":202,"kind":"video",
              "mime":"video/mp4","source_checks":0,"source_type":"video"}
        m=self.doc("image/png","imagen.png")
        self.assertFalse(await inspect_source(g,7,item,m))
        args=db.execute.await_args.args
        self.assertEqual(args[4],"photo")
        self.assertEqual(args[5],"image/png")
        self.assertEqual(args[6],"photo_file")
        self.assertNotIn("state='missing'",args[0])

    async def test_gif_stores_source_and_playable_mp4_without_overwriting_original(self):
        from pathlib import Path
        from PIL import Image
        g,db=gallery()
        data={}
        async def object_size(key):return data.get(key)
        async def upload(path,key,mime):
            data[key]=Path(path).stat().st_size
        async def download_media(message,file):
            image=Image.new("RGB",(20,20),(30,60,90))
            image.save(file,format="GIF")
            return file
        def convert(src,dst):
            Path(dst).write_bytes(b"MP4 test fixture bytes")
        g.storage.object_size=AsyncMock(side_effect=object_size)
        g.storage.upload=AsyncMock(side_effect=upload)
        client=SimpleNamespace(download_media=AsyncMock(side_effect=download_media))
        with patch("web_galerias.gif_backup.convert_gif_to_mp4",side_effect=convert):
            await backup_gif(g,7,{"filename":"anim.gif"},SimpleNamespace(id=202),
                             client,AsyncMock())
        self.assertIn("folders/7/202/source.gif",data)
        self.assertIn("folders/7/202/original.mp4",data)
        self.assertIn("folders/7/202/thumb.jpg",data)
        args=db.execute.await_args.args
        self.assertIn("state='ready'",args[0])
        self.assertEqual(args[3],"folders/7/202/original.mp4")
        self.assertEqual(args[6],"video/mp4")
        self.assertEqual(args[7],"anim.mp4")

    async def test_photo_document_preserves_png_original(self):
        from pathlib import Path
        from PIL import Image
        g,db=gallery()
        original={}
        async def object_size(key):return original.get(key)
        async def upload(path,key,mime):original[key]=Path(path).stat().st_size
        async def download_media(message,*,file,progress_callback):
            Image.new("RGB",(20,20),(0,120,90)).save(file,format="PNG")
            return file
        g.storage.object_size=AsyncMock(side_effect=object_size)
        g.storage.upload=AsyncMock(side_effect=upload)
        client=SimpleNamespace(download_media=AsyncMock(side_effect=download_media))
        await backup_one(g,7,{"message":202,"kind":"photo","mime":"image/png","size":0},
                         SimpleNamespace(id=202),client,AsyncMock())
        self.assertIn("folders/7/202/original.png",original)
        self.assertIn("folders/7/202/thumb.jpg",original)
        self.assertIn("state='ready'",db.execute.await_args.args[0])

    async def test_telegram_totals_use_server_filters_without_downloading_messages(self):
        from telethon import types
        g,db=gallery()
        db.fetchval.return_value=7
        client=SimpleNamespace(
            get_messages=AsyncMock(side_effect=[
                SimpleNamespace(total=51443),SimpleNamespace(total=35572)]),
            iter_messages=MagicMock(),download_media=AsyncMock())
        guard=AsyncMock()
        started=__import__("time").time()
        with patch.dict(os.environ,{"GALERIAS_TELEGRAM_TOTALS_INTERVAL":"1800"}):
            result=await refresh_telegram_counts(g,7,client,"telegram_group",guard)
        self.assertTrue(result)
        self.assertEqual(client.get_messages.await_count,2)
        args=client.get_messages.await_args_list
        self.assertEqual(args[0].args,("telegram_group",0))
        self.assertEqual(args[1].args,("telegram_group",0))
        self.assertIs(args[0].kwargs["filter"],types.InputMessagesFilterPhotos)
        self.assertIs(args[1].kwargs["filter"],types.InputMessagesFilterVideo)
        client.iter_messages.assert_not_called()
        client.download_media.assert_not_awaited()
        record=db.execute.await_args.args
        self.assertIn("telegram_photos_total=$2",record[0])
        self.assertEqual(record[1:4],(7,51443,35572))
        self.assertGreaterEqual(record[4],started)
        self.assertGreaterEqual(record[5],started+1800)
        self.assertLess(record[5],started+1800+190)
        self.assertEqual(guard.await_count,3)

    async def test_zero_telegram_totals_are_valid_and_differ_from_unknown(self):
        g,db=gallery()
        db.fetchval.return_value=5
        client=SimpleNamespace(get_messages=AsyncMock(
            side_effect=[SimpleNamespace(total=0),SimpleNamespace(total=0)]))
        self.assertTrue(await refresh_telegram_counts(g,5,client,"empty",AsyncMock()))
        self.assertEqual(db.execute.await_args.args[2:4],(0,0))

    async def test_counts_skip_before_interval_without_any_rpc_calls(self):
        g,db=gallery()
        db.fetchval.return_value=None
        client=SimpleNamespace(get_messages=AsyncMock())
        self.assertFalse(await refresh_telegram_counts(g,7,client,"peer",AsyncMock()))
        client.get_messages.assert_not_awaited()
        db.execute.assert_not_awaited()

    async def test_counter_errors_keep_previous_totals_and_retry_later(self):
        g,db=gallery()
        db.fetchval.return_value=7
        client=SimpleNamespace(get_messages=AsyncMock(side_effect=TimeoutError()))
        self.assertFalse(await refresh_telegram_counts(g,7,client,"peer",AsyncMock()))
        sql=db.execute.await_args.args[0]
        self.assertIn("telegram_counts_error=$2",sql)
        self.assertIn("telegram_counts_next_attempt=$3",sql)
        self.assertNotIn("telegram_photos_total",sql)
        self.assertEqual(db.execute.await_args.args[2],"TimeoutError")

    async def test_counts_paused_for_main_download_do_not_reset_old_totals(self):
        g,db=gallery()
        db.fetchval.return_value=7
        with self.assertRaises(PauseRequested):
            await refresh_telegram_counts(g,7,SimpleNamespace(get_messages=AsyncMock()),
                                          "peer",AsyncMock(side_effect=PauseRequested()))
        self.assertNotIn("telegram_photos_total",db.execute.await_args.args[0])

    def test_supervisor_queries_only_a_few_due_folders_after_priority_previews(self):
        from pathlib import Path
        text=(Path(__file__).resolve().parents[1]/"web_galerias"/"sync.py").read_text()
        prime=text.index('await fair_round(g,folders,phase="prime")')
        counter=text.index('await fair_round(g,due,phase="counts")')
        archive=text.index('await fair_round(g,selected,phase="archive")')
        self.assertLess(prime,counter)
        self.assertLess(counter,archive)
        self.assertIn("telegram_counts_next_attempt<=$1",text)
        self.assertIn("ORDER BY f.telegram_counts_at ASC,f.id ASC LIMIT 3",text)

    async def test_preferred_account_must_match_linked_telegram_id(self):
        g,db=gallery()
        db.fetchrow.return_value={"uid":123456789}
        folder={"id":7,"source_uid":111111111,"preferred_uid":123456789}
        result=await choose_account(g,folder)
        self.assertEqual(result,123456789)
        sql=db.fetchrow.await_args.args
        self.assertIn("($4::bigint IS NULL OR a.uid=$4)",sql[0])
        self.assertEqual(sql[-1],123456789)
        self.assertEqual(sql[-2],111111111)

    async def test_switched_account_stops_old_account_from_accessing_gallery(self):
        g,db=gallery()
        db.fetchval.return_value=False
        self.assertFalse(await folder_active(g,7,111111111))
        sql=db.fetchval.await_args.args
        self.assertIn("preferred_uid=$2",sql[0])
        self.assertEqual(sql[1:3],(7,111111111))

    def test_archive_waits_for_visible_cushion_but_never_stops_r2(self):
        folders=[{"id":1},{"id":2},{"id":3}]
        counts=[{"folder":1,"pending":400,"preview":4},
                {"folder":2,"pending":0,"preview":10},
                {"folder":3,"pending":800,"preview":80}]
        self.assertEqual([f["id"] for f in archive_candidates(folders,counts,1)],[2,3])
        # Cada quinta vuelta permite respaldar incluso si el grupo crece.
        self.assertEqual([f["id"] for f in archive_candidates(folders,counts,5)],[1,2,3])
        self.assertEqual(archive_candidates(folders,[],1),[])

    async def test_prime_previews_have_larger_batch_than_archive(self):
        g,_=gallery()
        folders=[{"id":i+1,"source_uid":i%3+1,"preferred_uid":None} for i in range(50)]
        with patch.dict(os.environ,{"GALERIAS_SYNC_PARALLEL_ACCOUNTS":"3",
                                    "GALERIAS_SYNC_BETWEEN_FOLDERS":"0"}):
            with patch("web_galerias.sync.sync_folder",new_callable=AsyncMock) as worker:
                await fair_round(g,folders,phase="prime")
            self.assertEqual(worker.await_count,50)
            self.assertTrue(all(call.kwargs["max_files"]==2 for call in worker.await_args_list))
            worker.reset_mock()
            with patch("web_galerias.sync.sync_folder",new_callable=AsyncMock) as worker2:
                await fair_round(g,folders,phase="archive")
            self.assertTrue(all(call.kwargs["max_files"]==1 for call in worker2.await_args_list))

    def test_50_gallery_rounds_are_small_without_preventing_backups(self):
        with patch.dict(os.environ, {"GALERIAS_SYNC_BATCH_FILES": "5"}):
            self.assertEqual(sync_limits(1), (5, 90))
            self.assertEqual(sync_limits(10), (2, 60))
            self.assertEqual(sync_limits(50), (1, 30))

    async def test_50_galleries_use_bounded_parallel_accounts(self):
        g,_=gallery()
        folders=[{"id":i+1,"source_uid":i%4+1} for i in range(50)]
        active=set()
        peak=0
        calls=[]

        async def simulate(_g,folder,**kwargs):
            nonlocal peak
            account=folder["source_uid"]
            self.assertNotIn(account,active)
            active.add(account)
            peak=max(peak,len(active))
            await asyncio.sleep(0.002)
            calls.append((folder["id"],kwargs))
            active.remove(account)

        with patch.dict(os.environ, {"GALERIAS_SYNC_PARALLEL_ACCOUNTS":"3",
                                     "GALERIAS_SYNC_BETWEEN_FOLDERS":"0"}):
            with patch("web_galerias.sync.sync_folder",side_effect=simulate):
                await fair_round(g,folders)
        self.assertEqual({x[0] for x in calls},set(range(1,51)))
        self.assertEqual(len(calls),50)
        self.assertGreater(peak,1)
        self.assertLessEqual(peak,3)
        self.assertTrue(all(item["max_files"]==1 and item["scan_size"]==30
                            for _,item in calls))

    async def test_unknown_source_galleries_use_distinct_candidate_accounts(self):
        g,_=gallery()
        folders=[{"id":i+1,"source_uid":None} for i in range(50)]
        active=set()
        peak=0
        visited=[]
        async def choose(_g,folder):
            return 100+(folder["id"]%3)
        async def simulate(_g,folder,**kwargs):
            nonlocal peak
            account=await choose(_g,folder)
            self.assertNotIn(account,active)
            active.add(account)
            peak=max(peak,len(active))
            visited.append((folder["id"],kwargs["phase"]))
            await asyncio.sleep(0.003)
            active.remove(account)
        with patch.dict(os.environ,{"GALERIAS_SYNC_PARALLEL_ACCOUNTS":"3",
                                    "GALERIAS_SYNC_BETWEEN_FOLDERS":"0"}):
            with patch("web_galerias.sync.choose_account",side_effect=choose) as chooser:
                with patch("web_galerias.sync.sync_folder",side_effect=simulate):
                    await fair_round(g,folders,phase="prime")
        self.assertEqual(chooser.await_count,50)
        self.assertEqual({x[0] for x in visited},set(range(1,51)))
        self.assertEqual(set(x[1] for x in visited),{"prime"})
        self.assertEqual(peak,3)

    async def test_prime_phase_combines_index_and_preview_on_one_connection(self):
        # El despachador debe enviar índice + miniatura en el mismo turno,
        # para que 50 galerías no tengan que acabar su indexación primero.
        g,_=gallery()
        folder={"id":1,"source_uid":777}
        with patch.dict(os.environ,{"GALERIAS_SYNC_PARALLEL_ACCOUNTS":"1",
                                    "GALERIAS_SYNC_BETWEEN_FOLDERS":"0"}):
            with patch("web_galerias.sync.sync_folder",new_callable=AsyncMock) as worker:
                await fair_round(g,[folder],phase="prime")
        worker.assert_awaited_once()
        self.assertEqual(worker.await_args.kwargs["phase"],"prime")

    async def test_50_galleries_sharing_one_account_never_duplicate_session(self):
        g,_=gallery()
        folders=[{"id":i+1,"source_uid":111} for i in range(50)]
        active=0
        peak=0

        async def simulate(_g,folder,**kwargs):
            nonlocal active,peak
            active+=1
            peak=max(peak,active)
            await asyncio.sleep(0)
            active-=1

        with patch.dict(os.environ, {"GALERIAS_SYNC_PARALLEL_ACCOUNTS":"4",
                                     "GALERIAS_SYNC_BETWEEN_FOLDERS":"0"}):
            with patch("web_galerias.sync.sync_folder",side_effect=simulate) as mocked:
                await fair_round(g,folders)
        self.assertEqual(mocked.await_count,50)
        self.assertEqual(peak,1)



if __name__=="__main__":
    unittest.main()
