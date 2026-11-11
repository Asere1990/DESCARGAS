"""Emergency, one-time, verified live Stripe receipt recovery for one tester.

No charge is made. Recovery requires the original user-scoped nonce stored by
the server at checkout plus a purchase independently verified by the operator
against Checkout, Invoice (paid), and active Subscription in Stripe.
"""
import json
import os
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from web_galerias.payment_link_checkout import recover_independently_verified_purchase

UID=7536216348
SUB="sub_mock_paid"
SESSION="cs_live_mock_paid"
PRICE="price_1UaHxOKjV2vzEefKTAdGJZej"
LINK="plink_1UaHxeKjV2vzEefKyFVwXM99"
NONCE="random_mock_original_checkout_nonce_abcdefghijklmnopqrstuvwxyz"

def receipt():
    return {"uid":UID,"nonce":NONCE,"session":SESSION,
        "subscription":SUB,"invoice":"in_mock_paid","customer":"cus_mock_paid",
        "price_id":PRICE,"payment_link":LINK,"currency":"usd",
        "amount_paid":699,"paid_until":int(time.time()+2592000),
        "livemode":True}

def environment(data=None):
    return {"GALERIAS_PAYMENT_TEST_UIDS":str(UID),
        "GALERIAS_STRIPE_PRICE_ID":PRICE,
        "GALERIAS_STRIPE_PAYMENT_LINK_ID":LINK,
        "GALERIAS_VERIFIED_PAID_RECOVERY":json.dumps(data or receipt())}

def make_db(checkout=True,active=True,already=False):
    source={"uid":UID,"session":None,"subscription":None,"created":time.time()-120}
    sub={"uid":UID,"subscription":SUB,"customer":"cus_mock_paid",
         "status":"active","paid_until":receipt()["paid_until"],"price_id":PRICE}
    conn=SimpleNamespace(
        execute=AsyncMock(),
        fetchrow=AsyncMock(side_effect=[source if checkout else None,sub]),
        fetchval=AsyncMock(side_effect=[True if already else None,True if active else None]))
    @asynccontextmanager
    async def transaction():yield
    @asynccontextmanager
    async def acquire():yield conn
    conn.transaction=transaction
    return SimpleNamespace(db=lambda:SimpleNamespace(acquire=acquire)),conn

class PaidRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_paid_receipt_restores_original_uid_without_charge(self):
        g,conn=make_db()
        with patch.dict(os.environ,environment()):
            result=await recover_independently_verified_purchase(g)
        self.assertEqual(result,"reconciled")
        self.assertEqual(conn.fetchrow.await_args_list[0].args[1:3],(NONCE,UID))
        statements=[call.args[0] for call in conn.execute.await_args_list]
        self.assertTrue(any("INSERT INTO wg_link_subscriptions" in sql for sql in statements))
        self.assertTrue(any("UPDATE wg_checkouts" in sql for sql in statements))
        self.assertTrue(any("INSERT INTO wg_subscriptions" in sql for sql in statements))
        self.assertTrue(any("INSERT INTO wg_activity" in sql for sql in statements))
        self.assertFalse(any("INSERT INTO wg_payment_events" in sql for sql in statements))
        self.assertFalse(any("DELETE" in sql for sql in statements))
        self.assertFalse(any("stripe.checkout.Session.create" in sql for sql in statements))
        self.assertEqual(conn.fetchval.await_count,2)

    async def test_if_checkout_nonce_does_not_exist_no_access_granted(self):
        g,conn=make_db(checkout=False)
        with patch.dict(os.environ,environment()):
            result=await recover_independently_verified_purchase(g)
        self.assertEqual(result,"original_checkout_not_found")
        conn.execute.assert_not_awaited()

    async def test_already_recovered_cannot_grant_access_second_time(self):
        g,conn=make_db(already=True)
        with patch.dict(os.environ,environment()):
            result=await recover_independently_verified_purchase(g)
        self.assertEqual(result,"already_reconciled")
        conn.execute.assert_not_awaited()
        conn.fetchrow.assert_not_awaited()

    async def test_bad_price_uid_amount_or_currency_cannot_grant_access(self):
        for key,wrong in (("price_id","price_wrong"),("uid",12345),
                          ("amount_paid",0),("currency","eur"),
                          ("livemode",False),("payment_link","plink_wrong"),
                          ("paid_until",int(time.time()-3600))):
            with self.subTest(key=key):
                data=receipt();data[key]=wrong
                g,conn=make_db()
                with patch.dict(os.environ,environment(data)):
                    with self.assertRaises(ValueError):
                        await recover_independently_verified_purchase(g)
                conn.fetchrow.assert_not_awaited()
                conn.execute.assert_not_awaited()

    async def test_subscription_not_activated_results_in_error_without_audit(self):
        g,conn=make_db(active=False)
        with patch.dict(os.environ,environment()):
            with self.assertRaises(ValueError):
                await recover_independently_verified_purchase(g)
        self.assertFalse(any("INSERT INTO wg_activity" in call.args[0]
                             for call in conn.execute.await_args_list))

    async def test_no_env_variable_does_not_query_or_modify_database(self):
        g,conn=make_db()
        with patch.dict(os.environ,{"GALERIAS_VERIFIED_PAID_RECOVERY":""}):
            self.assertIsNone(await recover_independently_verified_purchase(g))
        conn.fetchrow.assert_not_awaited()
        conn.execute.assert_not_awaited()

if __name__=="__main__":
    unittest.main()
