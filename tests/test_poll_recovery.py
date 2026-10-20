import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from descargas_app.scalable import ScaleService, TelegramDeliveryError


class PollRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_webhook_setup_does_not_block_start_command(self):
        @asynccontextmanager
        async def leader(bot_id):
            yield
        repo = SimpleNamespace(leader=leader, setting=AsyncMock(return_value='100'), set_setting=AsyncMock())
        service = SimpleNamespace(stopping=False, bot_id=5, repo=repo, tasks=[],
                                  configure_bot_menu=AsyncMock())
        update = {'update_id': 100, 'message': {'text': '/start'}}
        async def api(method, payload):
            if method == 'deleteWebhook':
                raise TelegramDeliveryError('Telegram rejected setup')
            self.assertEqual(method, 'getUpdates')
            return [update]
        async def process(received):
            self.assertEqual(received, update)
            service.stopping = True
        service.api = AsyncMock(side_effect=api)
        service.update = AsyncMock(side_effect=process)
        await ScaleService.poll(service)
        await asyncio.gather(*service.tasks)
        service.update.assert_awaited_once_with(update)
        repo.set_setting.assert_awaited_once_with('offset:5', 101)
        self.assertTrue(service.polling_confirmed)

    async def test_menu_configuration_does_not_hold_up_polling(self):
        @asynccontextmanager
        async def leader(bot_id):
            yield
        service = SimpleNamespace(stopping=False, bot_id=5, tasks=[], webhook_cleared=True,
            repo=SimpleNamespace(leader=leader, setting=AsyncMock(return_value='100'), set_setting=AsyncMock()))
        async def menu():
            await asyncio.Event().wait()
        service.configure_bot_menu = menu
        async def api(method, payload):
            self.assertEqual(method, 'getUpdates')
            service.stopping = True
            return []
        service.api = api
        await asyncio.wait_for(ScaleService.poll(service), 1)
        for task in service.tasks:task.cancel()
        await asyncio.gather(*service.tasks, return_exceptions=True)
