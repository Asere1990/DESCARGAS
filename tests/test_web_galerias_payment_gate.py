"""Targeted payment flow: exactly one tester can buy while pilot remains private."""
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from aiohttp import web
from web_galerias.permissions import is_admin,is_tester,pilot_allowed,button_visible,payment_test_user
from web_galerias.core import GalleryCore
from web_galerias import payments

USER=7536216348
OTHER=(1609522430,5662710947)
ROOT=Path(__file__).resolve().parents[1]/"web_galerias"
ENV={"GALERIAS_PUBLIC":"0","GALERIAS_PAYMENT_TEST_UIDS":str(USER)}

class PaymentGateTests(unittest.IsolatedAsyncioTestCase):
    def test_exempt_exact_one_tester_from_free_access_without_losing_start_button(self):
        with patch.dict(os.environ,ENV):
            self.assertFalse(is_admin(USER))
            self.assertTrue(is_tester(USER))
            self.assertTrue(payment_test_user(USER))
            self.assertFalse(pilot_allowed(USER))
            self.assertTrue(button_visible(USER))
            for uid in OTHER:
                self.assertFalse(payment_test_user(uid))
                self.assertTrue(pilot_allowed(uid))
                self.assertTrue(button_visible(uid))
            self.assertTrue(pilot_allowed(7539025033))
            self.assertFalse(button_visible(8193438800))
        # Turning off the scoped environment variable restores free pilot access.
        with patch.dict(os.environ,{"GALERIAS_PUBLIC":"0","GALERIAS_PAYMENT_TEST_UIDS":""}):
            self.assertFalse(payment_test_user(USER))
            self.assertTrue(pilot_allowed(USER))
            self.assertTrue(button_visible(USER))

    async def test_unpaid_test_user_is_denied_until_verified_subscription(self):
        db=SimpleNamespace(fetchrow=AsyncMock(return_value=None),fetchval=AsyncMock(return_value=False))
        g=SimpleNamespace(db=lambda:db,
                          free_granted=AsyncMock(side_effect=lambda uid: uid==OTHER[0]))
        with patch.dict(os.environ,ENV):
            self.assertFalse(await GalleryCore.entitled(g,USER))
            db.fetchrow.assert_awaited_once()
            self.assertEqual(db.fetchrow.await_args.args[1],USER)
            db.fetchrow.reset_mock()
            self.assertTrue(await GalleryCore.entitled(g,OTHER[0]))
            db.fetchrow.assert_not_awaited()
        # One valid paid subscription grants access without launching publicly.
        db.fetchrow.return_value={"state":"active","paid_until":time.time()+86400}
        with patch.dict(os.environ,ENV):
            self.assertTrue(await GalleryCore.entitled(g,USER))
        db.fetchrow.return_value={"state":"canceled","paid_until":time.time()+86400}
        with patch.dict(os.environ,ENV):
            self.assertFalse(await GalleryCore.entitled(g,USER))
        db.fetchrow.return_value={"state":"active","paid_until":time.time()-60}
        with patch.dict(os.environ,ENV):
            self.assertFalse(await GalleryCore.entitled(g,USER))

    async def test_bootstrap_shows_buy_button_only_for_target_without_revealing_media(self):
        db=SimpleNamespace(execute=AsyncMock(),fetchval=AsyncMock(return_value=0))
        g=SimpleNamespace(db=lambda:db,entitled=AsyncMock(return_value=False),
                          free_granted=AsyncMock(return_value=False),
                          design=AsyncMock(return_value={"button_label":"ENTRAR A LA GALERÍA"}),
                          folder_list=AsyncMock(),storage=SimpleNamespace(ready=True))
        with patch.dict(os.environ,ENV):
            response=await GalleryCore.bootstrap(g,USER)
            self.assertFalse(response["allowed"])
            self.assertTrue(response["pilot"])
            self.assertTrue(response["checkout_enabled"])
            self.assertEqual(response["folders"],[])
            self.assertEqual(response["personal_folders"],[])
            g.folder_list.assert_not_awaited()
            response2=await GalleryCore.bootstrap(g,8193438800)
            self.assertFalse(response2["checkout_enabled"])
        # No subscriptions are created during bootstrap.
        for call in db.execute.await_args_list:
            self.assertNotIn("wg_checkouts",call.args[0])

    async def test_checkout_blocked_for_everyone_else_during_pilot(self):
        db=SimpleNamespace(execute=AsyncMock())
        g=SimpleNamespace(db=lambda:db,entitled=AsyncMock(return_value=False))
        with patch.dict(os.environ,ENV):
            with self.assertRaises(web.HTTPForbidden):
                await payments.checkout(g,8193438800)
        db.execute.assert_not_awaited()

    async def test_payment_action_requires_config_and_never_charges_on_page_open(self):
        db=SimpleNamespace(execute=AsyncMock())
        g=SimpleNamespace(db=lambda:db,entitled=AsyncMock(return_value=False))
        with patch.dict(os.environ,{**ENV,
                                    "GALERIAS_STRIPE_KEY":"",
                                    "GALERIAS_STRIPE_PRICE_ID":"",
                                    "GALERIAS_STRIPE_WEBHOOK_SECRET":""}):
            with self.assertRaises(web.HTTPServiceUnavailable):
                await payments.checkout(g,USER)
        db.execute.assert_not_awaited()

    async def test_user_click_creates_checkout_for_only_the_target(self):
        import stripe
        db=SimpleNamespace(execute=AsyncMock())
        g=SimpleNamespace(db=lambda:db,entitled=AsyncMock(return_value=False))
        session=SimpleNamespace(id="cs_test_mock",url="https://checkout.stripe.com/example")
        env={**ENV,"GALERIAS_STRIPE_KEY":"sk_test_example",
             "GALERIAS_STRIPE_PRICE_ID":"price_test_example",
             "GALERIAS_STRIPE_WEBHOOK_SECRET":"whsec_mock",
             "GALERIAS_RETURN_BOT":"Descargas_Robot"}
        with patch.dict(os.environ,env):
            with patch.object(stripe.checkout.Session,"create",return_value=session) as create:
                result=await payments.checkout(g,USER)
        self.assertEqual(result,{"url":"https://checkout.stripe.com/example"})
        self.assertEqual(create.call_count,1)
        kwargs=create.call_args.kwargs
        self.assertEqual(kwargs["client_reference_id"],str(USER))
        self.assertEqual(kwargs["line_items"],[{"price":"price_test_example","quantity":1}])
        self.assertEqual(kwargs["mode"],"subscription")
        self.assertEqual(kwargs["subscription_data"]["metadata"]["gallery_uid"],str(USER))
        self.assertEqual(kwargs["success_url"],"https://t.me/Descargas_Robot?start=galerias")
        self.assertEqual(db.execute.await_count,2)
        self.assertIn("INSERT INTO wg_checkouts",db.execute.await_args_list[0].args[0])
        self.assertIn("UPDATE wg_checkouts",db.execute.await_args_list[1].args[0])

    def test_payment_ui_preserves_existing_user_galleries_and_buttons(self):
        script=(ROOT/"app.js").read_text(encoding="utf-8")
        html=(ROOT/"index.html").read_text(encoding="utf-8")
        self.assertIn('$("enter").textContent="COMPRAR MEMBRESÍA"',script)
        self.assertIn('S.me.checkout_enabled',script)
        self.assertIn('S.me.checkout_ready',script)
        self.assertIn('api("checkout")',script)
        self.assertIn('if(S.me.allowed){show("library");return;}',script)
        self.assertIn('const enter=$("enter");enter.disabled=true;',script)
        self.assertIn('finally{enter.disabled=false;}',script)
        self.assertIn('app.js?v=swiftgram-customer-navigation-20261112-v2',html)


if __name__=="__main__":
    unittest.main()
