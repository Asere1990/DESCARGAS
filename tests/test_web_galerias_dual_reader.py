"""A real Telegram ID is shared across DATOS/DESCARGAS in Web Galerías.

Only the gallery reader uses these helpers. No real user session, bot or
external database is modified by this test.
"""
import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from web_galerias import on_demand,sync
from web_galerias.reader_sessions import gallery_connected


class DualReaderSelection(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_datos_is_not_rejected_merely_for_existing_in_descargas(self):
        pool=N(fetchval=AsyncMock(side_effect=AssertionError(
            "A duplicate-account existence query must not reject DATOS")))
        g=N(s=N(datos=N(ready=True)),db=lambda:pool,
            priority_accounts_in_use=set(),
            priority_source_lock=asyncio.Lock())
        with patch.object(sync,"choose_account",new=AsyncMock(return_value=-804200001)),\
             patch.object(sync,"source_free",new=AsyncMock(return_value=True)):
            selected=await on_demand._select_source(g,{
                "id":4,"preferred_uid":-804200001})
        self.assertEqual(selected,-804200001)
        self.assertEqual(g.priority_accounts_in_use,{804200001})
        pool.fetchval.assert_not_awaited()

    async def test_same_real_account_cannot_claim_two_priority_slots(self):
        pool=N(fetch=AsyncMock(return_value=[{"uid":-804200001}]))
        g=N(s=N(datos=N(ready=True)),db=lambda:pool,
            priority_accounts_in_use={804200001},
            priority_source_lock=asyncio.Lock())
        with patch.object(sync,"choose_account",new=AsyncMock(return_value=804200001)),\
             patch.object(sync,"source_free",new=AsyncMock(return_value=True)):
            selected=await on_demand._select_source(g,{
                "id":4,"preferred_uid":None})
        self.assertIsNone(selected)
        self.assertEqual(g.priority_accounts_in_use,{804200001})

    async def test_distinct_real_accounts_remain_parallel_candidates(self):
        pool=N(fetch=AsyncMock(return_value=[{"uid":-804200002}]))
        g=N(s=N(datos=N(ready=True)),db=lambda:pool,
            priority_accounts_in_use={804200001},
            priority_source_lock=asyncio.Lock())
        with patch.object(sync,"choose_account",new=AsyncMock(return_value=804200001)),\
             patch.object(sync,"source_free",new=AsyncMock(return_value=True)):
            selected=await on_demand._select_source(g,{
                "id":4,"preferred_uid":None})
        self.assertEqual(selected,-804200002)
        self.assertEqual(g.priority_accounts_in_use,{804200001,804200002})

    async def test_background_round_serializes_same_uid_but_parallelizes_other_uids(self):
        folders=[
            {"id":1,"source_uid":804200001},
            {"id":2,"source_uid":-804200001},
            {"id":3,"source_uid":-804200002},
            {"id":4,"source_uid":804200003},
        ]
        live=set()
        max_live=0
        seen=set()
        async def fake_sync(g,folder,**kw):
            nonlocal max_live
            uid=abs(folder["source_uid"])
            self.assertNotIn(uid,live)
            live.add(uid)
            seen.add(folder["id"])
            max_live=max(max_live,len(live))
            await asyncio.sleep(.012)
            live.remove(uid)
        with patch.object(sync,"sync_folder",side_effect=fake_sync),\
             patch.dict(os.environ,{"GALERIAS_SYNC_BETWEEN_FOLDERS":"0",
                                    "GALERIAS_SYNC_PARALLEL_ACCOUNTS":"3"}):
            await sync.fair_round(N(closed=False),folders,phase="archive")
        self.assertEqual(seen,{1,2,3,4})
        self.assertEqual(max_live,3)

    async def test_priority_from_datos_preempts_background_using_same_real_uid(self):
        db=N(fetchval=AsyncMock(return_value=True))
        g=N(db=lambda:db,closed=False,storage=N(ready=True),
            priority_task=N(done=lambda:False))
        with patch.dict(os.environ,{"GALERIAS_SYNC_ENABLED":"1"}):
            self.assertTrue(await on_demand.has_urgent(g,804200001))
            self.assertTrue(await on_demand.has_urgent(g,-804200001))
        query,args0,args1=db.fetchval.await_args.args
        self.assertIn("ABS(f.preferred_uid)=ABS($2::bigint)",query)
        self.assertEqual(args1,-804200001)


class RealSharedLockPostgres(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url=os.getenv("TEST_DATABASE_URL")
        if not url:
            self.skipTest("TEST_DATABASE_URL solo en CI")
        import asyncpg
        from descargas_app.pg_store import Repository
        self.pool=await asyncpg.create_pool(url,min_size=1,max_size=5)
        repo=N(pool=self.pool)
        repo.account_lock=lambda uid,wait=False: Repository.account_lock(
            repo,uid,wait=wait)
        self.descargas=N(repo=repo)
        self.datos=N(ready=True)
        self.descargas.datos=self.datos
        self.g=N(s=self.descargas)
        @asynccontextmanager
        async def fake_connected(service,uid):
            if service is self.descargas:
                # Simulate the existing production DESCARGAS connected wrapper.
                async with repo.account_lock(uid,wait=False):
                    yield N(id=uid)
            else:
                # DATOS has its own isolated pool/lock and session already.
                yield N(id=uid)
        self.stub=fake_connected
        self.patcher=patch("descargas_app.account_chat.connected",new=self.stub)
        self.patcher.start()

    async def asyncTearDown(self):
        if hasattr(self,"patcher"):
            self.patcher.stop()
        if hasattr(self,"pool"):
            await self.pool.close()

    async def test_descargas_active_prevents_same_datos_account_but_not_another(self):
        uid=804200001
        async with gallery_connected(self.g,uid):
            # DATOS same real Telegram UID must retry, not open a second session.
            with self.assertRaisesRegex(ValueError,"Espera a que termine"):
                async with gallery_connected(self.g,-uid):
                    self.fail("duplicate Telegram session opened")
            # Another linked DATOS account can work at this very moment.
            async with gallery_connected(self.g,-(uid+1)) as client:
                self.assertEqual(client.id,uid+1)
        async with gallery_connected(self.g,-uid) as client:
            self.assertEqual(client.id,uid)

    async def test_datos_active_prevents_descargas_of_same_uid(self):
        uid=804200002
        async with gallery_connected(self.g,-uid):
            with self.assertRaisesRegex(ValueError,"Espera a que termine"):
                async with gallery_connected(self.g,uid):
                    self.fail("duplicate Telegram session opened")
        async with gallery_connected(self.g,uid) as client:
            self.assertEqual(client.id,uid)


if __name__=="__main__":
    unittest.main()
