"""CUBANITAS payment risk and duplicate prevention; all services mocked."""
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch

from web_galerias import payment_risk,dupe_guard
from web_galerias import payment_link_checkout as stripe_link


class PaymentRiskTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_stripe_signed_webhook_approved_types_are_staged(self):
        conn=N(execute=AsyncMock())
        await payment_risk.stage_event(conn,"charge.refunded","evt_refund_test",time.time())
        await payment_risk.stage_event(conn,"charge.dispute.created","evt_dispute_test",time.time())
        await payment_risk.stage_event(conn,"invoice.paid","evt_paid_test",time.time())
        self.assertEqual(conn.execute.await_count,2)
        self.assertTrue(all("wg_web_risk_events" in c.args[0]
                            for c in conn.execute.await_args_list))

    async def gallery(self,with_owner=True):
        transaction_conn=N(execute=AsyncMock())
        @asynccontextmanager
        async def transaction():
            yield
        transaction_conn.transaction=transaction
        @asynccontextmanager
        async def acquire():
            yield transaction_conn
        db=N(execute=AsyncMock(),fetchrow=AsyncMock(return_value={
            "uid":11223344} if with_owner else None),acquire=acquire)
        return N(db=lambda:db),db,transaction_conn

    async def test_only_verified_web_full_refund_blocks_matching_subscription(self):
        g,db,conn=await self.gallery()
        event={"event_id":"evt_refund_test","kind":"charge.refunded","attempts":0}
        result={"verified":True,"subscription":"sub_web_test",
                "kind":"refund","event_created":int(time.time())}
        with patch.object(payment_risk.customer_bridge,"post_signed",new=AsyncMock(
                return_value=result)) as request:
            await payment_risk._process(g,event)
        request.assert_awaited_once()
        self.assertTrue(any("wg_web_revocations" in c.args[0]
                            for c in conn.execute.await_args_list))
        self.assertFalse(any("wg_web_dispute_blocks" in c.args[0]
                             for c in conn.execute.await_args_list))
        self.assertFalse(any("wg_plus_" in c.args[0]
                             for c in conn.execute.await_args_list))
        self.assertTrue(any("wg_subscriptions SET changed" in c.args[0]
                             for c in conn.execute.await_args_list))

    async def test_dispute_blocks_whole_web_account_not_other_products(self):
        g,db,conn=await self.gallery()
        event={"event_id":"evt_dispute_test","kind":"charge.dispute.created","attempts":0}
        result={"verified":True,"subscription":"sub_web_test","kind":"dispute"}
        with patch.object(payment_risk.customer_bridge,"post_signed",new=AsyncMock(
                return_value=result)):
            await payment_risk._process(g,event)
        self.assertTrue(any("wg_web_dispute_blocks" in c.args[0]
                            for c in conn.execute.await_args_list))

    async def test_unverified_event_does_not_revoke_any_user(self):
        g,db,conn=await self.gallery()
        with patch.object(payment_risk.customer_bridge,"post_signed",new=AsyncMock(
                return_value={"verified":False})):
            await payment_risk._process(g,{"event_id":"evt_other","kind":"charge.refunded","attempts":0})
        db.fetchrow.assert_not_awaited()
        self.assertFalse(any("wg_web_revocations" in c.args[0]
                             for c in conn.execute.await_args_list))

    async def test_unlinked_or_provider_error_defers_instead_of_revoking(self):
        g,db,conn=await self.gallery(with_owner=False)
        event={"event_id":"evt_refund_test","kind":"charge.refunded","attempts":0}
        with patch.object(payment_risk.customer_bridge,"post_signed",new=AsyncMock(
                return_value={"verified":True,"kind":"refund",
                              "subscription":"sub_web_test"})):
            await payment_risk._process(g,event)
        self.assertFalse(any("wg_web_revocations" in c.args[0]
                             for c in conn.execute.await_args_list))
        self.assertTrue(any("state='queued'" in c.args[0]
                             for c in db.execute.await_args_list))

    async def test_payment_receipts_order_can_only_move_to_newer_subscription(self):
        import os
        newer=int(time.time())+86400
        receipt={"uid":99,"customer":"cus_test","subscription":"sub_new",
                 "status":"active","price_id":"price_exact",
                 "paid_until":newer,"started_at":int(time.time()),
                 "cancel_at_period_end":False}
        conn=N(fetchrow=AsyncMock(return_value=receipt),execute=AsyncMock())
        with patch.dict(os.environ,{"GALERIAS_STRIPE_PRICE_ID":"price_exact"}):
            await stripe_link.reconcile_access(conn,"sub_new")
        sql=conn.execute.await_args.args[0]
        self.assertIn("subscription_started_at>=wg_subscriptions.subscription_started_at",sql)
        self.assertIn("ELSE excluded.paid_until",sql)
        self.assertNotIn("DELETE",sql)


class WebDuplicateTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_duplicate_keeps_latest_and_schedules_old_renewal(self):
        db=N(fetch=AsyncMock(return_value=[{
            "uid":12345678,
            "subscriptions":["sub_old","sub_new"],
        }]),execute=AsyncMock())
        g=N(db=lambda:db)
        with patch.object(dupe_guard.customer_bridge,"ready",return_value=True):
            with patch.object(dupe_guard.customer_bridge,"post_signed",new=AsyncMock(
                return_value={
                    "ok":True,"kept":"sub_new",
                    "older":[{"subscription":"sub_old","scheduled":True}],
                })) as ask:
                count=await dupe_guard.dedupe_once(g)
        self.assertEqual(count,1)
        self.assertEqual(ask.await_args.args[1]["telegram_id"],12345678)
        self.assertEqual(db.execute.await_count,2)
        self.assertIn("cancel_at_period_end=TRUE",db.execute.await_args_list[0].args[0])
        self.assertIn("UPDATE wg_subscriptions",db.execute.await_args_list[1].args[0])
        self.assertNotIn("REFUND",str(db.execute.await_args_list))

    async def test_invalid_selection_does_not_modify_web_or_charge_anyone(self):
        db=N(fetch=AsyncMock(return_value=[{
            "uid":12345678,"subscriptions":["sub_old","sub_new"],
        }]),execute=AsyncMock())
        g=N(db=lambda:db)
        with patch.object(dupe_guard.customer_bridge,"ready",return_value=True):
            with patch.object(dupe_guard.customer_bridge,"post_signed",new=AsyncMock(
                return_value={"ok":True,"kept":"sub_other","older":[]})):
                result=await dupe_guard.dedupe_once(g)
        self.assertEqual(result,0)
        db.execute.assert_not_awaited()


if __name__=="__main__":
    unittest.main()
