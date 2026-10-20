import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from descargas_app.billing import Billing


class PaymentNavigationTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        repo = SimpleNamespace(is_blocked=AsyncMock(return_value=False),
            member_until=AsyncMock(return_value=0),
            payment_intent=AsyncMock(return_value={'token': 'd_test', 'url': 'https://t.me/invoice/test'}),
            setting=AsyncMock(return_value='{"ready":true}'))
        service = SimpleNamespace(repo=repo, bot_id=5, api=AsyncMock(), tell=AsyncMock())
        return service

    async def test_card_and_method_selection_edit_same_message(self):
        service = self.service()
        billing = Billing(service)
        billing.group_update = AsyncMock(return_value=False)
        query = {'id': 'query', 'from': {'id': 123},
                 'message': {'message_id': 45, 'chat': {'id': 123}}, 'data': 'pay:stripe'}
        with patch.dict('os.environ', {'DESCARGAS_PAYMENT_LINK': 'https://buy.stripe.com/test'}):
            self.assertTrue(await billing.update({'callback_query': query}))
        edit = service.api.await_args_list[-1]
        self.assertEqual(edit.args[0], 'editMessageText')
        self.assertEqual(edit.args[1]['message_id'], 45)
        self.assertEqual(edit.args[1]['reply_markup']['inline_keyboard'][1][0]['callback_data'], 'pay:methods')
        self.assertIn('𝐏𝐫𝐞𝐜𝐢𝐨: $6.99 USD/mes', edit.args[1]['text'])
        service.api.reset_mock()
        query['data'] = 'pay:methods'
        with patch('descargas_app.moderation.authorized', return_value=False):
            self.assertTrue(await billing.update({'callback_query': query}))
        edit = service.api.await_args_list[-1]
        self.assertEqual(edit.args[0], 'editMessageText')
        self.assertEqual(edit.args[1]['message_id'], 45)
        self.assertEqual(edit.args[1]['reply_markup']['inline_keyboard'][0][0]['url'], 'https://t.me/invoice/test')
        self.assertNotIn('sendMessage', [call.args[0] for call in service.api.await_args_list])

    async def test_cannot_edit_another_users_payment_message(self):
        service = self.service()
        billing = Billing(service)
        billing.group_update = AsyncMock(return_value=False)
        await billing.update({'callback_query': {'id': 'q', 'from': {'id': 123},
            'message': {'message_id': 45, 'chat': {'id': 999}}, 'data': 'pay:methods'}})
        service.api.assert_awaited_once_with('answerCallbackQuery', {'callback_query_id': 'q'})
