"""Saved-session identity verification must not turn temporary RPC errors into logout."""
import sys
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS, ModuleType
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors
from telethon.client.users import UserMethods
from descargas_app.account_chat import connected


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setup_service(self, me=NS(id=123)):
        self.client=NS(connect=AsyncMock(),disconnect=AsyncMock(),
                       is_user_authorized=AsyncMock(return_value=False),
                       get_me=AsyncMock(return_value=me))
        self.locked=False
        @asynccontextmanager
        async def lock(uid,wait=False):
            self.assertEqual(uid,123);self.assertFalse(wait)
            self.locked=True
            try:yield
            finally:self.locked=False
        self.service=NS(repo=NS(account_lock=lock),get_session=AsyncMock(return_value='existing-session'),client=Mock(return_value=self.client))
        # Only inject the permission boundary. No login or Mini App code is loaded.
        module=ModuleType('descargas_app.creator_access')
        module.require_account=AsyncMock()
        return patch.dict(sys.modules,{'descargas_app.creator_access':module})

    async def test_valid_identity_uses_existing_session_despite_negative_legacy_probe(self):
        with self.setup_service():
            async with connected(self.service,123) as client:
                self.assertIs(client,self.client);self.assertTrue(self.locked)
        self.client.is_user_authorized.assert_not_called()
        self.service.client.assert_called_once_with('existing-session')
        self.client.disconnect.assert_awaited_once()
        self.assertFalse(self.locked)

    async def test_wrong_identity_is_never_exposed(self):
        with self.setup_service(NS(id=999)):
            with self.assertRaisesRegex(ValueError,'no coincide'):
                async with connected(self.service,123):self.fail('wrong account exposed')
        self.client.disconnect.assert_awaited_once()

    async def test_missing_identity_is_not_accepted(self):
        with self.setup_service(None):
            with self.assertRaisesRegex(ValueError,'no reconoce'):
                async with connected(self.service,123):self.fail('unauthorized client exposed')
        self.client.disconnect.assert_awaited_once()

    async def test_temporary_errors_preserve_their_real_type(self):
        for error in [errors.FloodWaitError(None,17),errors.ServerError(None,'temporary',500),TimeoutError()]:
            with self.subTest(error=type(error).__name__),self.setup_service():
                self.client.get_me.side_effect=error
                with self.assertRaises(type(error)):
                    async with connected(self.service,123):self.fail('failed client exposed')
                self.client.disconnect.assert_awaited_once()
                self.assertFalse(self.locked)

    async def test_absent_session_never_creates_a_client(self):
        with self.setup_service():
            self.service.get_session.return_value=''
            with self.assertRaisesRegex(ValueError,'no está conectada'):
                async with connected(self.service,123):self.fail('missing session exposed')
        self.service.client.assert_not_called()

    async def test_telethon_legacy_probe_misclassifies_flood_wait(self):
        class Probe:
            _authorized=None
            async def __call__(self,request):raise errors.FloodWaitError(request,17)
        self.assertFalse(await UserMethods.is_user_authorized(Probe()))
