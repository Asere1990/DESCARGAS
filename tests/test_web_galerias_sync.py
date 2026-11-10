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
    sync_folder, media_kind, find_dialog, fair_round, sync_limits
)
from web_galerias.storage import PrivateStorage
from web_galerias.admin import admin_op


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
