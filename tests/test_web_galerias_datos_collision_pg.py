"""Real PostgreSQL regression: honor an explicitly selected DATOS duplicate.

Uses only the isolated localhost /descargas_test test database. All tables are
TEMPORARY; no production data, Telegram sessions, or workers are used.
"""
import os
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import asyncpg
from web_galerias.sync import choose_account

URL=os.getenv("TEST_DATABASE_URL","")


@unittest.skipUnless(URL,"Requires an isolated local PostgreSQL test database")
class ExplicitDatosPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed=urlparse(URL)
        if parsed.hostname not in ("localhost","127.0.0.1") or parsed.path!="/descargas_test":
            raise RuntimeError("Refusing to run this test against a real database")
        self.conn=await asyncpg.connect(URL)
        try:
            await self.conn.execute("""
                CREATE TEMP TABLE descarga_accounts (
                    uid BIGINT PRIMARY KEY, session BYTEA
                );
                CREATE TEMP TABLE wg_discovery (
                    folder BIGINT NOT NULL, uid BIGINT NOT NULL,
                    tried_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                    works BOOLEAN NOT NULL DEFAULT FALSE,
                    next_after DOUBLE PRECISION NOT NULL DEFAULT 0,
                    PRIMARY KEY(folder,uid)
                );
                CREATE TEMP TABLE wg_account_cooldown (
                    uid BIGINT PRIMARY KEY,
                    available_at DOUBLE PRECISION NOT NULL DEFAULT 0
                );
            """)
            await self.conn.execute(
                "INSERT INTO descarga_accounts(uid,session) VALUES($1,$2)",
                222,b"primary-session-present")
        except BaseException:
            await self.conn.close()
            raise
        self.datos=N(ready=True,account_keys=AsyncMock(return_value=[-222]),
                     has_account=AsyncMock(return_value=True))
        self.gallery=N(s=N(datos=self.datos),db=lambda:self.conn)

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_explicit_datos_wins_even_when_same_uid_connected_to_descargas(self):
        # The same Telegram ID exists in both DBs. Explicit DATOS must win.
        selected=await choose_account(self.gallery,dict(
            id=7,source_uid=-222,preferred_uid=-222))
        self.assertEqual(selected,-222)

    async def test_explicit_datos_is_available_before_first_scan(self):
        selected=await choose_account(self.gallery,dict(
            id=8,source_uid=None,preferred_uid=-222))
        self.assertEqual(selected,-222)

    async def test_auto_selection_keeps_original_descargas_deduplication(self):
        selected=await choose_account(self.gallery,dict(
            id=9,source_uid=None,preferred_uid=None))
        self.assertEqual(selected,222)

    async def test_datos_only_user_remains_available(self):
        await self.conn.execute("DELETE FROM descarga_accounts WHERE uid=222")
        selected=await choose_account(self.gallery,dict(
            id=10,source_uid=None,preferred_uid=-222))
        self.assertEqual(selected,-222)


if __name__=="__main__":
    unittest.main()
