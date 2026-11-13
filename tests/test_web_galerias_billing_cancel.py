"""Renewal cancellation: existing signed billing only; no real Stripe writes."""
import os
import time
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from aiohttp import web
from web_galerias import billing_cancel


class BillingCancelTests(unittest.IsolatedAsyncioTestCase):
    def setup_gallery(self,*,status="active",expires=None,plus=False,
                      plus_price="price_PlusTest"):
        db=N(fetchrow=AsyncMock(return_value={
            "subscription":"sub_main_test","state":status,
            "paid_until":expires if expires is not None else time.time()+86400*22,
            "cancel_at_period_end":False,
        }),fetch=AsyncMock(return_value=[{
            "subscription":"sub_plus_test","price_id":plus_price,
            "cancel_at_period_end":False,
        }] if plus else []),execute=AsyncMock())
        return N(db=lambda:db),db

    async def test_no_user_confirmation_never_contacts_bridge(self):
        g,db=self.setup_gallery()
        with patch.object(billing_cancel.customer_bridge,"post_signed",new=AsyncMock()) as req:
            with self.assertRaises(web.HTTPBadRequest):
                await billing_cancel.cancel(g,10001,"other")
        req.assert_not_awaited()
        db.fetchrow.assert_not_awaited()

    async def test_requires_paid_main_membership(self):
        g,db=self.setup_gallery(status="canceled")
        with patch.object(billing_cancel.customer_bridge,"ready",return_value=True):
            with patch.object(billing_cancel.customer_bridge,"post_signed",new=AsyncMock()) as req:
                with self.assertRaises(web.HTTPForbidden):
                    await billing_cancel.cancel(g,10001,"CANCELAR_RENOVACION_WEB")
        req.assert_not_awaited()
        db.execute.assert_not_awaited()

    async def test_cancels_exact_verified_web_price_without_plus(self):
        g,db=self.setup_gallery()
        response={"main":{"scheduled":True,"end":int(time.time())+86400*22},
                  "plus":[],"ok":True}
        sent=[]
        async def bridge(op,data,timeout):
            sent.append((op,data))
            return response
        with patch.dict(os.environ,{"GALERIAS_PLUS_PRICE":"price_PlusTest"}),patch.object(
                billing_cancel.customer_bridge,"ready",return_value=True),patch.object(
                billing_cancel.customer_bridge,"post_signed",new=bridge):
            result=await billing_cancel.cancel(g,10001,"CANCELAR_RENOVACION_WEB")
        self.assertTrue(result["ok"])
        self.assertTrue(result["main_scheduled"])
        self.assertEqual(sent[0][0],"cancel-renewal")
        self.assertEqual(sent[0][1]["plus"],[])
        self.assertEqual(sent[0][1]["telegram_id"],10001)
        self.assertEqual(len(db.execute.await_args_list),2)

    async def test_plus_is_bound_to_same_owner_and_cancelled_separately(self):
        g,db=self.setup_gallery(plus=True)
        response={"main":{"scheduled":True,"end":int(time.time())+86400*22},
                  "plus":[{"subscription":"sub_plus_test","scheduled":True,
                           "end":int(time.time())+86400*12}],"ok":True}
        with patch.dict(os.environ,{"GALERIAS_PLUS_PRICE":"price_PlusTest"}),patch.object(
                billing_cancel.customer_bridge,"ready",return_value=True),patch.object(
                billing_cancel.customer_bridge,"post_signed",new=AsyncMock(
                    return_value=response)) as bridge:
            result=await billing_cancel.cancel(g,10001,"CANCELAR_RENOVACION_WEB")
        self.assertTrue(result["ok"])
        self.assertEqual(result["plus_total"],1)
        self.assertEqual(result["plus_scheduled"],1)
        payload=bridge.await_args.args[1]
        self.assertEqual(payload["plus"],[{"subscription":"sub_plus_test",
                                          "price":"price_PlusTest"}])
        self.assertEqual(len(db.execute.await_args_list),3)

    async def test_plus_wrong_price_blocks_both_cancellations(self):
        g,db=self.setup_gallery(plus=True,plus_price="price_other")
        with patch.dict(os.environ,{"GALERIAS_PLUS_PRICE":"price_PlusTest"}),patch.object(
                billing_cancel.customer_bridge,"ready",return_value=True),patch.object(
                billing_cancel.customer_bridge,"post_signed",new=AsyncMock()) as bridge:
            with self.assertRaises(web.HTTPServiceUnavailable):
                await billing_cancel.cancel(g,10001,"CANCELAR_RENOVACION_WEB")
        bridge.assert_not_awaited()
        db.execute.assert_not_awaited()

    async def test_failed_plus_is_partial_and_not_silently_marked_canceled(self):
        g,db=self.setup_gallery(plus=True)
        result_from_stripe={"main":{"scheduled":True,"end":int(time.time())+86400},
                            "plus":[{"subscription":"sub_plus_test","scheduled":False,"end":0}],
                            "ok":False}
        with patch.dict(os.environ,{"GALERIAS_PLUS_PRICE":"price_PlusTest"}),patch.object(
                billing_cancel.customer_bridge,"ready",return_value=True),patch.object(
                billing_cancel.customer_bridge,"post_signed",new=AsyncMock(
                    return_value=result_from_stripe)):
            result=await billing_cancel.cancel(g,10001,"CANCELAR_RENOVACION_WEB")
        self.assertFalse(result["ok"])
        self.assertTrue(result["main_scheduled"])
        self.assertEqual(result["plus_scheduled"],0)
        self.assertEqual(len(db.execute.await_args_list),2)

    async def test_network_failure_does_not_mark_billing_canceled(self):
        g,db=self.setup_gallery()
        with patch.object(billing_cancel.customer_bridge,"ready",return_value=True),patch.object(
                billing_cancel.customer_bridge,"post_signed",new=AsyncMock(
                side_effect=TimeoutError("network"))):
            with self.assertRaises(web.HTTPServiceUnavailable):
                await billing_cancel.cancel(g,10001,"CANCELAR_RENOVACION_WEB")
        db.execute.assert_not_awaited()


if __name__=="__main__":
    unittest.main()
