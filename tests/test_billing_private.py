import hashlib,hmac,json,time,unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from descargas_app.billing import Billing,PaymentRequired,verified_event,next_month
from descargas_app.media_cache import deliver_cached

class BillingTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        repo=NS(policy=AsyncMock(return_value={'pay':True,'locked':False}),member_until=AsyncMock(return_value=0),
            intent=AsyncMock(return_value={'bot_id':123,'uid':10,'provider':'stars','subscription':None}),
            is_blocked=AsyncMock(return_value=False),grant_stars=AsyncMock(return_value=True),stripe_event=AsyncMock(),queue_buyer=AsyncMock())
        s=NS(repo=repo,bot_id=123,api=AsyncMock(),tell=AsyncMock());s.billing=Billing(s);return s
    async def test_subscription_expiry_and_personal_scope(self):
        s=self.service()
        with self.assertRaises(PaymentRequired):await s.billing.require(10,-100123)
        s.repo.member_until.return_value=time.time()+10
        await s.billing.require(10,-100123)
        s.repo.member_until.assert_awaited_with(123,10)
    async def test_precheckout_rejects_other_user_currency_amount_or_existing_membership(self):
        for changes in ({'from':{'id':20}},{'currency':'USD'},{'total_amount':499},{}):
            s=self.service();s.repo.member_until.return_value=time.time()+100
            pre={'id':'q','from':{'id':10},'currency':'XTR','total_amount':500,'invoice_payload':'d_token',**changes}
            await s.billing.update({'pre_checkout_query':pre})
            self.assertFalse(s.api.call_args.args[1]['ok'])
        s=self.service();await s.billing.update({'pre_checkout_query':{'id':'q','from':{'id':10},'currency':'XTR','total_amount':500,'invoice_payload':'d_token'}})
        self.assertTrue(s.api.call_args.args[1]['ok'])
    async def test_recurring_receipt_uses_telegram_expiry(self):
        s=self.service();until=int(time.time()+2592000)
        await s.billing.update({'message':{'chat':{'type':'private'},'from':{'id':10},'successful_payment':{'currency':'XTR','total_amount':500,'is_recurring':True,'subscription_expiration_date':until,'invoice_payload':'d_token','telegram_payment_charge_id':'charge'}}})
        s.repo.grant_stars.assert_awaited_once_with(123,10,'d_token','charge',until)
    async def test_group_entry_is_scoped_and_duplicate_updates_are_silent(self):
        s=self.service();s.repo.confirm_payment_join=AsyncMock(return_value=True)
        event={'message':{'chat':{'id':-100123},'new_chat_members':[{'id':10}]}}
        with patch.dict('os.environ',{'DESCARGAS_PAYMENT_GROUP':'-100123'}):
            self.assertTrue(await s.billing.group_update(event))
            s.repo.confirm_payment_join.assert_awaited_once_with(123,10)
            self.assertIn('esperando',s.tell.call_args.args[1])
            s.tell.reset_mock();s.repo.confirm_payment_join.return_value=False
            await s.billing.group_update(event);s.tell.assert_not_awaited()
            event['message']['chat']['id']=-100999
            self.assertFalse(await s.billing.group_update(event))

    async def test_buyer_profile_keeps_only_verified_charge_summary(self):
        from descargas_app.buyer_profiles import enrich_rows
        from unittest.mock import Mock
        data={'invoice_id':'in_test','customer_id':'cus_test'}
        repo=NS(decode=Mock(return_value=dict(data)),encode=Mock(side_effect=lambda x:x),pool=NS(execute=AsyncMock()))
        charge={'id':'ch_test','customer':'cus_test','paid':True,'billing_details':{'name':'Buyer','email':'buyer@example.test'},'payment_method_details':{'type':'card','card':{'last4':'4242','brand':'visa','funding':'credit','fingerprint':'test_fingerprint'}}}
        client=NS(v1=NS(invoice_payments=NS(list_async=AsyncMock(return_value={'data':[{'payment':{'payment_intent':'pi_test'}}]})),payment_intents=NS(retrieve_async=AsyncMock(return_value={'latest_charge':charge}))))
        await enrich_rows(NS(repo=repo),client,[{'invoice':'in_test','payload':b'encrypted'}])
        summary=repo.pool.execute.call_args.args[2]
        self.assertEqual(summary['card_last4'],'4242');self.assertEqual(summary['card_fingerprint'],'test_fingerprint')
        repo.pool.execute.reset_mock();charge['customer']='cus_foreign'
        await enrich_rows(NS(repo=repo),client,[{'invoice':'in_test','payload':b'encrypted'}])
        self.assertIn('available=',repo.pool.execute.call_args.args[0])

    def test_signature_rejects_tampering_and_expiry(self):
        raw=json.dumps({'id':'evt','created':1000}).encode();signature=hmac.new(b'only-test',b'1000.'+raw,hashlib.sha256).hexdigest()
        self.assertEqual(verified_event(raw,'t=1000,v1='+signature,'only-test',now=1000)['id'],'evt')
        for body,now in [(raw+b' ',1000),(raw,1400)]:
            with self.assertRaises(ValueError):verified_event(body,'t=1000,v1='+signature,'only-test',now=now)
    async def test_stripe_filters_foreign_products_unpaid_and_processes_real_period(self):
        s=self.service()
        with patch.dict('os.environ',{'DESCARGAS_STRIPE_PRICE_ID':'price_own','DESCARGAS_STRIPE_LINK_ID':'plink_own'}):
            event={'id':'evt','created':1000,'livemode':True,'type':'invoice.paid','data':{'object':{'id':'in_own','status':'paid','currency':'usd','amount_paid':699,'parent':{'subscription_details':{'subscription':'sub_own'}},'lines':{'data':[{'pricing':{'price_details':{'price':'price_other'}},'period':{'end':2000}}]}}}}
            await s.billing.stripe(event);s.repo.stripe_event.assert_not_awaited()
            event['data']['object']['lines']['data'][0]['pricing']['price_details']['price']='price_own'
            await s.billing.stripe(event);s.repo.stripe_event.assert_awaited_once_with(event,period=('sub_own','in_own',2000))

class PrivateCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_album_reuses_ids_without_almacen_or_outbox(self):
        items=[NS(photo=NS(id=i),document=None,file=NS(size=10),message='caption',entities=[]) for i in (1,2)]
        stored={'items':[{'file_id':'own-photo-'+str(i),'type':'photo'} for i in (1,2)]}
        repo=NS(policy=AsyncMock(return_value={'pay':False,'locked':True}),is_blocked=AsyncMock(return_value=False),
            private_cache=AsyncMock(return_value=stored),remember_media_route=AsyncMock(),media_cache=AsyncMock(),media_ready=AsyncMock())
        s=NS(bot_id=123,almacen=-100999,repo=repo,source_items=AsyncMock(return_value=((-100123,50),items)),before_send=AsyncMock(),api=AsyncMock(return_value=[{'message_id':1},{'message_id':2}]))
        self.assertEqual(await deliver_cached(s,10,None,'link',set()),2)
        method,payload=s.api.call_args.args
        self.assertEqual(method,'sendMediaGroup');self.assertEqual(payload['chat_id'],10)
        self.assertEqual([i['media'] for i in payload['media']],['own-photo-1','own-photo-2'])
        repo.media_cache.assert_not_awaited();repo.media_ready.assert_not_awaited()
