"""No-network CUBANITAS bridge checks: original Stripe checkout and R2 untouched."""
import hashlib
import hmac
import os
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from web_galerias import customer_bridge, core


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


if __name__ == "__main__":
    unittest.main()
