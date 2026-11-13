"""The gallery entry bot can rotate without account or payment operations."""
import unittest
from unittest.mock import patch
from web_galerias.public_entry import PublicGalleryEntry

TOKEN = "12345:" + ("x" * 35)


class TestPublicGalleryBot(unittest.IsolatedAsyncioTestCase):
    async def test_missing_public_bot_keeps_existing_system(self):
        entry = PublicGalleryEntry("")
        self.assertEqual(entry.authorized_token(), "")
        self.assertIsNone(await entry.link())
        self.assertEqual(await entry.details(), {"configured": False, "ready": False, "url": None})

    async def test_cannot_use_original_or_secondary_bots(self):
        entry = PublicGalleryEntry(TOKEN)
        self.assertEqual(entry.authorized_token(TOKEN), "")
        self.assertEqual(entry.authorized_token("another", TOKEN), "")

    async def test_public_bot_signature_uses_original_telegram_verifier(self):
        """Only a real, signed Telegram user may access paid galleries."""
        import os
        from types import SimpleNamespace
        from descargas_app import security
        from web_galerias.core import GalleryCore

        protected = "98765:" + ("z" * 35)
        gallery = SimpleNamespace(s=SimpleNamespace(
            token=protected, public_entry=PublicGalleryEntry(TOKEN)))
        request = SimpleNamespace(headers={"X-Telegram-Init-Data": "signed"})
        attempted = []

        def verify(raw, token):
            attempted.append(token)
            if token == TOKEN and raw == "signed":
                return 7536216348
            raise ValueError("invalid for this bot")

        with patch.dict(os.environ, {"GALERIAS_BOT_TOKENS": ""}):
            with patch.object(security, "validate_init_data", side_effect=verify):
                self.assertEqual(await GalleryCore.auth(gallery, request), 7536216348)
        self.assertEqual(attempted, [protected, TOKEN])

    async def test_requires_botfather_main_miniapp(self):
        class Response:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            status = 200
            async def json(self):
                return {"ok": True, "result": {"is_bot": True, "username": "NewGalleryBot"}}
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def get(self, *args, **kwargs): return Response()
        with patch("web_galerias.public_entry.ClientSession", Session):
            self.assertIsNone(await PublicGalleryEntry(TOKEN).link())

    async def test_single_token_generates_link_without_extra_settings(self):
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def json(self):
                return {"ok": True, "result": {
                    "is_bot": True, "username": "PublicGalleryBot",
                    "has_main_web_app": True}}
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def get(self, *args, **kwargs): return Response()
        with patch("web_galerias.public_entry.ClientSession", Session):
            entry = PublicGalleryEntry(TOKEN)
            self.assertEqual(await entry.link(),
                             "https://t.me/PublicGalleryBot?startapp")
            self.assertEqual((await entry.details())["ready"], True)


if __name__ == "__main__":
    unittest.main()
