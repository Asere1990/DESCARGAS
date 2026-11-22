import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from web_galerias import interval_import as intervals


async def messages(*items):
    for item in items:
        yield item


class IntervalImportTests(IsolatedAsyncioTestCase):
    def test_telegram_links_resolve_public_private_and_topic_message_ids(self):
        self.assertEqual(intervals.message_link("https://t.me/MiGrupo/20"), ("migrupo", 20))
        self.assertEqual(intervals.message_link("https://t.me/c/12345/5/20?single"), (-10012345, 20))
        for invalid in ("https://t.me/+Invite", "https://example.com/c/12345/20",
                        "https://t.me/c/12345/0", "https://t.me/MiGrupo/9999999999"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                intervals.message_link(invalid)

    def test_protected_and_ephemeral_messages_are_ineligible(self):
        for metadata in ({"noforwards": True}, {"ttl_period": 60},
                         {"media": SimpleNamespace(ttl_seconds=1)}):
            self.assertIsNone(intervals.media_kind(SimpleNamespace(**metadata)))

    async def test_copy_resumes_with_exclusive_cursor_and_inclusive_end(self):
        entity = object()
        message = SimpleNamespace(id=21)
        client = SimpleNamespace(iter_messages=lambda *args, **kwargs: messages(message))
        with patch.object(client, "iter_messages", wraps=client.iter_messages) as iterate:
            db = SimpleNamespace(fetchrow=AsyncMock(return_value={"state": "ready"}),
                                 fetchval=AsyncMock(return_value="ready"), execute=AsyncMock())
            gallery = SimpleNamespace(db=lambda: db)
            job = {"id": 2, "peer": -10012345, "source": 9, "revision": 3,
                   "cursor": 20, "last_message": 30}

            with (patch.object(intervals, "resolve_peer", AsyncMock(return_value=entity)),
                  patch.object(intervals, "media_kind", return_value="photo"),
                  patch.object(intervals.media_text, "capture_reply", AsyncMock()),
                  patch.object(intervals.media_text, "enqueue", AsyncMock()),
                  patch("web_galerias.sync.catalog", AsyncMock()),
                  patch("web_galerias.sync.backup_one", AsyncMock()) as backup):
                await intervals.copy_interval(gallery, job, client, AsyncMock(), [asyncio.Lock()])
            iterate.assert_called_once_with(entity, reverse=True, min_id=20, max_id=31)
            backup.assert_not_awaited()
            self.assertEqual(db.execute.await_args.args[1:], (2, 3, 21, 1))
