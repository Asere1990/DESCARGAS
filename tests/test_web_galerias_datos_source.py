"""Isolated dual-source gallery regression tests: no Telegram network access."""
import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch

from aiohttp import web
from web_galerias.datos_source import DatosReader
from web_galerias.sync import choose_account, source_free, sync_folder, fair_round
from web_galerias.admin import linked_source_account


class DualSourceSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_credentials_leave_datos_disabled_and_gallery_intact(self):
        with patch.dict(os.environ,{
            "DATOS_DB":"postgres://unreachable/never_use",
            "DATOS_KEY":"",
            "GALERIAS_DATOS_DATABASE_URL":"",
            "GALERIAS_DATOS_SESSION_KEY":""}):
            reader=DatosReader()
            await reader.start()
            self.assertFalse(reader.ready)
            self.assertEqual(await reader.account_keys(),[])
            self.assertIsNone(reader.repo)

    async def test_short_datos_env_names_take_precedence_over_old_aliases(self):
        from cryptography.fernet import Fernet
        key=Fernet.generate_key().decode()
        with patch.dict(os.environ,{
            "DATOS_DB":"postgresql://datos-db/private",
            "DATOS_KEY":key,
            "GALERIAS_DATOS_DATABASE_URL":"postgresql://wrong/old",
            "GALERIAS_DATOS_SESSION_KEY":"not-a-fernet-key"}):
            reader=DatosReader()
            self.assertEqual(reader.db_url,"postgresql://datos-db/private")
            self.assertIsNotNone(reader.cipher)
            self.assertEqual(reader.cipher.decrypt(reader.cipher.encrypt(b"test")),b"test")

    async def test_chooser_queries_both_databases_but_deduplicates_same_telegram_uid(self):
        db=N(fetchrow=AsyncMock(return_value={"uid":-222}))
        datos=N(ready=True,account_keys=AsyncMock(return_value=[-111,-222]),
                has_account=AsyncMock(return_value=True))
        gallery=N(s=N(datos=datos),db=lambda:db)
        folder={"id":7,"source_uid":None,"preferred_uid":None}
        self.assertEqual(await choose_account(gallery,folder),-222)
        sql,*args=db.fetchrow.await_args.args
        self.assertIn("unnest($5::bigint[])",sql)
        self.assertIn("a0.uid=-d.uid AND a0.session IS NOT NULL",sql)
        self.assertIn("COALESCE(d.next_after,0)<=$2",sql)
        self.assertEqual(args[-1],[-111,-222])
        self.assertEqual(args[:4],[7,args[1],None,None])

    async def test_explicit_datos_source_is_not_suppressed_by_same_uid_in_descargas(self):
        """An admin-pinned DATOS account must be queried, not discarded.

        This is the exact failure mode: a positive UID in DESCARGAS and the
        same UID prefixed DATOS: in Web Galerías. Do not touch either session.
        """
        db=N(fetchrow=AsyncMock(return_value={"uid":-222}))
        datos=N(ready=True,account_keys=AsyncMock(return_value=[-222]))
        gallery=N(s=N(datos=datos),db=lambda:db)
        folder={"id":7,"source_uid":-222,"preferred_uid":-222}
        self.assertEqual(await choose_account(gallery,folder),-222)
        sql,*args=db.fetchrow.await_args.args
        self.assertIn("d.uid=$4 OR NOT EXISTS",sql)
        self.assertIn("a0.uid=-d.uid AND a0.session IS NOT NULL",sql)
        self.assertIn("($4::bigint IS NULL OR a.uid=$4)",sql)
        self.assertEqual(args[-1],[-222])
        self.assertEqual(args[3],-222)

    async def test_unpinned_datos_duplicate_remains_deduplicated(self):
        db=N(fetchrow=AsyncMock(return_value={"uid":222}))
        datos=N(ready=True,account_keys=AsyncMock(return_value=[-222]))
        gallery=N(s=N(datos=datos),db=lambda:db)
        folder={"id":8,"source_uid":None,"preferred_uid":None}
        self.assertEqual(await choose_account(gallery,folder),222)
        sql,*args=db.fetchrow.await_args.args
        self.assertIn("d.uid=$4 OR NOT EXISTS",sql)
        self.assertIsNone(args[3])

    async def test_missing_datos_source_preserves_primary_selection(self):
        db=N(fetchrow=AsyncMock(return_value={"uid":123}))
        gallery=N(s=N(),db=lambda:db)
        folder={"id":7,"source_uid":None,"preferred_uid":None}
        self.assertEqual(await choose_account(gallery,folder),123)
        sql,*args=db.fetchrow.await_args.args
        self.assertIn("FROM descarga_accounts a",sql)
        self.assertEqual(len(args),4)

    async def test_datos_download_jobs_block_datos_gallery_but_not_descargas_db(self):
        primary=N(fetchval=AsyncMock(return_value=True))
        datos_pool=N(fetchval=AsyncMock(return_value=True))
        gallery=N(closed=False,s=N(datos=N(ready=True,repo=N(pool=datos_pool))),
                  db=lambda:primary)
        with patch.dict(os.environ,{"GALERIAS_SYNC_ENABLED":"1"}):
            self.assertFalse(await source_free(gallery,-123456,7))
        self.assertEqual(primary.fetchval.await_count,1)
        self.assertEqual(datos_pool.fetchval.await_count,1)
        self.assertEqual(datos_pool.fetchval.await_args.args[-1],123456)

    async def test_datos_gallery_uses_its_own_service_and_positive_telegram_uid(self):
        db=N(fetchval=AsyncMock(return_value=True),execute=AsyncMock())
        datos=N(ready=True)
        gallery=N(closed=False,s=N(datos=datos),db=lambda:db,storage=N(ready=True))
        seen=[]
        @asynccontextmanager
        async def fake_connected(service,uid):
            seen.append((service,uid))
            yield N()
        folder={"id":7,"peer":-10012345,"source_uid":None,"preferred_uid":None}
        count_calls=AsyncMock()
        patches=[
            patch("web_galerias.sync.folder_active",new=AsyncMock(return_value=True)),
            patch("web_galerias.sync.choose_account",new=AsyncMock(return_value=-222)),
            patch("web_galerias.sync.source_free",new=AsyncMock(return_value=True)),
            patch("descargas_app.account_chat.connected",new=fake_connected),
            patch("web_galerias.sync.find_dialog",new=AsyncMock(return_value="peer")),
            patch("web_galerias.sync.mark_discovery",new=AsyncMock()),
            patch("web_galerias.sync.refresh_telegram_counts",new=count_calls)
        ]
        for p in patches:p.start();self.addCleanup(p.stop)
        await sync_folder(gallery,folder,phase="counts")
        self.assertEqual(seen,[(datos,222)])
        count_calls.assert_awaited_once()

    async def test_admin_validates_only_the_selected_database(self):
        db=N(fetchval=AsyncMock(return_value=True))
        datos=N(ready=True,has_account=AsyncMock(return_value=True))
        gallery=N(s=N(datos=datos),db=lambda:db)
        self.assertEqual(await linked_source_account(gallery,222222,"datos"),-222222)
        db.fetchval.assert_not_awaited()
        datos.has_account.assert_awaited_once_with(222222)
        self.assertEqual(await linked_source_account(gallery,333333,"descargas"),333333)
        with self.assertRaises(web.HTTPBadRequest):
            await linked_source_account(gallery,333333,"unknown")

    async def test_bounded_parallel_lanes_include_both_independent_sources(self):
        gallery=N(closed=False,s=N())
        folders=[{"id":1,"source_uid":111},{"id":2,"source_uid":-222},
                 {"id":3,"source_uid":111},{"id":4,"source_uid":-222}]
        active=set();peak=0;phases=[]
        async def run_one(g,folder,**kwargs):
            nonlocal peak
            uid=folder["source_uid"]
            self.assertNotIn(uid,active)
            active.add(uid);peak=max(peak,len(active))
            phases.append(kwargs["phase"])
            await asyncio.sleep(0.005)
            active.remove(uid)
        with patch.dict(os.environ,{"GALERIAS_SYNC_PARALLEL_ACCOUNTS":"2",
                                    "GALERIAS_SYNC_BETWEEN_FOLDERS":"0"}):
            with patch("web_galerias.sync.sync_folder",side_effect=run_one):
                await fair_round(gallery,folders,phase="prime")
        self.assertEqual(peak,2)
        self.assertEqual(phases,["prime"]*4)


if __name__=="__main__":
    unittest.main()
