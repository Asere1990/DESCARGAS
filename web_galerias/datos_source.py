"""Independent, read-only DATOS account source for Web Galerías.

This module is only loaded by CLOUDFLARE's standalone process. It does not
change DATOS or DESCARGAS sessions, tokens, queues, or database schema.
Only account/session SELECTs and existing PostgreSQL account advisory locks
are used. Absence or failure of DATOS never blocks the original source.
"""
import logging
import os
import time

import asyncpg
from cryptography.fernet import Fernet

from descargas_app.pg_store import Repository
from descargas_app.service import Service

log = logging.getLogger("web-galerias-datos")


class ReadOnlyDatosRepository(Repository):
    async def start(self):
        # Deliberately do not call Repository.start(): that would migrate DATOS.
        self.pool = await asyncpg.create_pool(
            self.url, min_size=1, max_size=self.pool_size,
            command_timeout=20, max_inactive_connection_lifetime=60)
        try:
            await self.pool.fetchval("SELECT COUNT(*) FROM descarga_accounts WHERE FALSE")
        except BaseException:
            await self.pool.close()
            self.pool = None
            raise


class DatosReader:
    def __init__(self):
        # Only CLOUDFLARE uses these short, source-specific names. Its
        # DATABASE_URL and DESCARGAS_SESSION_KEY stay bound to DESCARGAS.
        # The former longer names remain accepted for a safe transition.
        self.db_url = (os.getenv("DATOS_DB") or
                       os.getenv("GALERIAS_DATOS_DATABASE_URL") or "").strip()
        raw_key = (os.getenv("DATOS_KEY") or
                   os.getenv("GALERIAS_DATOS_SESSION_KEY") or "").strip()
        try:
            self.cipher = Fernet(raw_key.encode()) if raw_key else None
        except (ValueError, TypeError):
            self.cipher = None
            log.warning("DATOS reader: invalid key configuration")
        self.repo = None
        self.api_id = ""
        self.api_hash = ""
        self.ready = False
        self._cached_accounts = ()
        self._cache_at = 0.0

    async def start(self):
        if not (self.db_url and self.cipher):
            log.warning("DATOS reader inactive: isolated database/key not configured")
            return
        repo = ReadOnlyDatosRepository(self.db_url, self.cipher, pool_size=3)
        try:
            await repo.start()
            raw = await repo.setting("runtime_config")
            if not raw:
                raise ValueError("Missing encrypted DATOS runtime configuration")
            snapshot = repo.decode(raw.encode("utf-8"))
            if not isinstance(snapshot, dict):
                raise ValueError("Unexpected DATOS runtime configuration")
            api_id = str(snapshot.get("api_id") or "")
            api_hash = str(snapshot.get("api_hash") or "")
            if not api_id.isdigit() or not api_hash:
                raise ValueError("Missing DATOS Telegram read credentials")
            self.repo = repo
            self.api_id, self.api_hash = api_id, api_hash
            self.ready = True
            log.warning("DATOS reader ready; private sessions remain in DATOS")
        except Exception as exc:
            log.warning("DATOS reader unavailable (%s); original gallery source remains active",
                        type(exc).__name__)
            await repo.close()

    async def close(self):
        self.ready = False
        if self.repo:
            await self.repo.close()
            self.repo = None

    async def account_keys(self):
        """Return stable negative source IDs; the actual Telegram UIDs stay positive."""
        if not self.ready:
            return []
        now = time.monotonic()
        if self._cache_at and now-self._cache_at < 60:
            return list(self._cached_accounts)
        try:
            rows = await self.repo.pool.fetch(
                "SELECT uid FROM descarga_accounts "
                "WHERE session IS NOT NULL AND uid > 0 ORDER BY uid LIMIT 20000")
            keys = tuple(-int(row["uid"]) for row in rows)
            self._cached_accounts = keys
            self._cache_at = now
            return list(keys)
        except Exception as exc:
            log.warning("DATOS account directory temporarily unavailable (%s)",
                        type(exc).__name__)
            return []

    async def has_account(self, uid):
        if not self.ready or not isinstance(uid, int) or uid <= 0:
            return False
        return bool(await self.repo.pool.fetchval(
            "SELECT 1 FROM descarga_accounts WHERE uid=$1 AND session IS NOT NULL",uid))

    async def get_session(self, uid):
        if not self.ready:
            return ""
        row = await self.repo.account(uid)
        return self.cipher.decrypt(bytes(row["session"])).decode() if row and row["session"] else ""

    def client(self, session):
        if not self.ready:
            raise ValueError("DATOS no está conectado a Web Galerías.")
        # Identical read-only Telegram fingerprint and update policy as DESCARGAS.
        return Service.client(self, session)
