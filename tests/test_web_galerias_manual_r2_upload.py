"""Focused regression checks for isolated manual R2 gallery uploads."""
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
