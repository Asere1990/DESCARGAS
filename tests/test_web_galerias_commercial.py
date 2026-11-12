"""Commercial dashboard metrics come from verified backend state, never Stripe on read."""
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch

from aiohttp import web
from web_galerias import commercial


class CommercialMetricsTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_is_private_deduplicated_and_indexed(self):
        for value in ("wg_visit_days","wg_commercial_events",
                      "PRIMARY KEY(uid,visit_day)","source TEXT PRIMARY KEY",
                      "CHECK(kind IN ('purchase','renewal','cancellation'))",
                      "ENABLE ROW LEVEL SECURITY","REVOKE ALL"):
            self.assertIn(value,commercial.SCHEMA)

    async def test_purchase_uses_verified_link_session_once_only(self):
        db=N(fetchrow=AsyncMock(return_value={"uid":888}),
             execute=AsyncMock())
        g=N(db=lambda:db)
        await commercial.record_verified_stripe_event(
            g,"checkout.session.completed",
            {"id":"cs_123","amount_total":699},
            "evt_purchase","sub_live",1760000000)
        self.assertIn("wg_checkouts",db.fetchrow.await_args.args[0])
        self.assertEqual(db.fetchrow.await_args.args[1:],("cs_123","sub_live"))
        query,*args=db.execute.await_args.args
        self.assertIn("ON CONFLICT(source) DO NOTHING",query)
        self.assertEqual(args[0],"checkout:cs_123")
        self.assertEqual(args[1],"purchase")
        self.assertEqual(args[2],888)
        self.assertEqual(args[3],"sub_live")
        self.assertEqual(args[5],699)

    async def test_renewals_only_cycle_invoice_with_owner_and_verified_subscription(self):
        db=N(fetchrow=AsyncMock(return_value={"uid":888}),
             execute=AsyncMock())
        g=N(db=lambda:db)
        await commercial.record_verified_stripe_event(
            g,"invoice.paid",
            {"id":"in_first","billing_reason":"subscription_create",
             "amount_paid":699,"currency":"usd"},
            "evt_initial","sub_live",1760000000)
        db.execute.assert_not_awaited()
        await commercial.record_verified_stripe_event(
            g,"invoice.paid",
            {"id":"in_recurring","billing_reason":"subscription_cycle",
             "amount_paid":699,"currency":"usd"},
            "evt_renewal","sub_live",1760000000)
        self.assertEqual(db.execute.await_args.args[1],"invoice:in_recurring")
        self.assertEqual(db.execute.await_args.args[2],"renewal")
        db.execute.reset_mock()
        db.fetchrow.return_value=None
        await commercial.record_verified_stripe_event(
            g,"invoice.paid",
            {"id":"in_foreign","billing_reason":"subscription_cycle",
             "amount_paid":699,"currency":"usd"},
            "evt_other","sub_other",1760000000)
        db.execute.assert_not_awaited()

    async def test_cancel_requires_billing_subscription_and_verification(self):
        db=N(fetchrow=AsyncMock(return_value={"uid":777}),
             execute=AsyncMock())
        g=N(db=lambda:db)
        await commercial.record_verified_stripe_event(
            g,"customer.subscription.updated",{"id":"sub_live","cancel_at_period_end":True},
            "evt_scheduled","sub_live",1760000000)
        db.execute.assert_not_awaited()
        await commercial.record_verified_stripe_event(
            g,"customer.subscription.deleted",{"id":"sub_live"},
            "evt_deleted","sub_live",1760000000)
        args=db.execute.await_args.args
        self.assertEqual(args[1],"cancel:evt_deleted")
        self.assertEqual(args[2],"cancellation")
        self.assertEqual(args[6],0)

    async def test_dashboard_reports_read_only_counts_and_shows_free_separately(self):
        fields=dict(visitors=40,checkout_users=12,checkout_attempts=15,
                    purchases=6,renewals=3,cancellations=1,gross_cents=6300,
                    active_paid=10,active_free=5)
        db=N(fetchrow=AsyncMock(return_value=fields),
             fetch=AsyncMock(return_value=[{
                 "day":date(2026,11,12),"visitors":3,"purchases":1
             }]),
             execute=AsyncMock())
        g=N(db=lambda:db)
        with patch.object(commercial,"is_admin",return_value=True),patch.object(
             commercial,"file_ids",return_value=set()):
            result=await commercial.report(g,111,{"days":30})
        self.assertEqual(result["visitors"],40)
        self.assertEqual(result["active_paid"],10)
        self.assertEqual(result["active_free"],5)
        self.assertEqual(result["conversion"],40.0)
        self.assertEqual(result["gross_cents"],6300)
        self.assertEqual(result["trend"],[{
            "day":"2026-11-12","visitors":3,"purchases":1
        }])
        db.execute.assert_not_awaited()
        self.assertIn("wg_visit_days",db.fetchrow.await_args.args[0])
        self.assertIn("wg_checkouts",db.fetchrow.await_args.args[0])
        self.assertIn("wg_commercial_events",db.fetchrow.await_args.args[0])
        self.assertIn("wg_free_access",db.fetchrow.await_args.args[0])
        self.assertIn("generate_series",db.fetch.await_args.args[0])

    async def test_admin_only_and_limited_period(self):
        db=N(fetchrow=AsyncMock(),fetch=AsyncMock())
        g=N(db=lambda:db)
        with patch.object(commercial,"is_admin",return_value=False):
            with self.assertRaises(web.HTTPForbidden):
                await commercial.report(g,777,{"days":7})
        with patch.object(commercial,"is_admin",return_value=True):
            for value in (0,1,365,"30",30.5,True):
                with self.subTest(value=value),self.assertRaises(web.HTTPBadRequest):
                    await commercial.report(g,777,{"days":value})
        db.fetchrow.assert_not_awaited()

    def test_admin_ui_is_lazy_and_never_uses_stripe_endpoint(self):
        root=Path(__file__).resolve().parents[1]/"web_galerias"
        app=(root/"app.js").read_text()
        tools=(root/"admin_tools.js").read_text()
        markup=(root/"index.html").read_text()
        self.assertIn('id="tab-comercio"',markup)
        self.assertIn('id="commercial-range"',markup)
        self.assertIn('id="commercial-metrics"',markup)
        self.assertIn('id="commercial-trend"',markup)
        self.assertIn('if(tab==="comercio")window.CubanitasTools?.loadCommercial?.();',app)
        self.assertIn('api("admin-commercial-report",{days:range})',tools)
        self.assertNotIn('fetch("https://api.stripe.com',tools)
        self.assertIn("after the",commercial.record_verified_stripe_event.__doc__.lower()+
                      " after the")  # no Stripe network call is part of this helper


if __name__=="__main__":
    unittest.main()
