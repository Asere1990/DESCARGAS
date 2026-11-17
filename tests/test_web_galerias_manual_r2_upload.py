"""Focused regression checks for isolated manual R2 gallery uploads."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiohttp import web
from web_galerias import manual_upload as manual


class ValidationTests(unittest.TestCase):
    def test_accept_photo_and_gigabyte_video(self):
        self.assertEqual(manual.parse_filename("video.MP4")[2:], ("video/mp4", "video"))
        self.assertEqual(manual.parse_filename("album/photo.HEIC")[2:],
                         ("image/heic", "photo"))
        self.assertEqual(manual.parse_filename("clip.MOV")[2:],
                         ("video/quicktime", "video"))
        self.assertGreaterEqual(manual.MAX_SIZE, 1024 ** 3)
        self.assertGreaterEqual(manual.PART_SIZE, 5 * 1024 * 1024)

    def test_reject_non_media(self):
        for value in ("danger.exe", "../page.html", "test.svg", "sound.mp3"):
            with self.subTest(value=value), self.assertRaises(web.HTTPBadRequest):
                manual.parse_filename(value)

    def test_file_names_are_basename_only(self):
        self.assertEqual(manual.parse_filename(r"folder\safe.jpg")[0], "safe.jpg")
        with self.assertRaises(web.HTTPBadRequest):
            manual.parse_filename("")

    def test_upload_ids_are_unforgeable_uuid_shape(self):
        with self.assertRaises(web.HTTPBadRequest):
            manual.upload_uuid("../../other")
        with self.assertRaises(web.HTTPBadRequest):
            manual.positive(-5, "Archivo")


class FakeS3:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.writes = []

    def get_bucket_cors(self, **kwargs):
        return {"CORSRules": list(self.existing)}

    def put_bucket_cors(self, **kwargs):
        self.writes.append(kwargs["CORSConfiguration"]["CORSRules"])

    def generate_presigned_url(self, method, Params, ExpiresIn, HttpMethod):
        return "https://example.r2.cloudflarestorage.com/upload"

class FakeStorage:
    bucket = "galeria"
    ready = True

    def __init__(self, client):
        self.client = client

    def _client(self):
        return self.client

class FakeGallery:
    def __init__(self, client):
        self.storage = FakeStorage(client)


class CorsTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_cors_is_left_unchanged(self):
        old = {"AllowedOrigins": ["https://cloudflare-t2gf.onrender.com"],
               "AllowedMethods": ["PUT"], "AllowedHeaders": ["Content-Type"]}
        client = FakeS3([old])
        g = FakeGallery(client)
        with patch.object(manual, "trusted_origins",
                          return_value=["https://cloudflare-t2gf.onrender.com"]):
            self.assertTrue(await manual.cors_ready(g))
        self.assertEqual(client.writes, [])

    async def test_new_origin_appended_not_replacing_existing_policy(self):
        old = {"AllowedOrigins": ["https://old.example.com"],
               "AllowedMethods": ["GET"], "AllowedHeaders": ["Authorization"]}
        client = FakeS3([old])
        g = FakeGallery(client)
        with patch.object(manual, "trusted_origins",
                          return_value=["https://cloudflare-t2gf.onrender.com"]):
            self.assertTrue(await manual.cors_ready(g))
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.writes[0][0], old)
        self.assertEqual(client.writes[0][1]["AllowedOrigins"],
                         ["https://cloudflare-t2gf.onrender.com"])
        self.assertEqual(client.writes[0][1]["AllowedMethods"], ["PUT"])

    async def test_signed_urls_have_one_object_scope(self):
        client = FakeS3()
        g = FakeGallery(client)
        row = {"id": "1", "status": "uploading", "object_key": "manual/1/uuid/original.mp4",
               "thumb_key": "manual/1/uuid/thumb.jpg", "multipart_id": None,
               "mime": "video/mp4", "media_id": None}
        out = await manual.describe(g, row)
        self.assertIn("thumb_url", out)
        self.assertIn("url", out)
        self.assertFalse(out["multipart"])




class RelayRequest:
    def __init__(self, upload, segment, body, length=None):
        self.match_info = {"upload": upload, "segment": segment}
        self.content_length = len(body) if length is None else length
        self.content = self
        self.body = body

    async def iter_chunked(self, size):
        for index in range(0, len(self.body), size):
            yield self.body[index:index + size]


class RelayS3(FakeS3):
    def __init__(self):
        super().__init__()
        self.received = []

    def put_object(self, **kwargs):
        self.received.append(("put", kwargs))
        return {"ETag": '"test"'}

    def upload_part(self, **kwargs):
        self.received.append(("part", kwargs))
        return {"ETag": '"test-part"'}


class CorsFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_cors_access_denied_keeps_upload_available_via_relay(self):
        from botocore.exceptions import ClientError

        class DeniedS3(RelayS3):
            def get_bucket_cors(self, **kwargs):
                raise ClientError({"Error": {"Code": "AccessDenied",
                                             "Message": "No CORS permission"}},
                                  "GetBucketCors")
        g = FakeGallery(DeniedS3())
        result = await manual.info(g)
        self.assertTrue(result["ready"])
        self.assertEqual(result["transport"], "relay")
        self.assertEqual(result["max_bytes"], 2 * 1024 ** 3)
        self.assertEqual(result["part_size"], 16 * 1024 ** 2)

    async def test_existing_cors_keeps_direct_browser_uploads(self):
        client = RelayS3()
        client.existing = [{"AllowedOrigins": ["https://cloudflare-t2gf.onrender.com"],
                            "AllowedMethods": ["PUT"],
                            "AllowedHeaders": ["Content-Type"]}]
        result = await manual.info(FakeGallery(client))
        self.assertEqual(result["transport"], "direct")
        self.assertEqual(client.writes, [])

    async def test_admin_relay_uploads_exact_parts_without_whole_video(self):
        s3 = RelayS3()
        g = FakeGallery(s3)
        row = {"status": "uploading", "bytes": 11, "multipart_id": "multi-id",
               "object_key": "manual/7/uuid/original.mp4",
               "thumb_key": "manual/7/uuid/thumb.jpg", "mime": "video/mp4"}
        with patch.object(manual, "PART_SIZE", 8), patch.object(
                manual, "row_for", new_callable=AsyncMock, return_value=row) as owner:
            first = await manual.receive_bytes(
                g, 17, RelayRequest("valid-uuid", "1", b"12345678"))
            second = await manual.receive_bytes(
                g, 17, RelayRequest("valid-uuid", "2", b"abc"))
        self.assertEqual(first, {"ok": True, "bytes": 8})
        self.assertEqual(second, {"ok": True, "bytes": 3})
        self.assertEqual([i[1]["PartNumber"] for i in s3.received], [1, 2])
        self.assertEqual([i[1]["Body"] for i in s3.received],
                         [b"12345678", b"abc"])
        self.assertEqual(owner.await_count, 2)
        self.assertEqual(owner.await_args.args[1], 17)

    async def test_relay_rejects_partial_part_without_uploading(self):
        s3 = RelayS3()
        g = FakeGallery(s3)
        row = {"status": "uploading", "bytes": 11, "multipart_id": "multi-id",
               "object_key": "manual/7/uuid/original.mp4",
               "thumb_key": "manual/7/uuid/thumb.jpg", "mime": "video/mp4"}
        with patch.object(manual, "PART_SIZE", 8), patch.object(
                manual, "row_for", new_callable=AsyncMock, return_value=row):
            with self.assertRaises(web.HTTPBadRequest):
                await manual.receive_bytes(
                    g, 17, RelayRequest("valid-uuid", "1", b"1234"))
            with self.assertRaises(web.HTTPBadRequest):
                await manual.receive_bytes(
                    g, 17, RelayRequest("valid-uuid", "9", b"12345678"))
        self.assertEqual(s3.received, [])

    async def test_relay_single_file_and_thumbnail_use_private_keys(self):
        s3 = RelayS3()
        g = FakeGallery(s3)
        row = {"status": "uploading", "bytes": 6, "multipart_id": None,
               "object_key": "manual/7/uuid/original.jpg",
               "thumb_key": "manual/7/uuid/thumb.jpg", "mime": "image/jpeg"}
        with patch.object(manual, "row_for", new_callable=AsyncMock,
                          return_value=row):
            await manual.receive_bytes(
                g, 17, RelayRequest("valid-uuid", "thumb", b"jpeg"))
            await manual.receive_bytes(
                g, 17, RelayRequest("valid-uuid", "file", b"123456"))
        self.assertEqual([record[0] for record in s3.received], ["put", "put"])
        self.assertEqual([record[1]["Key"] for record in s3.received],
                         [row["thumb_key"], row["object_key"]])


class CaptionFakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.sql_reads = []
        self.sql_writes = []

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def fetch(self, sql, *args):
        self.sql_reads.append((sql, args))
        return self.rows

    async def execute(self, sql, *args):
        self.sql_writes.append((sql, args))
        return "UPDATE " + str(len(self.rows))


class CaptionFakeGallery:
    def __init__(self, rows):
        self.connection = CaptionFakeConnection(rows)

    def db(self):
        return self.connection


class ManualBatchCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_caption_is_searchable_for_every_photo_and_video(self):
        import uuid
        one, two = uuid.uuid4(), uuid.uuid4()
        g = CaptionFakeGallery([
            {"media_id": 11, "folder": 7, "filename": "foto-1.jpg"},
            {"media_id": 12, "folder": 7, "filename": "video-1.mp4"},
        ])
        answer = await manual.caption(
            g, 1234, {"uploads": [str(one), str(two)], "caption": "La Habana — Playa"})
        self.assertEqual(answer, {"ok": True, "updated": 2})
        read_sql, (ids, actor) = g.connection.sql_reads[0]
        self.assertEqual(ids, [one, two])
        self.assertEqual(actor, 1234)
        self.assertIn("u.actor=$2", read_sql)
        self.assertIn("u.status='ready'", read_sql)
        self.assertIn("m.state='ready'", read_sql)
        self.assertIn("m.message<0", read_sql)
        self.assertIn("m.file_key=u.object_key", read_sql)
        sql, (media, caption, search) = g.connection.sql_writes[0]
        self.assertIn("UNNEST", sql)
        self.assertEqual(media, [11, 12])
        self.assertEqual(caption, "La Habana — Playa")
        self.assertEqual(search, [
            "la habana — playa foto-1.jpg",
            "la habana — playa video-1.mp4",
        ])

    async def test_missing_or_foreign_file_rejects_entire_batch(self):
        import uuid
        g = CaptionFakeGallery([{"media_id": 11, "folder": 7, "filename": "foto.jpg"}])
        with self.assertRaises(web.HTTPConflict):
            await manual.caption(g, 1234, {
                "uploads": [str(uuid.uuid4()), str(uuid.uuid4())],
                "caption": "Pinar del Río",
            })
        self.assertEqual(g.connection.sql_writes, [])

    async def test_mixed_folders_rejected_without_writes(self):
        import uuid
        g = CaptionFakeGallery([
            {"media_id": 11, "folder": 7, "filename": "foto.jpg"},
            {"media_id": 12, "folder": 8, "filename": "video.mp4"},
        ])
        with self.assertRaises(web.HTTPBadRequest):
            await manual.caption(g, 1234, {
                "uploads": [str(uuid.uuid4()), str(uuid.uuid4())],
                "caption": "un caption",
            })
        self.assertEqual(g.connection.sql_writes, [])

    async def test_empty_caption_requires_explicit_skip(self):
        import uuid
        g = CaptionFakeGallery([])
        for text in ("", "   ", "x" * 1501):
            with self.subTest(text=text[:10]), self.assertRaises(web.HTTPBadRequest):
                await manual.caption(g, 1234, {
                    "uploads": [str(uuid.uuid4())], "caption": text})
        self.assertEqual(g.connection.sql_reads, [])

    async def test_repeated_upload_identifier_is_rejected(self):
        import uuid
        g = CaptionFakeGallery([])
        ident = str(uuid.uuid4())
        with self.assertRaises(web.HTTPBadRequest):
            await manual.caption(g, 1234, {
                "uploads": [ident, ident], "caption": "La Habana"})
        self.assertEqual(g.connection.sql_reads, [])


if __name__ == "__main__":
    unittest.main()
