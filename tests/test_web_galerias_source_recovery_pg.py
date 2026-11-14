"""Safe Web Galerías source lifecycle and ordering on disposable localhost PG only.

Never opens Telegram, touches production records, or deletes Cloudflare R2.
"""
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from urllib.parse import urlparse

import asyncpg
from aiohttp import web
from web_galerias import folder_sources,folder_order

URL=os.environ.get("TEST_DATABASE_URL","")


class Pool:
    def __init__(self,conn):
        self.conn=conn
    @asynccontextmanager
    async def acquire(self):
        yield self.conn
    async def fetch(self,*args):
        return await self.conn.fetch(*args)
    async def fetchrow(self,*args):
        return await self.conn.fetchrow(*args)
    async def fetchval(self,*args):
        return await self.conn.fetchval(*args)
    async def execute(self,*args):
        return await self.conn.execute(*args)


@unittest.skipUnless(URL,"Disposable localhost Postgres required")
class RecoveryPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url=urlparse(URL)
        if url.hostname not in ("localhost","127.0.0.1") or url.path!="/descargas_test":
            raise RuntimeError("Refuse any non-disposable PostgreSQL connection")
        self.conn=await asyncpg.connect(URL)
        await self.conn.execute("""
          CREATE TEMP TABLE wg_folders(
            id BIGINT PRIMARY KEY,peer BIGINT UNIQUE NOT NULL,
            preferred_uid BIGINT,source_uid BIGINT,last_source_uid BIGINT,
            source_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            source_access_status TEXT NOT NULL DEFAULT 'unknown',
            active BOOLEAN NOT NULL DEFAULT TRUE,
            scan_cursor BIGINT NOT NULL DEFAULT 0,
            high_water BIGINT NOT NULL DEFAULT 0,
            complete BOOLEAN NOT NULL DEFAULT FALSE,
            last_check DOUBLE PRECISION NOT NULL DEFAULT 0,
            scan_count BIGINT NOT NULL DEFAULT 0,
            telegram_photos_total BIGINT,telegram_videos_total BIGINT,
            telegram_counts_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            telegram_counts_next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
            telegram_counts_error TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
          );
          CREATE TEMP TABLE wg_folder_sources(
            parent_folder BIGINT NOT NULL,source_folder BIGINT PRIMARY KEY);
          CREATE TEMP TABLE wg_media(
            id BIGINT PRIMARY KEY,folder BIGINT NOT NULL,
            state TEXT NOT NULL,thumb_key TEXT,file_key TEXT,
            error TEXT NOT NULL DEFAULT '',
            available DOUBLE PRECISION NOT NULL DEFAULT 0);
          CREATE TEMP TABLE wg_discovery(folder BIGINT,uid BIGINT);
          CREATE TEMP TABLE wg_source_peers(folder BIGINT,uid BIGINT,handle BYTEA);
          CREATE TEMP TABLE wg_folder_order(
            uid BIGINT NOT NULL,scope TEXT NOT NULL,folder BIGINT NOT NULL,
            position INT NOT NULL,PRIMARY KEY(uid,scope,folder));
          CREATE TEMP TABLE wg_personal_folders(id BIGINT PRIMARY KEY,uid BIGINT NOT NULL);
          INSERT INTO wg_folders(id,peer,preferred_uid,source_uid,scan_cursor,high_water,complete)
          VALUES (1,-100111,101,101,77,130,TRUE),
                 (2,-100222,202,202,22,80,TRUE),
                 (3,-100333,NULL,NULL,0,0,FALSE);
          INSERT INTO wg_folder_sources(parent_folder,source_folder) VALUES(1,2);
          INSERT INTO wg_media(id,folder,state,thumb_key,file_key)
          VALUES(10,1,'ready','r2/ready-thumb','r2/ready-original'),
                (11,1,'preview','r2/preview-thumb',NULL),
                (12,2,'preview','r2/child-thumb',NULL),
                (13,2,'ready','r2/child-ready-thumb','r2/child-original');
          INSERT INTO wg_personal_folders(id,uid) VALUES(51,900),(52,900);
          INSERT INTO wg_discovery(folder,uid) VALUES(1,101),(2,202);
          INSERT INTO wg_source_peers(folder,uid,handle)
          VALUES(1,101,decode('aabb','hex')),(2,202,decode('ccdd','hex'));
        """)
        self.db=Pool(self.conn)
        # A fake store that fails if an R2 delete method is ever used.
        self.r2=N(delete=AsyncMock(side_effect=AssertionError("R2 DELETE FORBIDDEN")),
                  delete_object=AsyncMock(side_effect=AssertionError("R2 DELETE FORBIDDEN")))
        self.g=N(db=lambda:self.db,storage=self.r2,log=AsyncMock(),
                 folder_list=AsyncMock(return_value=[{"id":1},{"id":3}]))
    async def asyncTearDown(self):
        await self.conn.close()

    async def test_revocation_pauses_previews_but_keeps_cursor_for_same_group_handoff(self):
        result=await folder_sources.mark_revoked(self.g,101)
        self.assertEqual(result,[1])
        record=await self.db.fetchrow(
            "SELECT source_enabled,source_access_status,preferred_uid,scan_cursor,high_water FROM wg_folders WHERE id=1")
        self.assertTrue(record["source_enabled"])
        self.assertEqual(record["source_access_status"],"revoked")
        self.assertEqual((record["preferred_uid"],record["scan_cursor"],record["high_water"]),(101,77,130))
        preview=await self.db.fetchrow("SELECT state,thumb_key FROM wg_media WHERE id=11")
        self.assertEqual(dict(preview),{"state":"paused_preview","thumb_key":"r2/preview-thumb"})
        untouched=await self.db.fetchval("SELECT state FROM wg_media WHERE id=12")
        self.assertEqual(untouched,"preview")
        with patch("web_galerias.admin.linked_source_account",new=AsyncMock(return_value=303)),\
             patch.object(folder_sources,"verified_reader",new=AsyncMock(return_value=True)) as verified:
            data=await folder_sources.change_reader(
                self.g,900,{"folder":1,"account_uid":"303"},carry=True)
        verified.assert_awaited_once_with(self.g,303,-100111)
        self.assertTrue(data["progress_preserved"])
        same=await self.db.fetchrow(
            "SELECT preferred_uid,source_uid,scan_cursor,high_water,source_access_status FROM wg_folders WHERE id=1")
        self.assertEqual((same["preferred_uid"],same["source_uid"]), (303,303))
        self.assertEqual((same["scan_cursor"],same["high_water"]),(77,130))
        self.assertEqual(same["source_access_status"],"ok")
        self.assertEqual(await self.db.fetchval("SELECT state FROM wg_media WHERE id=11"),"preview")
        self.assertEqual(await self.db.fetchval("SELECT file_key FROM wg_media WHERE id=10"),"r2/ready-original")
        self.r2.delete.assert_not_awaited()

    async def test_disconnect_hides_unfinished_previews_and_reconnect_starts_zero(self):
        original=await self.db.fetch("SELECT id,thumb_key,file_key FROM wg_media ORDER BY id")
        result=await folder_sources.disconnect(self.g,900,{"folder":1},"source")
        self.assertEqual(result["previews_hidden"],1)
        self.assertTrue(result["r2_preserved"])
        self.assertFalse(await self.db.fetchval("SELECT source_enabled FROM wg_folders WHERE id=1"))
        self.assertIsNone(await self.db.fetchval("SELECT preferred_uid FROM wg_folders WHERE id=1"))
        clean=await self.db.fetchrow("""
          SELECT scan_cursor,high_water,scan_count,complete,last_source_uid,
                 telegram_photos_total,telegram_videos_total,telegram_counts_at,
                 last_check,error FROM wg_folders WHERE id=1""")
        self.assertEqual((clean["scan_cursor"],clean["high_water"],clean["scan_count"]),(0,0,0))
        self.assertFalse(clean["complete"])
        for key in ("last_source_uid","telegram_photos_total","telegram_videos_total"):
            self.assertIsNone(clean[key])
        self.assertEqual(clean["error"],"")
        self.assertEqual(clean["last_check"],0)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM wg_discovery WHERE folder=1"),0)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM wg_source_peers WHERE folder=1"),0)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM wg_discovery WHERE folder=2"),1)
        self.assertEqual(await self.db.fetchval("SELECT state FROM wg_media WHERE id=11"),"pending")
        self.assertEqual(await self.db.fetchval("SELECT state FROM wg_media WHERE id=10"),"ready")
        self.assertEqual(await self.db.fetchval("SELECT state FROM wg_media WHERE id=12"),"preview")
        self.assertEqual(await self.db.fetch("SELECT id,thumb_key,file_key FROM wg_media ORDER BY id"),original)
        with patch("web_galerias.admin.linked_source_account",new=AsyncMock(return_value=-404)),\
             patch.object(folder_sources,"verified_reader",new=AsyncMock(return_value=True)) as verified:
            reply=await folder_sources.change_reader(
                self.g,900,{"folder":1,"account_uid":"404","account_system":"datos"},carry=False)
        self.assertFalse(reply["progress_preserved"])
        verified.assert_awaited_once_with(self.g,-404,-100111)
        actual=await self.db.fetchrow(
            "SELECT source_enabled,preferred_uid,scan_cursor,high_water,complete FROM wg_folders WHERE id=1")
        self.assertEqual((actual["source_enabled"],actual["preferred_uid"],actual["scan_cursor"],
                          actual["high_water"],actual["complete"]),(True,-404,0,0,False))
        self.assertEqual(await self.db.fetch("SELECT id,thumb_key,file_key FROM wg_media ORDER BY id"),original)
        self.r2.delete.assert_not_awaited()

    async def test_add_same_group_id_reactivates_archived_source_without_duplicate(self):
        await folder_sources.disconnect(self.g,900,{"folder":1},"source")
        original=await self.db.fetch("SELECT id,folder,thumb_key,file_key FROM wg_media ORDER BY id")
        with patch("web_galerias.admin.linked_source_account",
                   new=AsyncMock(return_value=707)),\
             patch.object(folder_sources,"verified_reader",
                          new=AsyncMock(return_value=True)) as verify:
            result=await folder_sources.add(self.g,900,{
                "folder":1,"peer":-100111,"account_uid":"707"})
        self.assertTrue(result["reactivated"])
        self.assertFalse(result["progress_preserved"])
        self.assertEqual(result["source_folder"],1)
        verify.assert_awaited_once_with(self.g,707,-100111)
        self.assertTrue(await self.db.fetchval("SELECT source_enabled FROM wg_folders WHERE id=1"))
        self.assertEqual(await self.db.fetchval("SELECT preferred_uid FROM wg_folders WHERE id=1"),707)
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM wg_folders"),3)
        self.assertEqual(await self.db.fetch("SELECT id,folder,thumb_key,file_key FROM wg_media ORDER BY id"),original)
        self.r2.delete.assert_not_awaited()

    async def test_disconnected_cannot_claim_lossless_handoff(self):
        await folder_sources.disconnect(self.g,900,{"folder":1},"account")
        with patch("web_galerias.admin.linked_source_account",new=AsyncMock(return_value=303)),\
             patch.object(folder_sources,"verified_reader",new=AsyncMock()) as verified:
            with self.assertRaises(web.HTTPConflict):
                await folder_sources.change_reader(
                    self.g,900,{"folder":1,"account_uid":303},carry=True)
        verified.assert_not_awaited()

    async def test_admin_global_and_individual_folder_orders_are_isolated(self):
        # The child source 2 is not a public folder: it is grouped under 1.
        with patch("web_galerias.permissions.is_admin",return_value=True):
            await folder_order.save(self.g,900,"public",[3,1],global_order=True)
        items=[{"id":1},{"id":3}]
        self.assertEqual([x["id"] for x in await folder_order.ordered(self.g,901,"public",items)],[3,1])
        await folder_order.save(self.g,901,"public",[1,3])
        self.assertEqual([x["id"] for x in await folder_order.ordered(self.g,901,"public",items)],[1,3])
        self.assertEqual([x["id"] for x in await folder_order.ordered(self.g,902,"public",items)],[3,1])
        await folder_order.save(self.g,900,"personal",[52,51])
        self.assertEqual([x["id"] for x in await folder_order.ordered(
            self.g,900,"personal",[{"id":51},{"id":52}])],[52,51])
        with self.assertRaises(web.HTTPConflict):
            await folder_order.save(self.g,901,"public",[1,999])
        self.assertEqual([x["id"] for x in await folder_order.ordered(self.g,901,"public",items)],[1,3])

    def test_revoked_error_is_not_a_transient_network_or_floodwait(self):
        self.assertTrue(folder_sources.revoked_error(ValueError(
            "La sesión fue revocada. Conecta de nuevo la cuenta.")))
        self.assertFalse(folder_sources.revoked_error(TimeoutError("temporary")))
        self.assertFalse(folder_sources.revoked_error(ValueError("Espera a que termine")))


if __name__=="__main__":
    unittest.main()
