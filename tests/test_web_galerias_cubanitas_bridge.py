"""No-network CUBANITAS bridge checks: original Stripe checkout and R2 untouched."""
import hashlib
import hmac
import os
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiohttp import web

from web_galerias import customer_bridge, core
from web_galerias.app import Gallery


class CubanitasBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_signing_and_isolated_schema(self):
        secret = "cubanitas-test-" + "b" * 64
        with patch.dict(os.environ, {"CUBANITAS_BRIDGE_SECRET": secret}):
            body, headers = customer_bridge.signed_payload({"telegram_id": 7536216348})
            expected = hmac.new(secret.encode(),
                (headers["X-Cubanitas-Time"] + ".").encode() + body,
                hashlib.sha256).hexdigest()
            self.assertEqual(expected, headers["X-Cubanitas-Signature"])
        self.assertIn("wg_bridge_snapshots", core.SCHEMA)
        self.assertIn("wg_access_blocks", core.SCHEMA)
        self.assertNotIn("ALTER TABLE descarga_", core.SCHEMA)

    async def test_customer_bot_signed_identity_and_dispute_block(self):
        uid = 7536216348
        db = SimpleNamespace(execute=AsyncMock())
        g = SimpleNamespace(db=lambda:db)
        with patch.dict(os.environ, {"CUBANITAS_BRIDGE_SECRET": "x" * 64}):
            customer_bridge._AUTH_CACHE.clear()
            with patch.object(customer_bridge, "post_signed",
                              new=AsyncMock(return_value={"uid": uid, "blocked": True})) as send:
                result = await customer_bridge.verify_customer_bot("signed", g)
                self.assertEqual(result, uid)
                self.assertIn("wg_access_blocks", db.execute.await_args.args[0])
                again = await customer_bridge.verify_customer_bot("signed", g)
                self.assertEqual(again, uid)
                send.assert_awaited_once()
        customer_bridge._AUTH_CACHE.clear()

    async def test_sync_keeps_uid_from_original_checked_out_subscription(self):
        row = {
            "uid": 7536216348, "customer": "cus_demo", "subscription": "sub_demo",
            "state": "active", "paid_until": int(time.time()+200000),
            "changed": float(time.time()), "session": "cs_live_demo",
            "created": float(time.time()-60), "telegram_name": "Test",
        }
        connection = SimpleNamespace(execute=AsyncMock())
        @asynccontextmanager
        async def trans():
            yield
        @asynccontextmanager
        async def acquire():
            yield connection
        connection.transaction = trans
        db = SimpleNamespace(fetch=AsyncMock(return_value=[row]), acquire=acquire)
        g = SimpleNamespace(db=lambda: db, closed=False)
        env = {
            "CUBANITAS_BRIDGE_SECRET": "y" * 64,
            "GALERIAS_STRIPE_PRICE_ID": "price_1UaHxOKjV2vzEefKTAdGJZej",
        }
        with patch.dict(os.environ, env):
            with patch.object(customer_bridge, "post_signed",
                              new=AsyncMock(return_value={"ok":True,"blocked":False})) as send:
                count = await customer_bridge.sync_once(g)
        self.assertEqual(count, 1)
        sent = send.await_args.args[1]
        self.assertEqual(sent["telegram_id"], 7536216348)
        self.assertEqual(sent["subscription_id"], "sub_demo")
        self.assertEqual(sent["checkout_session_id"], "cs_live_demo")
        self.assertEqual(sent["price_id"], env["GALERIAS_STRIPE_PRICE_ID"])
        self.assertEqual(connection.execute.await_count, 2)
        self.assertTrue(all("wg_" in call.args[0] for call in connection.execute.await_args_list))

    async def test_failure_never_revokes_user_or_updates_checkpoint(self):
        db = SimpleNamespace(fetch=AsyncMock(return_value=[{
            "uid":7536216348,"customer":"cus_demo","subscription":"sub_demo",
            "state":"active","paid_until":int(time.time()+120000),
            "changed":float(time.time()),"session":"cs_live_demo",
            "created":float(time.time()-50),"telegram_name":"Test",
        }]), acquire=AsyncMock())
        g = SimpleNamespace(db=lambda:db, closed=False)
        with patch.dict(os.environ, {
            "CUBANITAS_BRIDGE_SECRET": "z" * 64,
            "GALERIAS_STRIPE_PRICE_ID": "price_1UaHxOKjV2vzEefKTAdGJZej",
        }):
            with patch.object(customer_bridge, "post_signed",
                              new=AsyncMock(side_effect=ConnectionError("unreachable"))):
                self.assertEqual(await customer_bridge.sync_once(g), 0)
        db.acquire.assert_not_called()


    async def test_disputed_checkout_is_rejected_before_any_stripe_action(self):
        uid=7536216348
        db=SimpleNamespace(fetchval=AsyncMock(return_value=True))
        g=SimpleNamespace(auth=AsyncMock(return_value=uid),db=lambda:db)
        request=SimpleNamespace(content_length=0,json=AsyncMock(
            return_value={"op":"checkout"}))
        with patch("web_galerias.app.payments.checkout", new=AsyncMock()) as checkout:
            with self.assertRaises(web.HTTPForbidden):
                await Gallery.api(g,request)
            checkout.assert_not_called()
        self.assertIn("wg_access_blocks",db.fetchval.await_args.args[0])

    async def test_disputed_user_sees_no_checkout_button(self):
        uid=7536216348
        db=SimpleNamespace(execute=AsyncMock(),
            fetchval=AsyncMock(side_effect=[0,True]))
        g=SimpleNamespace(db=lambda:db,
                          entitled=AsyncMock(return_value=False),
                          free_granted=AsyncMock(return_value=False),
                          design=AsyncMock(return_value={}),
                          storage=SimpleNamespace(ready=True))
        with patch.dict(os.environ,{"GALERIAS_PUBLIC":"1"}):
            data=await core.GalleryCore.bootstrap(g,uid)
        self.assertFalse(data["checkout_enabled"])
        self.assertEqual(data["folders"],[])


    async def test_customer_bot_identity_is_scoped_to_a_signed_request(self):
        uid=1609522430
        from web_galerias.core import customer_bot_signed, _CUSTOMER_BOT_UID
        from descargas_app import security
        request=SimpleNamespace(headers={"X-Telegram-Init-Data": "signed-for-customer-bot"})
        g=SimpleNamespace(s=SimpleNamespace(token="unrelated-bot"))
        with patch.object(security,"validate_init_data",side_effect=ValueError("different bot")):
            with patch.object(customer_bridge,"ready",return_value=True):
                with patch.object(customer_bridge,"verify_customer_bot",
                                  new=AsyncMock(return_value=uid)):
                    token=_CUSTOMER_BOT_UID.set(0)
                    try:
                        self.assertEqual(await core.GalleryCore.auth(g,request),uid)
                        self.assertTrue(customer_bot_signed(uid))
                        self.assertFalse(customer_bot_signed(uid+1))
                    finally:
                        _CUSTOMER_BOT_UID.reset(token)
        self.assertFalse(customer_bot_signed(uid))

    async def test_new_customer_bot_user_can_buy_without_opening_the_original_pilot(self):
        from web_galerias import payments, payment_link_checkout
        from web_galerias.core import _CUSTOMER_BOT_UID
        from web_galerias.permissions import pilot_allowed
        uid=8129084202
        self.assertFalse(pilot_allowed(uid))  # never free, even within pilot
        db=SimpleNamespace(execute=AsyncMock(),fetchval=AsyncMock(return_value=False),
                           fetchrow=AsyncMock(return_value=None))
        g=SimpleNamespace(db=lambda:db, entitled=AsyncMock(return_value=False),
            design=AsyncMock(return_value={}),folder_list=AsyncMock(),
            storage=SimpleNamespace(ready=True))
        with patch.dict(os.environ,{"GALERIAS_PUBLIC":"0","GALERIAS_PAYMENT_TEST_UIDS":""}):
            token=_CUSTOMER_BOT_UID.set(0)
            try:
                # Original private pilot remains closed to unsigned/new customers.
                self.assertFalse((await core.GalleryCore.bootstrap(g,uid, "Tester"))["checkout_enabled"])
                with self.assertRaises(web.HTTPForbidden):
                    await payments.checkout(g,uid)
                # Only the previously authenticated CUSTOMER-SERVICE request gets checkout.
                _CUSTOMER_BOT_UID.set(uid)
                data=await core.GalleryCore.bootstrap(g,uid,"Tester")
                self.assertTrue(data["checkout_enabled"])
                with patch.object(payment_link_checkout,"configured",return_value=True):
                    with patch.object(payment_link_checkout,"checkout",
                                      new=AsyncMock(return_value={"url":"https://buy.stripe.com/test"})):
                        self.assertEqual((await payments.checkout(g,uid))["url"],
                                         "https://buy.stripe.com/test")
                # A dispute blocks checkout without touching the Stripe API.
                db.fetchval.return_value=True
                with self.assertRaises(web.HTTPForbidden):
                    await payments.checkout(g,uid)
            finally:
                _CUSTOMER_BOT_UID.reset(token)

    async def test_paid_customer_bot_user_gets_verified_access(self):
        from web_galerias.core import _CUSTOMER_BOT_UID
        uid=7536216348
        db=SimpleNamespace(fetchval=AsyncMock(return_value=False),
                           fetchrow=AsyncMock(return_value={
            "state":"active","paid_until":time.time()+900,"blocked":False,
            "risk_refund":False}))
        g=SimpleNamespace(db=lambda:db,free_granted=AsyncMock(return_value=False))
        with patch.dict(os.environ,{"GALERIAS_PUBLIC":"0","GALERIAS_PAYMENT_TEST_UIDS":str(uid)}):
            token=_CUSTOMER_BOT_UID.set(0)
            try:
                # Existing Stripe-paid pilot membership remains recognized,
                # regardless of which signed bot opens the Mini App.
                self.assertTrue(await core.GalleryCore.entitled(g,uid))
                _CUSTOMER_BOT_UID.set(uid)
                self.assertTrue(await core.GalleryCore.entitled(g,uid))
                db.fetchrow.return_value={**db.fetchrow.return_value,"blocked":True}
                self.assertFalse(await core.GalleryCore.entitled(g,uid))
            finally:
                _CUSTOMER_BOT_UID.reset(token)


    async def test_signed_customer_bot_does_not_open_public_pilot(self):
        from web_galerias.core import _CUSTOMER_BOT_UID, customer_bot_signed
        from web_galerias import payments
        uid=987654321
        db=SimpleNamespace(execute=AsyncMock(),fetchval=AsyncMock(return_value=False),
                           fetchrow=AsyncMock(return_value=None))
        g=SimpleNamespace(db=lambda:db,entitled=AsyncMock(return_value=False),
            design=AsyncMock(return_value={}),folder_list=AsyncMock(),
            storage=SimpleNamespace(ready=True))
        with patch.dict(os.environ,{"GALERIAS_PUBLIC":"0","GALERIAS_PAYMENT_TEST_UIDS":""}):
            token=_CUSTOMER_BOT_UID.set(uid)
            try:
                self.assertFalse(customer_bot_signed(uid))
                data=await core.GalleryCore.bootstrap(g,uid,"Tester")
                self.assertFalse(data["checkout_enabled"])
                with self.assertRaises(web.HTTPForbidden):
                    await payments.checkout(g,uid)
                self.assertFalse(await core.GalleryCore.entitled(
                    SimpleNamespace(db=lambda:db,free_granted=AsyncMock(return_value=False)),uid))
                db.fetchrow.assert_not_awaited()
            finally:
                _CUSTOMER_BOT_UID.reset(token)

    async def test_signed_customer_bot_only_five_pilot_ids(self):
        from web_galerias.core import _CUSTOMER_BOT_UID,customer_bot_signed
        with patch.dict(os.environ,{"GALERIAS_PUBLIC":"0","GALERIAS_PAYMENT_TEST_UIDS":"7536216348"}):
            for uid in (1609522430,5662710947,7536216348,7539025033,8129084202):
                token=_CUSTOMER_BOT_UID.set(uid)
                try:self.assertTrue(customer_bot_signed(uid))
                finally:_CUSTOMER_BOT_UID.reset(token)
            token=_CUSTOMER_BOT_UID.set(987654321)
            try:self.assertFalse(customer_bot_signed(987654321))
            finally:_CUSTOMER_BOT_UID.reset(token)

    def test_landing_does_not_advertise_stripe_before_checkout(self):
        from pathlib import Path
        js=(Path(__file__).resolve().parents[1]/"web_galerias"/"app.js").read_text()
        self.assertNotIn("Compra segura mediante Stripe",js)
        self.assertNotIn("La compra se habilitará cuando Stripe esté configurado",js)
        self.assertIn('$("intro-notice").classList.add("hidden")',js)


if __name__ == "__main__":
    unittest.main()
