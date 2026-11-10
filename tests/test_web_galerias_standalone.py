"""Regresión: el servicio CLOUDFLARE no modifica tablas ni sesiones de DESCARGAS."""
import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from cryptography.fernet import Fernet
from unittest.mock import AsyncMock, patch
from galerias_standalone import GalleryRepository, IsolatedGalleryService


class IsolatedDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_selects_the_existing_account_table(self):
        conn=type("Connection", (), {})()
        conn.fetchval=AsyncMock(return_value=0)
        conn.execute=AsyncMock()
        pool=type("Pool", (), {})()

        @asynccontextmanager
        async def acquire():
            yield conn

        pool.acquire=acquire
        pool.close=AsyncMock()
        repo=GalleryRepository("postgresql://user:pwd@localhost/db",None,pool_size=3)
        with patch("galerias_standalone.asyncpg.create_pool",new=AsyncMock(return_value=pool)):
            await repo.start()
        conn.fetchval.assert_awaited_once_with("SELECT count(*) FROM descarga_accounts WHERE FALSE")
        conn.execute.assert_not_awaited()
        await repo.close()

    async def test_runtime_config_is_reused_without_duplicate_bot_secrets(self):
        cipher_key=Fernet.generate_key().decode()
        with patch.dict(os.environ,{
            "DESCARGAS_SESSION_KEY":cipher_key,
            "DATABASE_URL":"postgresql://user@database/descargas",
            "GALERIAS_AUTH_BOT_TOKEN":"",
            "ID":"","HASH":""
        }):
            service=IsolatedGalleryService()
            encrypted=service.repo.encode({
                "token":"123456:TEST_BOT_TOKEN", "api_id":"12345678",
                "api_hash":"abcdef0123456789abcdef0123456789",
            }).decode()
            service.repo.start=AsyncMock()
            service.repo.setting=AsyncMock(return_value=encrypted)
            service.repo.close=AsyncMock()
            with patch("galerias_standalone.verify_r2",new=AsyncMock()):
                app={}
                await service.start(app)
                self.assertTrue(service.ready)
                self.assertEqual(service.token,"123456:TEST_BOT_TOKEN")
                self.assertEqual(service.api_id,"12345678")
                self.assertEqual(service.api_hash,"abcdef0123456789abcdef0123456789")
                service.repo.setting.assert_awaited_once_with("runtime_config")
                await service.close(app)

    async def test_missing_secrets_fail_closed(self):
        with patch.dict(os.environ, {
            "DESCARGAS_SESSION_KEY":"","DATABASE_URL":"","GALERIAS_AUTH_BOT_TOKEN":""
        }):
            instance=IsolatedGalleryService()
            instance.ready=False
            with patch("galerias_standalone.verify_r2",new=AsyncMock()):
                app={}
                await instance.start(app)
                self.assertFalse(instance.ready)
                self.assertIsNone(instance.repo)
                await instance.close(app)

if __name__=="__main__":unittest.main()
