import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,patch
from descargas_app.payment_notices import receipt_text,send_pending

class NoticeTests(unittest.IsolatedAsyncioTestCase):
    def test_format_escapes_names_and_uses_billing_country_and_new_york_time(self):
        text=receipt_text(10,{'name':'Buyer <&>','email':'x@example.test','paid_at':1792454400,'billing_address':{'city':'Homestead','state':'FL','country':'US'}},{'first_name':'Juan','username':'juan'})
        self.assertEqual(text.count('tg://user?id=10'),2)
        self.assertIn('Buyer &lt;&amp;&gt;',text)
        self.assertIn('Homestead, FL',text);self.assertIn('Estados Unidos (facturación)',text)
        self.assertIn('@juan',text);self.assertIn('20:00:00',text)
        self.assertNotIn('t.me/+',text)
    async def test_only_claim_winner_sends_and_transport_error_is_not_retried(self):
        pool=NS(fetch=AsyncMock(return_value=[{'invoice':'in_1','uid':10,'created':1000,'payload':b'x'}]),fetchval=AsyncMock(return_value=None),execute=AsyncMock())
        service=NS(bot_id=123,repo=NS(pool=pool,decode=Mock(return_value={'paid_at':1000})),api=AsyncMock(return_value={'first_name':'Juan'}))
        with patch.dict('os.environ',{'DESCARGAS_PAYMENT_GROUP':'-1004457751225'}):
            await send_pending(service)
            self.assertEqual(service.api.await_count,1)
            pool.fetchval.return_value='in_1'
            service.api.side_effect=[{'first_name':'Juan'},TimeoutError()]
            await send_pending(service)
            self.assertIn("state='review'",pool.execute.call_args.args[0])
