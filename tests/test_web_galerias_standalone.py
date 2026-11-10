"""Regresión: el servicio CLOUDFLARE no modifica tablas ni sesiones de DESCARGAS."""
import asyncio
import os
import unittest
from contextlib import asynccontextmanager
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
