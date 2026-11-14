"""Burst-load contracts: 250 requests, source isolation and real transfer progress."""
import asyncio
import inspect
import os
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch

from aiohttp import web
from cryptography.fernet import Fernet

from web_galerias import on_demand
from web_galerias.storage import PrivateStorage


def item(mid=55, state="preview"):
    return {
        "id": mid, "folder": 7, "message": mid + 1000,
        "kind": "video", "mime": "video/mp4", "size": 10_000_000,
        "filename": "clip.mp4", "state": state,
        "file_key": f"folders/7/{mid+1000}/original.mp4" if state == "ready" else None,
        "thumb_key": f"folders/7/{mid+1000}/thumb.jpg",
        "source_type": "video", "source_checks": 0,
        "active": True, "peer": -100123456,
        "preferred_uid": None, "source_uid": 123456,
    }


class BurstDB:
    def __init__(self):
        self.jobs = {}
        self.lock = asyncio.Lock()
        self.inserts = 0

    async def fetchrow(self, sql, *args):
        if "FROM wg_media m JOIN wg_folders" in sql:
            return item(int(args[0]))
        if "SELECT state,next_attempt,requested_at" in sql:
            return self.jobs.get(int(args[0]))
        if "FROM wg_media_priority WHERE media" in sql:
            return self.jobs.get(int(args[0]))
        if "COUNT(*) FILTER" in sql:
            stamp = float(args[0])
            return {
                "position": sum(j["state"] == "queued" and j["requested_at"] <= stamp
                                for j in self.jobs.values()),
                "total": sum(j["state"] == "queued" for j in self.jobs.values())
            }
        return None

    async def fetchval(self, sql, *args):
        if "COUNT(*) FROM wg_media_priority WHERE requester" in sql:
            return sum(j["requester"] == args[0] for j in self.jobs.values())
        if "COUNT(*) FROM wg_media_priority" in sql:
            return len(self.jobs)
        return None

    async def execute(self, sql, *args):
        if "INSERT INTO wg_media_priority" in sql and args[0] not in self.jobs:
            self.inserts += 1
            self.jobs[int(args[0])] = {
                "state": "queued", "next_attempt": 0,
                "requested_at": args[2], "requester": args[1],
                "received_bytes": 0, "expected_bytes": 0
            }

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            yield


def gallery(db):
    return N(
        db=lambda: db, storage=N(ready=True), closed=False,
        priority_task=N(done=lambda: False),
        permitted=AsyncMock(return_value=True),
        s=N(cipher=Fernet(Fernet.generate_key()))
    )


class BurstAPI(unittest.IsolatedAsyncioTestCase):
    async def test_two_hundred_fifty_distinct_users_can_queue_without_telegram_sessions(self):
        db = BurstDB()
        g = gallery(db)
        with patch.dict(os.environ, {"GALERIAS_SYNC_ENABLED": "1"}):
            results = await asyncio.gather(*(
                on_demand.request(g, 10_000 + n, 5000 + n)
                for n in range(250)))
            self.assertEqual(len(db.jobs), 250)
            self.assertEqual(db.inserts, 250)
            self.assertTrue(all(r["priority_state"] == "queued" for r in results))
            # The same video is downloaded only once, regardless of viewers.
            duplicate = await on_demand.request(g, 999_999, 5000)
        self.assertEqual(db.inserts, 250)
        self.assertEqual(duplicate["media"], 5000)

    async def test_one_user_cannot_fill_all_slots(self):
        db, uid = BurstDB(), 10101
        g = gallery(db)
        with patch.dict(os.environ, {"GALERIAS_SYNC_ENABLED": "1"}):
            for n in range(on_demand.MAX_USER_PENDING):
                await on_demand.request(g, uid, 8000 + n)
            with self.assertRaises(web.HTTPTooManyRequests):
                await on_demand.request(g, uid, 9000)
        self.assertEqual(len(db.jobs), on_demand.MAX_USER_PENDING)

    def test_progress_is_real_and_hundred_requires_ready_state(self):
        g = gallery(BurstDB())
        pending = item()
        job = {"state": "running", "next_attempt": 0, "received_bytes": 4_200_000,
               "expected_bytes": 10_000_000}
        self.assertEqual(on_demand._result(g, 999, pending, job)["progress_percent"], 42)
        job["received_bytes"] = 10_000_000
        self.assertEqual(on_demand._result(g, 999, pending, job)["progress_percent"], 99)
        self.assertEqual(on_demand._result(g, 999, item(state="ready"))["progress_percent"], 100)
        job["expected_bytes"] = 0
        self.assertIsNone(on_demand._result(g, 999, pending, job)["progress_percent"])

    def test_dispatcher_is_fifo_and_bounded_by_verified_accounts(self):
        code = inspect.getsource(on_demand.run)
        self.assertIn("ORDER BY e.user_round,e.requested_at ASC", code)
        self.assertIn("PARTITION BY m.folder", code)
        self.assertIn("FROM wg_discovery d", code)
        self.assertGreaterEqual(on_demand.MAX_GLOBAL_PENDING, 250)
        self.assertLessEqual(on_demand.MAX_PARALLEL, 8)

    async def test_an_already_verified_datos_account_can_be_used_when_primary_busy(self):
        db, g = BurstDB(), None
        g = gallery(db)
        g.s.datos = N(ready=True)
        g.priority_accounts_in_use = {123456}
        g.priority_source_lock = asyncio.Lock()
        async def read(sql, *args):
            return [{"uid": -654321}]
        db.fetch = read
        with patch("web_galerias.sync.choose_account",
                   new=AsyncMock(return_value=123456)), \
             patch("web_galerias.sync.source_free",
                   new=AsyncMock(return_value=True)):
            uid = await on_demand._select_source(g, {"id": 7, "preferred_uid": None})
        self.assertEqual(uid, -654321)
        # The reservation uses the physical Telegram ID, not signed provider key.
        self.assertIn(654321, g.priority_accounts_in_use)
        self.assertNotIn(-654321, g.priority_accounts_in_use)


class StreamingProgress(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_byte_counter_is_monotonic_and_no_full_video_buffer(self):
        blob = b"x" * (512 * 1024)
        events = []
        calls = []
        class FakeR2:
            def create_multipart_upload(self, **kwargs):
                return {"UploadId": "test"}
            def upload_part(self, **kwargs):
                calls.append(("part", len(kwargs["Body"])))
                return {"ETag": f"tag-{len(calls)}"}
            def complete_multipart_upload(self, **kwargs):
                calls.append(("complete", len(kwargs["MultipartUpload"]["Parts"])))
        store = PrivateStorage()
        store.bucket = "test"
        store._client = lambda: FakeR2()
        store.object_size = AsyncMock(return_value=5 * len(blob))
        async def chunks():
            for _ in range(5):
                yield blob
        async def progress(received):
            events.append(received)
        written = await store.upload_stream(chunks(), "folders/7/5/original.mp4",
                                           "video/mp4", progress=progress)
        self.assertEqual(written, len(blob) * 5)
        self.assertEqual(events, [len(blob)*n for n in range(1, 6)])
        self.assertEqual(sum(n for k, n in calls if k == "part"), written)
        self.assertEqual(calls[-1], ("complete", 1))


if __name__ == "__main__":
    unittest.main()
