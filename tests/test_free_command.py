import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from descargas_app.moderation import command


class FreeCommandTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        return SimpleNamespace(bot_id=5, almacen=-100123, api=AsyncMock(),
                               repo=SimpleNamespace(policy_set=AsyncMock()))

    def message(self, text):
        return {'message_id': 1, 'chat': {'id': 10, 'type': 'private'},
                'from': {'id': 10}, 'text': text}

    async def test_free_and_pay_toggle_only_user_pay_policy(self):
        service = self.service()
        with patch('descargas_app.moderation.authorized', return_value=True):
            self.assertTrue(await command(service, self.message('/free 123')))
            service.repo.policy_set.assert_awaited_once_with(5, 123, 'pay', False, 10)
            service.repo.policy_set.reset_mock()
            self.assertTrue(await command(service, self.message('/pay 123')))
            service.repo.policy_set.assert_awaited_once_with(5, 123, 'pay', True, 10)

    async def test_free_requires_admin_and_positive_user_id(self):
        service = self.service()
        with patch('descargas_app.moderation.authorized', return_value=False):
            await command(service, self.message('/free 123'))
        service.repo.policy_set.assert_not_awaited()
        with patch('descargas_app.moderation.authorized', return_value=True):
            await command(service, self.message('/free -100123'))
        service.repo.policy_set.assert_not_awaited()
