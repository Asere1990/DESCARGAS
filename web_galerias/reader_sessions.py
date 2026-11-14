"""Coordinate Web Galerías reader sessions by the REAL Telegram account.

DATOS and DESCARGAS use separate databases and independent session locks.
For a DATOS reader, also hold the original DESCARGAS account lock for the
duration of the Telegram connection. This serializes identical Telegram IDs
across both providers (including DESCARGAS's active worker sessions), while
different real accounts can continue working in parallel.

Never write to either linked account, change a session, or require the account
to be absent from the other system.
"""
from contextlib import asynccontextmanager

from descargas_app.account_chat import connected


@asynccontextmanager
async def gallery_connected(g, signed_uid):
    """Use one account from either provider, never concurrently from both.

    The existing source-specific connected context still verifies the
    Telegram authorization and owns that provider's normal session lock.
    The additional shared lock is needed only for DATOS, because DESCARGAS
    already holds the main database's lock inside connected.
    """
    uid=int(signed_uid)
    if uid == 0:
        raise ValueError("La cuenta de Telegram no está asignada.")
    real_uid=abs(uid)
    if uid > 0:
        async with connected(g.s,real_uid) as client:
            yield client
        return
    datos=getattr(g.s,"datos",None)
    if not getattr(datos,"ready",False):
        raise ValueError("DATOS no está disponible para leer esa cuenta.")
    # Lock the same real Telegram UID used by DESCARGAS, not its negative
    # provider-specific database key. An existing DESCARGAS download blocks
    # this DATOS attempt temporarily; it NEVER bans or revokes the account.
    async with g.s.repo.account_lock(real_uid,wait=False):
        async with connected(datos,real_uid) as client:
            yield client
