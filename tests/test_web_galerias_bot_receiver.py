"""TOKEN_WEB receive activation: existing hooks are never removed without admin consent."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from web_galerias import bot_feed, admin


class ReceiverStateTests(unittest.TestCase):
    def test_known_webhook_host_is_safe_and_token_never_shown(self):
        address="https://bot123456:SENSITIVE@hook.example.com/tokenSECRET"
        self.assertEqual(bot_feed._safe_webhook_host(address),"hook.example.com")
        g=SimpleNamespace(_bot_feed_receiver_state="webhook_conflict",
                          _bot_feed_webhook_host="hook.example.com",
                          _bot_feed_privacy_disabled=False)
        with patch.object(bot_feed,"token",return_value="123:TOPSECRET"):
            state=bot_feed.receiver_status(g)
        self.assertEqual(state["mode"],"webhook_conflict")
        self.assertEqual(state["webhook_host"],"hook.example.com")
        self.assertFalse(state["privacy_disabled"])
        self.assertNotIn("TOPSECRET",str(state))

    def test_missing_token_marks_disabled(self):
        with patch.object(bot_feed,"token",return_value=""):
            state=bot_feed.receiver_status(SimpleNamespace())
        self.assertEqual(state["mode"],"not_configured")


class ConfirmedActivationTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        client=MagicMock()
        client.__aenter__=AsyncMock(return_value=SimpleNamespace())
        client.__aexit__=AsyncMock(return_value=False)
        return client

    async def test_missing_explicit_confirmation_cannot_delete_any_webhook(self):
        g=SimpleNamespace()
        with patch.object(bot_feed,"telegram",new_callable=AsyncMock) as tg:
            with self.assertRaises(web.HTTPBadRequest):
                await bot_feed.activate_receiver(g,{"confirm":False})
        tg.assert_not_awaited()

    async def test_manual_admin_activation_keeps_telegram_pending_updates(self):
        g=SimpleNamespace(bot_feed_task=None)
        client=self.make_client()
        async def fake_telegram(session, token, method, payload=None):
            if method=="getWebhookInfo":
                return {"url":"https://another-service.example.com/route/SECRET",
                        "pending_update_count":17}
            if method=="deleteWebhook":
                self.assertEqual(payload,{"drop_pending_updates":False})
                return True
            raise AssertionError("Unexpected Bot API method: "+method)
        with patch.object(bot_feed,"token",return_value="123:TOKEN"),patch.object(
            bot_feed.aiohttp,"ClientSession",return_value=client
        ),patch.object(bot_feed,"telegram",new_callable=AsyncMock,
                      side_effect=fake_telegram) as tg,patch.object(
            bot_feed,"run",new_callable=AsyncMock) as receiver:
            result=await bot_feed.activate_receiver(g,{"confirm":True})
            await g.bot_feed_task
        self.assertTrue(result["pending_preserved"])
        self.assertEqual(result["previous_webhook_host"],"another-service.example.com")
        self.assertEqual(g._bot_feed_receiver_state,"starting")
        self.assertEqual(tg.await_count,2)
        self.assertEqual(tg.await_args_list[1].args[2],"deleteWebhook")
        receiver.assert_awaited_once_with(g)

    async def test_no_old_webhook_does_not_call_delete_webhook(self):
        g=SimpleNamespace(bot_feed_task=None)
        client=self.make_client()
        with patch.object(bot_feed,"token",return_value="123:TOKEN"),patch.object(
            bot_feed.aiohttp,"ClientSession",return_value=client
        ),patch.object(bot_feed,"telegram",new_callable=AsyncMock,
                      return_value={"url":""}) as tg,patch.object(
            bot_feed,"run",new_callable=AsyncMock) as receiver:
            result=await bot_feed.activate_receiver(g,{"confirm":True})
            await g.bot_feed_task
        self.assertTrue(result["ok"])
        tg.assert_awaited_once()
        self.assertEqual(tg.await_args.args[2],"getWebhookInfo")
        receiver.assert_awaited_once()

    async def test_running_receiver_is_not_restarted(self):
        waiting=asyncio.Event()
        task=asyncio.create_task(waiting.wait())
        g=SimpleNamespace(bot_feed_task=task)
        client=self.make_client()
        with patch.object(bot_feed,"token",return_value="123:TOKEN"),patch.object(
            bot_feed.aiohttp,"ClientSession",return_value=client
        ),patch.object(bot_feed,"telegram",new_callable=AsyncMock,
                      return_value={"url":""}),patch.object(
            bot_feed,"run",new_callable=AsyncMock) as receiver:
            await bot_feed.activate_receiver(g,{"confirm":True})
            receiver.assert_not_awaited()
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)


class ConflictReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_does_not_auto_delete_webhook_of_another_service(self):
        g=SimpleNamespace(closed=False,db=lambda:None)
        client=MagicMock()
        client.__aenter__=AsyncMock(return_value=SimpleNamespace())
        client.__aexit__=AsyncMock(return_value=False)
        async def methods(session, token, name, data=None):
            if name=="getMe":return {"id":1042,"can_read_all_group_messages":False}
            if name=="getWebhookInfo":return {
                "url":"https://another-app.example.net/tokenSecret"}
            raise AssertionError("Unexpected Bot API method")
        with patch.object(bot_feed,"token",return_value="someToken"),patch.object(
            bot_feed,"schema",new_callable=AsyncMock
        ),patch.object(bot_feed.aiohttp,"ClientSession",return_value=client),patch.object(
            bot_feed,"telegram",new_callable=AsyncMock,side_effect=methods
        ) as tg:
            await bot_feed.run(g)
        self.assertEqual(g._bot_feed_receiver_state,"webhook_conflict")
        self.assertEqual(g._bot_feed_webhook_host,"another-app.example.net")
        self.assertFalse(g._bot_feed_privacy_disabled)
        self.assertEqual([c.args[2] for c in tg.await_args_list],
                         ["getMe","getWebhookInfo"])


class AdminIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonadmin_cannot_trigger_delete_webhook(self):
        with patch.object(admin,"is_admin",return_value=False),patch.object(
            bot_feed,"activate_receiver",new_callable=AsyncMock) as action:
            with self.assertRaises(web.HTTPForbidden):
                await admin.admin_op(SimpleNamespace(),7,
                    "admin-bot-feed-activate",{"confirm":True})
        action.assert_not_awaited()

    async def test_activation_is_separate_from_old_folder_update(self):
        g=SimpleNamespace(db=lambda:SimpleNamespace())
        with patch.object(admin,"is_admin",return_value=True),patch.object(
            bot_feed,"activate_receiver",new_callable=AsyncMock,
            return_value={"ok":True}) as action:
            result=await admin.admin_op(g,7,
                "admin-bot-feed-activate",{"confirm":True})
        self.assertTrue(result["ok"])
        action.assert_awaited_once_with(g,{"confirm":True})


if __name__=="__main__":
    unittest.main()
