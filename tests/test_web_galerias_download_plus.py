"""No-network tests for isolated daily download allowances and the Stripe Plus add-on."""
import os
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit,parse_qs
from aiohttp import web
from cryptography.fernet import Fernet
from web_galerias import download_plus,downloads

class DownloadPlusTests(unittest.IsolatedAsyncioTestCase):
    async def test_base_tier_defaults_to_50_without_opening_telegram(self):
        pool=N(fetchval=AsyncMock(return_value=None),execute=AsyncMock())
        g=N(s=N(repo=N(pool=pool)),db=lambda:N(fetchval=AsyncMock()),
            free_granted=AsyncMock(return_value=False))
        with patch.object(download_plus,"configured",return_value=False):
            plan=await download_plus.tier(g,1234)
        self.assertEqual(plan["tier"],"basic")
        self.assertEqual(plan["daily_limit"],50)
        pool.fetchval.assert_awaited_once()
        self.assertIn("uid=$1 AND session IS NOT NULL",pool.fetchval.await_args.args[0])
        self.assertEqual(pool.fetchval.await_args.args[1],1234)
        pool.execute.assert_not_awaited()

    async def test_linked_descargas_account_gets_200_without_using_datos(self):
        pool=N(fetchval=AsyncMock(return_value=True),execute=AsyncMock())
        g=N(s=N(repo=N(pool=pool)),db=lambda:N(fetchval=AsyncMock()),
            free_granted=AsyncMock(return_value=False))
        with patch.object(download_plus,"configured",return_value=False):
            plan=await download_plus.tier(g,100)
        self.assertEqual(plan["tier"],"linked")
        self.assertEqual(plan["daily_limit"],200)
        self.assertIn("FROM descarga_accounts",pool.fetchval.await_args.args[0])
        pool.execute.assert_not_awaited()

    async def test_explicit_existing_free_grant_remains_unlimited(self):
        pool=N(fetchval=AsyncMock())
        g=N(s=N(repo=N(pool=pool)),free_granted=AsyncMock(return_value=True))
        with patch.object(download_plus,"configured",return_value=False):
            plan=await download_plus.tier(g,101)
        self.assertEqual(plan["tier"],"unlimited")
        self.assertIsNone(plan["daily_limit"])
        pool.fetchval.assert_not_awaited()

    async def test_paid_addon_requires_actual_paid_period_not_a_checkout_request(self):
        db=N(fetchval=AsyncMock(return_value=True))
        g=N(db=lambda:db,free_granted=AsyncMock(return_value=False),
            s=N(repo=N(pool=N(fetchval=AsyncMock(return_value=True)))))
        with patch.dict(os.environ,{"GALERIAS_PLUS_PRICE":"price_plus"}):
            with patch.object(download_plus,"configured",return_value=True):
                plan=await download_plus.tier(g,102)
        self.assertTrue(plan["quota_unlimited"])
        self.assertTrue(plan["plus_active"])
        sql=db.fetchval.await_args.args[0]
        for requirement in ("wg_plus_checkouts","wg_plus_subscriptions",
                            "c.session IS NOT NULL","s.paid_until>$2",
                            "s.price_id=$3","s.status IN ('active','trialing')"):
            self.assertIn(requirement,sql)
        self.assertEqual(db.fetchval.await_args.args[-1],"price_plus")

    async def test_failed_descargas_lookup_never_grants_200(self):
        pool=N(fetchval=AsyncMock(side_effect=OSError("offline")))
        g=N(s=N(repo=N(pool=pool)),free_granted=AsyncMock(return_value=False))
        with patch.object(download_plus,"configured",return_value=False):
            plan=await download_plus.tier(g,200)
        self.assertEqual(plan["daily_limit"],50)

    async def test_stripe_plus_checkout_only_for_existing_members(self):
        g=N(entitled=AsyncMock(return_value=False),db=lambda:N())
        with self.assertRaises(web.HTTPForbidden):
            await download_plus.checkout(g,123)
        g.entitled.assert_awaited_once_with(123)

    async def test_nonce_checkout_url_does_not_contain_telegram_id(self):
        db=N(execute=AsyncMock(),fetchval=AsyncMock(return_value=False))
        g=N(entitled=AsyncMock(return_value=True),db=lambda:db)
        with patch.dict(os.environ,{
            "GALERIAS_PLUS_URL":"https://buy.stripe.com/aAbbCc0011",
            "GALERIAS_PLUS_LINK":"plink_extra",
            "GALERIAS_PLUS_PRICE":"price_extra",
            "GALERIAS_STRIPE_WEBHOOK_SECRET":"whsec_test12345"}):
            with patch.object(download_plus,"tier",new=AsyncMock(
                return_value={"quota_unlimited":False})):
                result=await download_plus.checkout(g,987654321)
        url=urlsplit(result["url"])
        self.assertEqual(url.netloc,"buy.stripe.com")
        self.assertEqual(len(parse_qs(url.query)["client_reference_id"][0]),36)
        self.assertNotIn("987654321",result["url"])
        self.assertIn("wg_plus_checkouts",db.execute.await_args.args[0])
        self.assertEqual(db.execute.await_args.args[2],987654321)

    async def test_main_scheduled_cancel_blocks_new_paid_plus_after_current_access(self):
        db=N(execute=AsyncMock(),fetchval=AsyncMock(return_value=True))
        g=N(entitled=AsyncMock(return_value=True),db=lambda:db)
        with self.assertRaises(web.HTTPForbidden) as caught:
            await download_plus.checkout(g,1234)
        self.assertIn("cancelación programada",caught.exception.text)
        db.execute.assert_not_awaited()
        self.assertIn("cancel_at_period_end",db.fetchval.await_args.args[0])

    async def test_wrong_checkout_link_or_amount_never_binds_user(self):
        conn=N(fetchrow=AsyncMock(),execute=AsyncMock())
        payload={"mode":"subscription","payment_status":"paid",
                 "currency":"usd","amount_subtotal":199,
                 "payment_link":"plink_unrelated","subscription":"sub_abc",
                 "client_reference_id":"x"*24,"id":"cs_session"}
        with patch.dict(os.environ,{"GALERIAS_PLUS_LINK":"plink_plus",
                                     "GALERIAS_PLUS_PRICE":"price_plus"}):
            with patch.object(download_plus,"configured",return_value=True):
                await download_plus.apply_webhook(conn,"checkout.session.completed",payload,int(time.time()))
                payload["payment_link"]="plink_plus"
                payload["amount_subtotal"]=699
                await download_plus.apply_webhook(conn,"checkout.session.completed",payload,int(time.time()))
        conn.fetchrow.assert_not_awaited()
        conn.execute.assert_not_awaited()

    async def test_matching_paid_checkout_bind_is_nonce_locked(self):
        conn=N(fetchrow=AsyncMock(return_value={"uid":77}),execute=AsyncMock())
        payload={"mode":"subscription","payment_status":"paid",
                 "currency":"usd","amount_subtotal":199,
                 "payment_link":"plink_plus","subscription":"sub_plus",
                 "client_reference_id":"x"*25,"id":"cs_plus"}
        with patch.dict(os.environ,{"GALERIAS_PLUS_LINK":"plink_plus",
                                     "GALERIAS_PLUS_PRICE":"price_plus"}):
            with patch.object(download_plus,"configured",return_value=True):
                await download_plus.apply_webhook(conn,"checkout.session.completed",payload,int(time.time()))
        self.assertIn("FOR UPDATE",conn.fetchrow.await_args.args[0])
        self.assertIn("wg_plus_checkouts",conn.execute.await_args.args[0])
        self.assertNotIn("wg_subscriptions",conn.execute.await_args.args[0])

    async def test_invoice_only_accepts_exact_plus_price_and_paid_period(self):
        conn=N(execute=AsyncMock())
        invoice={"id":"in_plus","subscription":"sub_plus","customer":"cus_plus",
                 "status":"paid","paid":True,
                 "lines":{"data":[{"price":{"id":"price_elsewhere"},
                         "period":{"end":int(time.time()+3600)}}]}}
        with patch.dict(os.environ,{"GALERIAS_PLUS_LINK":"plink_plus",
                                     "GALERIAS_PLUS_PRICE":"price_plus"}):
            with patch.object(download_plus,"configured",return_value=True):
                await download_plus.apply_webhook(conn,"invoice.paid",invoice,int(time.time()))
                conn.execute.assert_not_awaited()
                invoice["lines"]["data"][0]["price"]["id"]="price_plus"
                await download_plus.apply_webhook(conn,"invoice.paid",invoice,int(time.time()))
        self.assertEqual(conn.execute.await_count,2)
        self.assertIn("wg_plus_subscriptions",conn.execute.await_args_list[0].args[0])
        self.assertIn("wg_plus_invoices",conn.execute.await_args_list[1].args[0])

    async def test_subscription_failure_revokes_extra_only(self):
        conn=N(execute=AsyncMock())
        invoice={"subscription":"sub_plus"}
        with patch.dict(os.environ,{"GALERIAS_PLUS_PRICE":"price_plus"}):
            with patch.object(download_plus,"configured",return_value=True):
                await download_plus.apply_webhook(conn,"invoice.payment_failed",invoice,int(time.time()))
        sql=conn.execute.await_args.args[0]
        self.assertIn("UPDATE wg_plus_subscriptions",sql)
        self.assertNotIn("UPDATE wg_subscriptions",sql)

    async def test_atomic_reservation_uses_200_tier_and_same_10_file_batch(self):
        ids=[11,12]
        c=N(fetch=AsyncMock(return_value=[
             {"id":11,"size":4,"folder":7,"mime":"image/jpeg","filename":"1.jpg"},
             {"id":12,"size":4,"folder":7,"mime":"image/jpeg","filename":"2.jpg"}]),
             fetchval=AsyncMock(return_value=198),execute=AsyncMock())
        @asynccontextmanager
        async def transaction():yield
        c.transaction=transaction
        @asynccontextmanager
        async def acquire():yield c
        service=N(cipher=Fernet(Fernet.generate_key()))
        g=N(entitled=AsyncMock(return_value=True),storage=N(ready=True),
            s=service,db=lambda:N(acquire=acquire))
        with patch.object(download_plus,"tier",new=AsyncMock(
             return_value={"daily_limit":200,"quota_unlimited":False,"tier":"linked"})):
            result=await downloads.reserve(g,888,ids)
        self.assertEqual(result["remaining"],0)
        self.assertEqual(result["quota_daily_limit"],200)
        self.assertTrue(any("FOR UPDATE" in x.args[0] for x in c.fetchval.await_args_list))
        self.assertTrue(any("UPDATE wg_quota" in x.args[0] for x in c.execute.await_args_list))
        self.assertEqual(result["count"],2)

if __name__=="__main__":unittest.main()
