"""Regression tests for cookie-free, expiring Safari Stars download capabilities."""
import json
import time
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from aiohttp import web
from cryptography.fernet import Fernet

from descargas_app import panel_native_paid as native


class FakePanel:
    def __init__(self):
        self.cipher = Fernet(Fernet.generate_key())
        self.s = N(
            bot_id=7,
            url="https://descargas.example",
            repo=N(
                account=AsyncMock(return_value={"session": b"encrypted", "generation": 3}),
                setting=AsyncMock(return_value="session-v1"),
            ),
        )
        self.media = AsyncMock(return_value="streaming-response")

    def pack(self, purpose, **values):
        return self.cipher.encrypt(
            json.dumps({"purpose": purpose, "bot": 7, **values}).encode()
        ).decode()

    def unpack(self, token, purpose):
        try:
            result = json.loads(self.cipher.decrypt(token.encode()))
            if result["purpose"] != purpose or result["bot"] != 7:
                raise ValueError()
            return result
        except Exception:
            raise ValueError("Invalid token") from None


class NativeStarsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.panel = FakePanel()
        self.peer = self.panel.pack(
            "peer", actor=111, uid=22, generation=3,
            kind="channel", id=55, hash=77,
        )
        self.url = native.issue(
            self.panel, 111, 22, 3, -(1000000000000 + 55),
            self.peer, "purchase-tx-1", 0, "session-v1",
        )
        self.token = parse_qs(urlsplit(self.url).query)["ticket"][0]

    def request(self, method="GET", token=None):
        return N(method=method, query={"ticket": token or self.token},
                 headers={"Range": "bytes=1048576-2097151"})

    async def test_download_link_works_without_panel_cookie_and_preserves_range(self):
        with patch.object(native, "authorized", return_value=True), \
                patch.object(native, "require_account", new_callable=AsyncMock) as allowed:
            result = await native.serve(self.panel, self.request())
        self.assertEqual(result, "streaming-response")
        allowed.assert_awaited_with(self.panel.s, 22, 111)
        self.panel.media.assert_awaited_once()
        args, kwargs = self.panel.media.await_args
        self.assertEqual(args[1:4], (111, 22, self.peer))
        self.assertEqual(kwargs["index"], 0)
        self.assertTrue(kwargs["native_download"])
        purchase = self.panel.unpack(kwargs["purchase"], "purchase")
        self.assertEqual(purchase["transaction"], "purchase-tx-1")
        self.assertEqual(purchase["source"], -(1000000000000 + 55))

    async def test_head_is_authorized_and_delegated_without_cookie(self):
        with patch.object(native, "authorized", return_value=True), \
                patch.object(native, "require_account", new_callable=AsyncMock):
            result = await native.serve(self.panel, self.request(method="HEAD"))
        self.assertEqual(result, "streaming-response")
        self.assertEqual(self.panel.media.await_args.args[0].method, "HEAD")

    async def test_revoked_panel_session_prevents_native_export(self):
        self.panel.s.repo.setting.return_value = "revoked-v2"
        with patch.object(native, "authorized", return_value=True), \
                patch.object(native, "require_account", new_callable=AsyncMock):
            with self.assertRaises(web.HTTPNotFound):
                await native.serve(self.panel, self.request())
        self.panel.media.assert_not_awaited()

    async def test_unlinked_or_replaced_account_prevents_export(self):
        self.panel.s.repo.account.return_value = {"session": None, "generation": 4}
        with patch.object(native, "authorized", return_value=True), \
                patch.object(native, "require_account", new_callable=AsyncMock):
            with self.assertRaises(web.HTTPNotFound):
                await native.serve(self.panel, self.request())
        self.panel.media.assert_not_awaited()

    async def test_unauthorized_actor_prevents_export(self):
        with patch.object(native, "authorized", return_value=False):
            with self.assertRaises(web.HTTPNotFound):
                await native.serve(self.panel, self.request())
        self.panel.media.assert_not_awaited()

    def test_expired_tampered_and_cross_account_tokens_are_rejected(self):
        with self.assertRaises(web.HTTPNotFound):
            native.validate(self.panel, self.token, now=int(time.time()) + native.TICKET_SECONDS + 2)
        with self.assertRaises(web.HTTPNotFound):
            native.validate(self.panel, self.token[:-2] + "zz")
        foreign = self.panel.pack(
            "native-stars-file", actor=999, uid=22, generation=3,
            source=-(1000000000000 + 55), peer=self.peer,
            transaction="purchase-tx-1", index=0, version="session-v1",
            issued=int(time.time()), expires=int(time.time()) + 3600,
        )
        with self.assertRaises(web.HTTPNotFound):
            native.validate(self.panel, foreign)

    def test_link_expires_exactly_once_even_on_boundary(self):
        with patch.object(native.time, "time", return_value=1000):
            link = native.issue(
                self.panel, 111, 22, 3, -(1000000000000 + 55),
                self.peer, "purchase-tx-1", 0, "session-v1",
            )
        token = parse_qs(urlsplit(link).query)["ticket"][0]
        self.assertEqual(
            self.panel.unpack(token, "native-stars-file")["expires"],
            1000 + native.TICKET_SECONDS,
        )
        self.assertEqual(native.validate(self.panel, token, now=1000)["uid"], 22)


if __name__ == "__main__":
    unittest.main()
