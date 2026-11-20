import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from aiohttp import web
from cryptography.fernet import Fernet

from web_galerias import history_import as history


async def iterate(*items):
    for item in items:
        yield item


class HistoryTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = SimpleNamespace(fetchval=AsyncMock(return_value=1),
                                  fetchrow=AsyncMock(return_value={"state": "pending"}),
                                  execute=AsyncMock())
        self.cipher = Fernet(Fernet.generate_key())
        self.client = SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(), log_out=AsyncMock(),
            send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash")),
            sign_in=AsyncMock(return_value=SimpleNamespace(id=20, bot=False)),
            session=SimpleNamespace(save=lambda: "separate-session"),
            get_me=AsyncMock(return_value=SimpleNamespace(id=10)),
            get_messages=AsyncMock(return_value=[SimpleNamespace(id=30)]),
        )
        self.gallery = SimpleNamespace(
            s=SimpleNamespace(cipher=self.cipher, client=lambda _: self.client),
            history_login_lock=asyncio.Lock(), history_logins={}, history_next_code={},
            db=lambda: self.db,
        )

    async def test_admin_links_the_verified_account_under_its_own_telegram_id(self):
        pending = await history.login(self.gallery, 10, "send", {"phone": "+15555555555"})
        code = {"code": "12345", "flow": pending["flow"]}
        with self.assertRaises(web.HTTPBadRequest):
            await history.login(self.gallery, 11, "code", code)
        with self.assertRaises(web.HTTPBadRequest):
            await history.login(self.gallery, 10, "code", {**code, "flow": "old"})
        self.db.execute.assert_not_awaited()
        result = await history.login(self.gallery, 10, "code", code)
        self.assertEqual(result, {"step": "linked", "account": "20"})
        _, uid, encrypted = self.db.execute.await_args.args
        self.assertEqual(uid, 20)
        self.assertEqual(self.cipher.decrypt(encrypted), b"separate-session")

    async def test_login_rejects_bot_accounts(self):
        self.client.sign_in.return_value.bot = True
        pending = await history.login(self.gallery, 10, "send", {"phone": "+15555555555"})
        with self.assertRaises(web.HTTPForbidden):
            await history.login(self.gallery, 10, "code", {"code": "12345", "flow": pending["flow"]})
        self.db.execute.assert_not_awaited()
        self.client.log_out.assert_awaited_once()

    async def test_import_resumes_oldest_first_at_saved_cutoff(self):
        self.db.fetchval.return_value = self.cipher.encrypt(b"separate-session")
        dialog = SimpleNamespace(id=-100123, is_group=True, entity=object(), input_entity="peer")
        self.client.iter_dialogs = lambda: iterate(dialog)
        messages = [SimpleNamespace(id=7, noforwards=True), SimpleNamespace(id=8)]
        ranges = []

        def iter_messages(entity, **kwargs):
            ranges.append((entity, kwargs))
            return iterate(*messages)

        self.client.iter_messages = iter_messages
        job = {"uid": 10, "source": 3, "peer": -100123, "cursor": 6, "stop_at": 9}
        with (patch.object(history, "classify", return_value=("video", "video/mp4", "video")),
              patch("web_galerias.sync.catalog", new_callable=AsyncMock) as catalog,
              patch("web_galerias.sync.backup_one", new_callable=AsyncMock) as backup):
            await history.import_history(self.gallery, job)
        self.assertEqual(ranges, [("peer", {"reverse": True, "min_id": 6, "max_id": 10})])
        self.client.get_messages.assert_not_called()
        catalog.assert_awaited_once_with(self.gallery, 3, messages[1])
        self.assertIs(backup.await_args.args[3], messages[1])
        self.assertEqual(self.db.execute.await_args.args[1:], (3, 8))
