"""Bounded HTTP range and HEAD tests for native Safari Stars download responses."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from telethon import types

from descargas_app import web_panel, panel_purchases


class FakeStreamResponse:
    def __init__(self, status, headers):
        self.status = status
        self.headers = headers
        self.writes = []
        self.ended = False

    async def prepare(self, request):
        return self

    async def write(self, data):
        self.writes.append(bytes(data))

    async def write_eof(self):
        self.ended = True


class DownloadIterator:
    def __init__(self, chunk):
        self.chunk = chunk
        self.closed = False

    def __aiter__(self):
        return self.stream()

    async def stream(self):
        yield self.chunk

    async def close(self):
        self.closed = True


class NativeStarsStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.size = 1600000000  # Metadata only; no 1.6 GB allocation.
        document = types.Document(
            id=123, access_hash=2, file_reference=b"ref",
            date=datetime.now(timezone.utc), mime_type="video/mp4",
            size=self.size, thumbs=[], dc_id=2,
            attributes=[types.DocumentAttributeVideo(duration=1800, w=1080, h=1920)],
        )
        self.video = N(
            media=types.MessageMediaDocument(document=document),
            document=document, photo=None, ttl_period=None,
        )
        self.iterator = DownloadIterator(b"Z" * 8192)
        self.seen = []

        def iter_download(media, offset, request_size, chunk_size):
            self.seen.append((offset, request_size, chunk_size))
            return self.iterator

        self.client = N(iter_download=iter_download)
        self.entity = types.User(id=55, access_hash=80, first_name="Fuente")
        self.panel = N(
            s=N(bot_id=7, repo=N()),
            paid_transfers=asyncio.Semaphore(1),
            target=AsyncMock(return_value=self.entity),
        )

        @asynccontextmanager
        async def parallel(actor, uid):
            self.assertEqual((actor, uid), (111, 22))
            yield self.client, 3

        self.panel.paid_stream_account = parallel
        self.panel.account = lambda *args, **kwargs: self.fail(
            "Native downloads must never take the normal account lock")
        self.resolver = patch.object(panel_purchases, "resolve", new_callable=AsyncMock)
        self.resolve_mock = self.resolver.start()
        self.resolve_mock.return_value = self.video
        self.addCleanup(self.resolver.stop)
        self.auth = patch.object(web_panel, "authorized", return_value=True)
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.guards = [
            patch.object(web_panel, "require_account", new_callable=AsyncMock),
            patch.object(web_panel, "require_source", new_callable=AsyncMock),
        ]
        for p in self.guards:
            p.start()
            self.addCleanup(p.stop)

    def request(self, method="GET", range_header=None):
        return N(method=method, query={}, headers={"Range": range_header}
                 if range_header else {})

    async def test_head_reports_real_size_without_downloading_video_bytes(self):
        response = await web_panel.TelegramPanel.media(
            self.panel, self.request("HEAD"), 111, 22, "peer",
            1, purchase="paid", index=0, native_download=True,
        )
        self.assertIsInstance(response, web.Response)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Length"], str(self.size))
        self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        self.assertEqual(self.seen, [])
        self.resolve_mock.assert_awaited_once()

    async def test_get_reads_only_requested_range_and_returns_valid_206(self):
        with patch.object(web_panel.web, "StreamResponse", FakeStreamResponse):
            response = await web_panel.TelegramPanel.media(
                self.panel, self.request("GET", "bytes=4096-8191"), 111, 22,
                "peer", 1, purchase="paid", index=0, native_download=True,
            )
        self.assertEqual(response.status, 206)
        self.assertEqual(response.headers["Content-Range"],
                         f"bytes 4096-8191/{self.size}")
        self.assertEqual(response.headers["Content-Length"], "4096")
        self.assertEqual(response.writes, [b"Z" * 4096])
        self.assertTrue(response.ended)
        self.assertTrue(self.iterator.closed)
        self.assertEqual(self.seen[0][0], 4096)

    async def test_invalid_range_fails_before_starting_telegram_stream(self):
        with self.assertRaises(web.HTTPRequestRangeNotSatisfiable):
            await web_panel.TelegramPanel.media(
                self.panel, self.request("GET", f"bytes={self.size}-"),
                111, 22, "peer", 1,
                purchase="paid", index=0, native_download=True,
            )
        self.assertEqual(self.seen, [])


    async def test_short_telegram_source_resumes_from_last_byte_without_http_eof(self):
        from descargas_app import panel_native_stream
        first = DownloadIterator(b"Z" * 8192)
        second = DownloadIterator(b"Z" * 4096)
        self.seen.clear()
        def limited(media, offset, request_size, chunk_size):
            self.seen.append((offset, request_size, chunk_size))
            return first if len(self.seen) == 1 else second
        self.client.iter_download = limited
        with patch.object(web_panel.web, "StreamResponse", FakeStreamResponse), \
             patch.object(panel_native_stream.asyncio, "sleep", new_callable=AsyncMock):
            response = await web_panel.TelegramPanel.media(
                self.panel, self.request("GET", "bytes=4096-16383"),
                111, 22, "peer", 1, purchase="paid",
                index=0, native_download=True,
            )
        self.assertEqual(response.status, 206)
        self.assertEqual([len(v) for v in response.writes], [8192, 4096])
        self.assertEqual(sum(map(len, response.writes)), 12288)
        self.assertEqual([r[0] for r in self.seen], [4096, 12288])
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertTrue(response.ended)

    async def test_repeated_short_telegram_source_never_marks_http_complete(self):
        from descargas_app import panel_native_stream
        self.seen.clear()
        def empty(media, offset, request_size, chunk_size):
            self.seen.append(offset)
            return DownloadIterator(b"")
        self.client.iter_download = empty
        created = []
        def make_response(status, headers):
            obj = FakeStreamResponse(status, headers)
            created.append(obj)
            return obj
        with patch.object(web_panel.web, "StreamResponse", make_response), \
             patch.object(panel_native_stream.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaises(ConnectionError):
                await web_panel.TelegramPanel.media(
                    self.panel, self.request("GET", "bytes=4096-8191"),
                    111, 22, "peer", 1, purchase="paid",
                    index=0, native_download=True,
                )
        self.assertEqual(len(self.seen), panel_native_stream.MAX_EARLY_EOF_RETRIES + 1)
        self.assertFalse(created[0].ended)
        self.assertEqual(created[0].writes, [])

    async def test_client_disconnect_does_not_silently_report_complete(self):
        class ConnectionLost(FakeStreamResponse):
            async def write(self, data):
                self.writes.append(bytes(data))
                raise ConnectionResetError("client closed after first chunk")
        created = []
        def make_response(status, headers):
            obj = ConnectionLost(status, headers)
            created.append(obj)
            return obj
        with patch.object(web_panel.web, "StreamResponse", make_response):
            with self.assertRaises(ConnectionResetError):
                await web_panel.TelegramPanel.media(
                    self.panel, self.request("GET", "bytes=4096-16383"),
                    111, 22, "peer", 1, purchase="paid",
                    index=0, native_download=True,
                )
        self.assertFalse(created[0].ended)
        self.assertTrue(self.iterator.closed)


if __name__ == "__main__":
    unittest.main()
