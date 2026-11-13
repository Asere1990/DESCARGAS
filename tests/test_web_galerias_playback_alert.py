"""Playback failure is a safe read-only R2 check plus the EXISTING queue."""
import time
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch

from aiohttp import web
from web_galerias import on_demand


def media(state="ready", size=500*1024*1024):
    return {
        "id": 200, "folder": 8, "kind": "video", "mime": "video/mp4",
        "state": state, "file_key": "folders/8/300/original.mp4" if state=="ready" else None,
        "thumb_key": "folders/8/300/thumb.jpg", "size": size,
        "active": True,
    }


class VideoFailureTests(unittest.IsolatedAsyncioTestCase):
    def fake(self, state="ready", size=500*1024*1024, r2size=None, permitted=True):
        row=media(state,size)
        db=N(fetchrow=AsyncMock(return_value=row),
             fetchval=AsyncMock(return_value=200))
        storage=N(object_size=AsyncMock(return_value=size if r2size is None else r2size))
        gallery=N(db=lambda:db,storage=storage,
                  permitted=AsyncMock(return_value=permitted))
        return gallery,db,storage

    def test_running_or_busy_queue_never_counts_as_failed(self):
        row=media("preview")
        priority={
            "state":"running", "next_attempt":0,
            "received_bytes":1_000_000, "expected_bytes":500_000_000,
            "last_error":"", "progress_at":time.time(),
        }
        result=on_demand._result(None,123,row,priority)
        self.assertFalse(result["priority_failure"])
        self.assertEqual(result["priority_state"],"running")
        self.assertEqual(result["progress_percent"],0)
        priority.update(state="queued",next_attempt=time.time()+60,
                        last_error="source_busy_or_unavailable")
        self.assertFalse(on_demand._result(None,123,row,priority)["priority_failure"])
        priority["last_error"]="invalid_or_incomplete_original"
        self.assertTrue(on_demand._result(None,123,row,priority)["priority_failure"])

    async def test_full_size_ready_original_never_uses_telegram_queue(self):
        g,db,r2=self.fake()
        with patch.object(on_demand,"request",new=AsyncMock()) as request:
            result=await on_demand.report_playback_failure(g,4321,200)
        self.assertEqual(result,{"ok":True,"requeued":False})
        r2.object_size.assert_awaited_once()
        db.fetchval.assert_not_awaited()
        request.assert_not_awaited()

    async def test_proven_short_r2_original_goes_to_existing_queue_only(self):
        g,db,r2=self.fake(size=600*1024*1024,r2size=1024)
        with patch.object(on_demand,"request",new=AsyncMock(return_value={
            "available":False})) as request:
            result=await on_demand.report_playback_failure(g,4321,200)
        self.assertEqual(result,{"ok":True,"requeued":True})
        request.assert_awaited_once_with(g,4321,200)
        sql=db.fetchval.await_args.args[0]
        self.assertIn("state='preview'",sql)
        self.assertIn("state='ready'",sql)
        self.assertEqual(db.fetchval.await_args.args[1],200)

    async def test_preview_already_queued_is_reused_without_r2_request(self):
        g,db,r2=self.fake(state="preview")
        with patch.object(on_demand,"request",new=AsyncMock(return_value={
            "available":False})) as request:
            response=await on_demand.report_playback_failure(g,4321,200)
        self.assertEqual(response,{"ok":True,"requeued":True})
        r2.object_size.assert_not_awaited()
        db.fetchval.assert_not_awaited()
        request.assert_awaited_once()

    async def test_transient_r2_error_does_not_mark_video_corrupt(self):
        g,db,r2=self.fake()
        r2.object_size.side_effect=OSError("temporary network outage")
        with patch.object(on_demand,"request",new=AsyncMock()) as request:
            result=await on_demand.report_playback_failure(g,4321,200)
        self.assertEqual(result,{"ok":True,"requeued":False})
        db.fetchval.assert_not_awaited()
        request.assert_not_awaited()

    async def test_unauthorized_caller_can_neither_check_nor_requeue(self):
        g,db,r2=self.fake(permitted=False)
        with self.assertRaises(web.HTTPForbidden):
            await on_demand.report_playback_failure(g,4321,200)
        r2.object_size.assert_not_awaited()
        db.fetchval.assert_not_awaited()

    async def test_non_video_rejected(self):
        g,db,r2=self.fake()
        db.fetchrow.return_value={**media(),"kind":"photo"}
        with self.assertRaises(web.HTTPBadRequest):
            await on_demand.report_playback_failure(g,4321,200)
        r2.object_size.assert_not_awaited()


if __name__=="__main__":
    unittest.main()
