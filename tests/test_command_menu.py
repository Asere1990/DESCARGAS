import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from descargas_app.command_menu import configure_commands


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_menu_is_private_and_revoked_admin_loses_suggestions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'admins.txt'
            path.write_text('123 # owner\n456\n')
            service = SimpleNamespace(bot_id=99, api=AsyncMock(), repo=SimpleNamespace(
                setting=AsyncMock(return_value='[789]'), set_setting=AsyncMock()))
            with patch('descargas_app.moderation.ADMIN_FILE', path):
                await configure_commands(service)
            payloads = [call.args[1] for call in service.api.await_args_list]
            self.assertEqual([item['command'] for item in payloads[0]['commands']], ['mi_cuenta'])
            scoped = {item['scope']['chat_id']: item for item in payloads[1:]}
            for uid in (123, 456):
                commands = {item['command'] for item in scoped[uid]['commands']}
                self.assertTrue({'datos', 'ban', 'search', 'mi_cuenta', 'comandos'} <= commands)
                self.assertEqual(scoped[uid]['scope']['type'], 'chat')
            self.assertEqual([item['command'] for item in scoped[789]['commands']], ['mi_cuenta'])
            service.repo.set_setting.assert_awaited_once_with('command_menu_admins:99', '[123, 456]')
