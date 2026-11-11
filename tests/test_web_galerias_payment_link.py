"""Payment Link checkout: exact $6.99 Stripe subscription, no secret API key needed."""
import json
import os
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock,patch
from urllib.parse import parse_qs,urlsplit
from aiohttp import web
from web_galerias import payment_link_checkout as link
from web_galerias import payments

UID=7536216348
PRICE="price_1UaHxOKjV2vzEefKTAdGJZej"
PLINK="plink_1UaHxeKjV2vzEefKyFVwXM99"
URL="https://buy.stripe.com/28EeVe4Dmfzq42Xbf01B60i"
ENV={
 "GALERIAS_PUBLIC":"0",
 "GALERIAS_PAYMENT_TEST_UIDS":str(UID),
 "GALERIAS_STRIPE_PAYMENT_LINK_URL":URL,
 "GALERIAS_STRIPE_PAYMENT_LINK_ID":PLINK,
 "GALERIAS_STRIPE_PRICE_ID":PRICE,
 "GALERIAS_STRIPE_WEBHOOK_SECRET":"whsec_fixture_for_testing",
 "GALERIAS_STRIPE_KEY":"",
}

def subscription(period=None):
    return {"id":"sub_demo123456789","customer":"cus_demo",
            "status":"active","items":{"data":[{
                "price":{"id":PRICE},"current_period_end":period or int(time.time()+2600000)
            }]}}

def invoice(period=None):
    return {"id":"in_demo123","paid":True,"status":"paid",
      "customer":"cus_demo",
      "parent":{"subscription_details":{"subscription":"sub_demo123456789"}},
      "lines":{"data":[{"pricing":{"price_details":{"price":PRICE}},
                        "period":{"end":period or int(time.time()+2600000)}}]}}

class PaymentLinkTests(unittest.IsolatedAsyncioTestCase):
    def test_configuration_is_specific_and_refuses_missing_signature(self):
        with patch.dict(os.environ,ENV,clear=False):
            self.assertTrue(link.configured())
            self.assertTrue(payments.ready())
            for name in ("GALERIAS_STRIPE_PAYMENT_LINK_ID",
                         "GALERIAS_STRIPE_PRICE_ID",
                         "GALERIAS_STRIPE_WEBHOOK_SECRET"):
                with self.subTest(name=name):
                    with patch.dict(os.environ,{name:""}):
                        self.assertFalse(link.configured())
        with patch.dict(os.environ,{**ENV,"GALERIAS_STRIPE_PAYMENT_LINK_URL":
             "https://fake.stripe.com/evil"}):
            self.assertFalse(link.configured())

    async def test_checkout_prepares_unique_reference_not_user_id(self):
        db=SimpleNamespace(execute=AsyncMock())
        g=SimpleNamespace(db=lambda:db,entitled=AsyncMock(return_value=False))
        with patch.dict(os.environ,ENV):
            result=await payments.checkout(g,UID)
        parsed=urlsplit(result["url"])
        self.assertEqual(parsed.netloc,"buy.stripe.com")
        self.assertEqual(parsed.path,urlsplit(URL).path)
        nonce=parse_qs(parsed.query)["client_reference_id"][0]
        self.assertGreaterEqual(len(nonce),20)
        self.assertNotIn(str(UID),result["url"])
        self.assertNotIn("sk_",result["url"])
        sql,*args=db.execute.await_args.args
        self.assertIn("INSERT INTO wg_checkouts",sql)
        self.assertEqual(args[0],nonce)
        self.assertEqual(args[1],UID)
        self.assertGreater(args[2],0)

    async def test_other_nonpilot_user_still_cannot_checkout(self):
        db=SimpleNamespace(execute=AsyncMock())
        g=SimpleNamespace(db=lambda:db,entitled=AsyncMock(return_value=False))
        with patch.dict(os.environ,ENV):
            with self.assertRaises(web.HTTPForbidden):
                await payments.checkout(g,123456789)
        db.execute.assert_not_awaited()

    def test_product_price_validation_works_with_new_and_old_stripe_invoices(self):
        self.assertTrue(link.price_matches({"price":{"id":PRICE}},PRICE))
        self.assertTrue(link.price_matches({"pricing":{
            "price_details":{"price":PRICE}}},PRICE))
        self.assertFalse(link.price_matches({"price":{"id":"price_other"}},PRICE))
        self.assertEqual(link.subscription_id_from_invoice(invoice()),"sub_demo123456789")
        self.assertGreater(link.invoice_paid_period(invoice(),PRICE),time.time())
        self.assertIsNone(link.subscription_period(subscription(), "price_wrong"))
        self.assertGreater(link.subscription_period(subscription(),PRICE),time.time())
        bad=invoice();bad["lines"]["data"][0]["pricing"]["price_details"]["price"]="price_wrong"
        self.assertEqual(link.invoice_paid_period(bad,PRICE),0)

    async def test_checkout_event_requires_expected_link_amount_and_nonce(self):
        connection=SimpleNamespace(fetchrow=AsyncMock(return_value={"uid":UID}),
                                   execute=AsyncMock())
        obj={"id":"cs_live_demo123","client_reference_id":"reference_with_24_randomcharacters",
             "subscription":"sub_demo123456789",
             "payment_link":PLINK,"mode":"subscription",
             "currency":"usd","payment_status":"paid","amount_total":699}
        with patch.dict(os.environ,ENV):
            accepted=await link.bind_paid_checkout(connection,obj)
            self.assertEqual(accepted,"sub_demo123456789")
            self.assertIn("nonce=$1",connection.fetchrow.await_args.args[0])
            self.assertEqual(connection.fetchrow.await_args.args[1],obj["client_reference_id"])
            self.assertIn("UPDATE wg_checkouts",connection.execute.await_args.args[0])
            for key,invalid in (("payment_link","plink_wrong"),
                 ("amount_total",999),("payment_status","unpaid"),
                 ("mode","payment"),("currency","eur")):
                with self.subTest(key=key):
                    bad=dict(obj);bad[key]=invalid
                    self.assertIsNone(await link.bind_paid_checkout(connection,bad))
        self.assertEqual(connection.execute.await_count,1)

    async def test_paid_invoice_period_is_staged_before_checkout_and_is_reconciled(self):
        db=SimpleNamespace(execute=AsyncMock())
        period=int(time.time()+30*86400)
        with patch.dict(os.environ,ENV):
            sub=await link.stage_paid_invoice(db,invoice(period))
            self.assertEqual(sub,"sub_demo123456789")
            self.assertIn("paid_until=GREATEST",db.execute.await_args.args[0])
            self.assertEqual(db.execute.await_args.args[4],period)
            stage=await link.stage_subscription(db,subscription(period),time.time())
            self.assertEqual(stage,"sub_demo123456789")
            self.assertIn("INSERT INTO wg_link_subscriptions",db.execute.await_args.args[0])
            bound=SimpleNamespace(uid=UID)
            db.fetchrow=AsyncMock(return_value={
                "uid":UID,"customer":"cus_demo",
                "subscription":"sub_demo123456789","status":"active",
                "price_id":PRICE,"paid_until":period})
            await link.reconcile_access(db,"sub_demo123456789")
            self.assertIn("INSERT INTO wg_subscriptions",db.execute.await_args.args[0])
            self.assertEqual(db.execute.await_args.args[1],UID)
            self.assertEqual(db.execute.await_args.args[5],period)

    async def test_unpaid_or_canceled_subscription_cannot_grant_access(self):
        period=time.time()+30000
        db=SimpleNamespace(fetchrow=AsyncMock(return_value={
            "uid":UID,"customer":"cus_demo",
            "subscription":"sub_demo123456789","status":"active",
            "price_id":PRICE,"paid_until":0}),execute=AsyncMock())
        with patch.dict(os.environ,ENV):
            await link.reconcile_access(db,"sub_demo123456789")
            db.execute.assert_not_awaited()
            db.fetchrow.return_value={**db.fetchrow.return_value,
                                      "paid_until":period,"status":"canceled"}
            await link.reconcile_access(db,"sub_demo123456789")
            self.assertNotIn("INSERT INTO wg_subscriptions",db.execute.await_args.args[0])
            db.execute.reset_mock()
            db.fetchrow.return_value={**db.fetchrow.return_value,
                                      "status":"active","price_id":"price_wrong"}
            await link.reconcile_access(db,"sub_demo123456789")
            db.execute.assert_not_awaited()

    async def test_webhook_signature_is_required_and_retries_safely(self):
        import stripe
        conn=SimpleNamespace(fetchval=AsyncMock(return_value=None),
                             execute=AsyncMock(),fetchrow=AsyncMock())
        @asynccontextmanager
        async def trans():yield
        @asynccontextmanager
        async def acquire():yield conn
        conn.transaction=trans
        g=SimpleNamespace(db=lambda:SimpleNamespace(acquire=acquire))
        req=SimpleNamespace(read=AsyncMock(return_value=b"{}"),
                            headers={"Stripe-Signature":"bad"})
        with patch.dict(os.environ,ENV):
            with patch.object(stripe.Webhook,"construct_event",
                              side_effect=ValueError("Bad signature")):
                with self.assertRaises(web.HTTPBadRequest):
                    await link.webhook(g,req)
            conn.execute.assert_not_awaited()
            event={"id":"evt_demo123","type":"customer.subscription.created",
                   "created":int(time.time()),"data":{"object":{"id":"sub_other",
                   "items":{"data":[{"price":{"id":"price_not_our_price"}}]}}}}
            with patch.object(stripe.Webhook,"construct_event",return_value=event):
                response=await link.webhook(g,req)
            self.assertEqual(response.status,200)
            self.assertIn("INSERT INTO wg_payment_events",conn.execute.await_args.args[0])
            conn.execute.reset_mock()
            conn.fetchval.return_value=True
            with patch.object(stripe.Webhook,"construct_event",return_value=event):
                response=await link.webhook(g,req)
            self.assertEqual(response.status,200)
            conn.execute.assert_not_awaited()

if __name__=="__main__":
    unittest.main()
