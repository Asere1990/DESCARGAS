import time,unittest
from contextlib import asynccontextmanager
from datetime import datetime,timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from descargas_app.subscription_actions import expiry_date,cancel,disputes

class SubscriptionActions(unittest.IsolatedAsyncioTestCase):
    def test_spanish_date(self):
        stamp=datetime(2026,10,20,12,tzinfo=timezone.utc).timestamp()
        self.assertEqual(expiry_date(stamp),'20-Octubre-2026')
    async def test_cancel_uses_authenticated_owner_and_keeps_paid_expiry(self):
        until=time.time()+86400
        pool=NS(fetch=AsyncMock(return_value=[{'provider':'stripe','subscription':'sub_owned','paid_until':until}]))
        service=NS(bot_id=123,repo=NS(pool=pool))
        client=NS(v1=NS(subscriptions=NS(update_async=AsyncMock())))
        @asynccontextmanager
        async def fake():yield client
        with patch('descargas_app.subscription_actions.stripe_client',fake):result=await cancel(service,10)
        self.assertEqual(pool.fetch.call_args.args[1:3],(123,10))
        client.v1.subscriptions.update_async.assert_awaited_once_with('sub_owned',params={'cancel_at_period_end':True})
        self.assertEqual(result['until'],expiry_date(until))
    async def test_cancel_failure_is_not_reported_as_success(self):
        pool=NS(fetch=AsyncMock(return_value=[{'provider':'stars','subscription':'charge_owned','paid_until':time.time()+100}]))
        service=NS(bot_id=123,repo=NS(pool=pool),api=AsyncMock(side_effect=RuntimeError('failure')))
        with self.assertRaises(RuntimeError):await cancel(service,10)
    async def test_dispute_resolves_charge_to_exact_subscription_owner(self):
        pool=NS(fetch=AsyncMock(side_effect=[[{'id':'dp_test','charge':'ch_test'}],[{'bot_id':123,'uid':10}]]),execute=AsyncMock())
        client=NS(v1=NS(charges=NS(retrieve_async=AsyncMock(return_value={'payment_intent':'pi_test'})),invoice_payments=NS(list_async=AsyncMock(return_value={'data':[{'invoice':{'parent':{'subscription_details':{'subscription':'sub_owned'}}}}]}))))
        @asynccontextmanager
        async def fake():yield client
        with patch('descargas_app.subscription_actions.stripe_client',fake):await disputes(NS(repo=NS(pool=pool)))
        self.assertEqual(pool.fetch.call_args.args[1],['sub_owned'])
        self.assertEqual(pool.execute.call_args_list[0].args[1:],(123,10,'dp_test'))
