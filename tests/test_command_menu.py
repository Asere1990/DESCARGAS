import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from descargas_app.command_menu import configure_commands


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_police_only_see_menu_including_police_outside_admins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'admins.txt'
            path.write_text('123\n456\n')
            path.with_name('police.txt').write_text('123 # both roles\n789, 987 # police only\n')
            service = SimpleNamespace(bot_id=99, api=AsyncMock(), repo=SimpleNamespace(
                setting=AsyncMock(return_value='[111]'), set_setting=AsyncMock()))
            with patch('descargas_app.moderation.ADMIN_FILE', path):
                await configure_commands(service)
            payloads = [call.args[1] for call in service.api.await_args_list]
            self.assertEqual([c['command'] for c in payloads[0]['commands']], ['mi_cuenta'])
            scoped = {p['scope']['chat_id']: p['commands'] for p in payloads[1:]}
            for uid in (123, 789, 987):
                self.assertEqual([c['command'] for c in scoped[uid]], ['menu'])
            self.assertIn('chat', [c['command'] for c in scoped[456]])
            self.assertEqual([c['command'] for c in scoped[111]], ['mi_cuenta'])
            service.repo.set_setting.assert_awaited_once_with('command_menu_admins:99', '[123, 456, 789, 987]')

    async def test_removed_police_returns_to_regular_admin_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'admins.txt'
            path.write_text('123\n')
            path.with_name('police.txt').write_text('')
            service = SimpleNamespace(bot_id=99, api=AsyncMock(), repo=SimpleNamespace(
                setting=AsyncMock(return_value='[123, 789]'), set_setting=AsyncMock()))
            with patch('descargas_app.moderation.ADMIN_FILE', path):
                await configure_commands(service)
            scoped = {c.args[1]['scope']['chat_id']: c.args[1]['commands']
                      for c in service.api.await_args_list if 'scope' in c.args[1]}
            self.assertIn('chat', [c['command'] for c in scoped[123]])
            self.assertEqual([c['command'] for c in scoped[789]], ['mi_cuenta'])

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
