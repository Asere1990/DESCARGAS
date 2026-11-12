"""On-demand priority: auth, deduplication, safe source sharing and expiry."""
import asyncio
import os
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch

from aiohttp import web
from cryptography.fernet import Fernet

from web_galerias import on_demand
from web_galerias.sync import PauseGuard, PauseRequested


def media(state="preview"):
    return {
        "id": 55, "folder": 7, "message": 105,
        "kind": "video", "mime": "video/mp4", "size": 512,
        "filename": "video.mp4", "state": state,
        "file_key": "folders/7/105/original.mp4" if state == "ready" else None,
        "thumb_key": "folders/7/105/thumb.jpg",
        "source_type": "video", "source_checks": 0,
        "active": True, "peer": -100123456,
        "preferred_uid": None, "source_uid": 123456,
    }


class FakeDB:
    def __init__(self, item=None):
        self.item = media() if item is None else item
        self.job = None
        self.inserts = 0
        self.commands = []
        self.queue_rows = []

    async def fetchrow(self, sql, *args):
        if "FROM wg_media_priority" in sql:
            return self.job
        if "FROM wg_media m" in sql:
            return self.item
        return None

    async def fetchval(self, sql, *args):
        if "COUNT(*) FROM wg_media_priority WHERE requester" in sql:
            return 1 if self.job else 0
        if "COUNT(*) FROM wg_media_priority" in sql:
            return 1 if self.job else 0
        if "SELECT state='ready'" in sql:
            return True
        if "SELECT EXISTS(" in sql:
            return False
        return None

    async def execute(self, sql, *args):
        self.commands.append((sql, args))
        if "INSERT INTO wg_media_priority" in sql and not self.job:
            self.inserts += 1
            self.job = {"state": "queued", "next_attempt": 0}
        if "DELETE FROM wg_media_priority" in sql:
            self.job = None

    async def fetch(self, sql, *args):
        return list(self.queue_rows)

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield


def gallery(db=None):
    db = db or FakeDB()
    key = Fernet.generate_key()
    task = N(done=lambda: False)
    g = N(db=lambda: db, storage=N(ready=True), priority_task=task,
          closed=False, permitted=AsyncMock(return_value=True),
          s=N(cipher=Fernet(key)))
    return g, db


class PriorityAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_authorization_before_queue_or_status(self):
        g, db = gallery()
        g.permitted.return_value = False
        with patch.dict(os.environ, {"GALERIAS_SYNC_ENABLED": "1"}):
            with self.assertRaises(web.HTTPForbidden):
                await on_demand.request(g, 999, 55)
            with self.assertRaises(web.HTTPForbidden):
                await on_demand.status(g, 999, 55)
        self.assertEqual(db.inserts, 0)

    async def test_cannot_queue_unknown_or_hidden_media(self):
        g, db = gallery()
        db.item = None
        with patch.dict(os.environ, {"GALERIAS_SYNC_ENABLED": "1"}):
            with self.assertRaises(web.HTTPNotFound):
                await on_demand.request(g, 999, 55)
        db.item = media()
        db.item["active"] = False
        with patch.dict(os.environ, {"GALERIAS_SYNC_ENABLED": "1"}):
            with self.assertRaises(web.HTTPNotFound):
                await on_demand.request(g, 999, 55)

    async def test_video_request_is_durable_and_deduplicated(self):
        g, db = gallery()
        with patch.dict(os.environ, {"GALERIAS_SYNC_ENABLED": "1"}):
            first = await on_demand.request(g, 999, 55)
            again = await on_demand.request(g, 999, 55)
        self.assertEqual(db.inserts, 1)
        self.assertEqual(first["priority_state"], "queued")
        self.assertFalse(first["available"])
        self.assertEqual(again["media"], 55)
        self.assertIn("/thumb?t=", again["thumb"])
        self.assertIsNone(first["view"])

    async def test_ready_video_does_not_start_new_transfer(self):
        g, db = gallery(FakeDB(media("ready")))
        with patch.dict(os.environ, {"GALERIAS_SYNC_ENABLED": "1"}):
            result = await on_demand.request(g, 999, 55)
        self.assertTrue(result["available"])
        self.assertIn("/view?t=", result["view"])
        self.assertEqual(db.inserts, 0)

    async def test_globally_disabled_priority_cannot_start_session(self):
        g, db = gallery()
        with patch.dict(os.environ, {"GALERIAS_SYNC_ENABLED": "0"}):
            with self.assertRaises(web.HTTPServiceUnavailable):
                await on_demand.request(g, 999, 55)
        self.assertEqual(db.inserts, 0)

    async def test_status_waiting_exposes_delay_without_errors(self):
        g, db = gallery()
        db.job = {"state": "queued", "next_attempt": time.time()+45}
        with patch.dict(os.environ, {"GALERIAS_SYNC_ENABLED": "1"}):
            result = await on_demand.status(g, 999, 55)
        self.assertEqual(result["priority_state"], "waiting")
        self.assertGreaterEqual(result["retry_after"], 43)
        self.assertLessEqual(result["retry_after"], 45)


class PriorityWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_lock_and_streaming_backup_used(self):
        g, db = gallery()
        db.job = {"state": "running", "next_attempt": 0}
        g.s.datos = N(ready=False)
        client = N(get_messages=AsyncMock(return_value=N(id=105)))

        @asynccontextmanager
        async def connected(service, uid):
            self.assertIs(service, g.s)
            self.assertEqual(uid, 123456)
            yield client

        with patch.dict(os.environ, {"GALERIAS_SYNC_ENABLED": "1"}), \
             patch("descargas_app.account_chat.connected", connected), \
             patch("web_galerias.sync.choose_account", new=AsyncMock(return_value=123456)), \
             patch("web_galerias.sync.source_free", new=AsyncMock(return_value=True)), \
             patch("web_galerias.sync.find_dialog", new=AsyncMock(return_value="peer")), \
             patch("web_galerias.sync.inspect_source", new=AsyncMock(return_value=True)), \
             patch("web_galerias.sync.backup_one", new_callable=AsyncMock) as backup, \
             patch("web_galerias.sync.PauseGuard", autospec=True) as guard:
            guard.return_value = AsyncMock()
            await on_demand._process(g, 55)
        backup.assert_awaited_once()
        self.assertEqual(backup.await_args.args[0:2], (g, 7))
        self.assertTrue(any("DELETE FROM wg_media_priority" in sql
                            for sql, _ in db.commands))

    async def test_busy_descargas_source_never_connects(self):
        g, db = gallery()
        db.job = {"state": "running", "next_attempt": 0}
        with patch.dict(os.environ, {"GALERIAS_SYNC_ENABLED": "1"}), \
             patch("web_galerias.sync.choose_account", new=AsyncMock(return_value=123456)), \
             patch("web_galerias.sync.source_free", new=AsyncMock(return_value=False)), \
             patch("descargas_app.account_chat.connected") as connect:
            await on_demand._process(g, 55)
        connect.assert_not_called()
        self.assertTrue(any("UPDATE wg_media_priority" in sql
                            for sql, _ in db.commands))

    async def test_bulk_backup_yields_to_urgent_video_but_urgent_does_not(self):
        g, db = gallery()
        with patch.dict(os.environ, {"GALERIAS_SYNC_ENABLED": "1"}), \
             patch("web_galerias.sync.source_free", new=AsyncMock(return_value=True)), \
             patch("web_galerias.on_demand.has_urgent", new=AsyncMock(return_value=True)):
            with self.assertRaises(PauseRequested):
                await PauseGuard(g, 123456, 7)()
            await PauseGuard(g, 123456, 7, priority=True)()


if __name__ == "__main__":
    unittest.main()
