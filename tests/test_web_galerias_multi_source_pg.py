"""Only localhost /descargas_test: combined gallery functionality + metadata tests.

No Telegram, R2, Stripe, Render, or user-data access. Database tables are
temporary and confined to one isolated test connection.
"""
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from urllib.parse import urlparse

import asyncpg
from aiohttp import web
from cryptography.fernet import Fernet

from web_galerias import folder_sources,media_text,personal,user_features,downloads,location
from web_galerias.core import GalleryCore

URL=os.environ.get("TEST_DATABASE_URL","")


class TestDB:
    def __init__(self,connection):
        self.conn=connection
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


@unittest.skipUnless(URL,"Local disposable PostgreSQL test database required")
class MultiSourcePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url=urlparse(URL)
        if url.hostname not in ("localhost","127.0.0.1") or url.path!="/descargas_test":
            raise RuntimeError("Refusing to use a real or shared PostgreSQL database")
        self.conn=await asyncpg.connect(URL)
        await self.conn.execute("""
          CREATE TEMP TABLE wg_folders(
            id BIGSERIAL PRIMARY KEY,peer BIGINT UNIQUE NOT NULL,
            name TEXT NOT NULL,public_name TEXT,active BOOLEAN NOT NULL DEFAULT TRUE,
            created DOUBLE PRECISION NOT NULL DEFAULT 0,preferred_uid BIGINT,
            source_uid BIGINT,complete BOOLEAN NOT NULL DEFAULT FALSE,
            error TEXT NOT NULL DEFAULT '',last_check DOUBLE PRECISION NOT NULL DEFAULT 0,
            scan_count BIGINT NOT NULL DEFAULT 0
          );
          CREATE TEMP TABLE wg_folder_sources(
            parent_folder BIGINT NOT NULL REFERENCES wg_folders(id),
            source_folder BIGINT PRIMARY KEY REFERENCES wg_folders(id),
            created DOUBLE PRECISION NOT NULL,
            CHECK (parent_folder<>source_folder)
          );
          CREATE TEMP TABLE wg_media(
            id BIGSERIAL PRIMARY KEY,folder BIGINT NOT NULL REFERENCES wg_folders(id),
            message BIGINT NOT NULL,kind TEXT NOT NULL,mime TEXT NOT NULL DEFAULT '',
            filename TEXT NOT NULL DEFAULT '',date DOUBLE PRECISION NOT NULL DEFAULT 0,
            width INTEGER NOT NULL DEFAULT 0,height INTEGER NOT NULL DEFAULT 0,
            size BIGINT NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'pending',
            thumb_key TEXT,file_key TEXT,search_text TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',grouped TEXT,ocr_text TEXT NOT NULL DEFAULT '',
            metadata_rev BIGINT NOT NULL DEFAULT 0,
            search_caption TEXT NOT NULL DEFAULT '',search_replies TEXT NOT NULL DEFAULT '',
            search_ocr TEXT NOT NULL DEFAULT '',
            UNIQUE(folder,message)
          );
          CREATE TEMP TABLE wg_user_prefs(uid BIGINT PRIMARY KEY,mode TEXT NOT NULL);
          CREATE TEMP TABLE wg_user_folders(uid BIGINT NOT NULL,folder BIGINT NOT NULL,
            PRIMARY KEY(uid,folder));
          CREATE TEMP TABLE wg_folder_covers(folder BIGINT PRIMARY KEY,updated DOUBLE PRECISION);
          CREATE TEMP TABLE wg_favorites(uid BIGINT NOT NULL,media BIGINT NOT NULL,
            created DOUBLE PRECISION NOT NULL, PRIMARY KEY(uid,media));
          CREATE TEMP TABLE wg_view_progress(uid BIGINT NOT NULL,folder BIGINT NOT NULL,
            media BIGINT NOT NULL,updated DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(uid,folder));
          CREATE TEMP TABLE wg_replies(folder BIGINT NOT NULL,parent_message BIGINT NOT NULL,
            reply_message BIGINT NOT NULL,words TEXT NOT NULL,PRIMARY KEY(folder,reply_message));
          CREATE TEMP TABLE wg_media_text_outbox(media BIGINT PRIMARY KEY,revision BIGINT NOT NULL,
            attempts INT NOT NULL DEFAULT 0,next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0);
          CREATE TEMP TABLE wg_personal_folders(
            id BIGSERIAL PRIMARY KEY,uid BIGINT NOT NULL,name TEXT NOT NULL,
            cover_media BIGINT,created DOUBLE PRECISION NOT NULL DEFAULT 0);
          CREATE TEMP TABLE wg_personal_items(
            uid BIGINT NOT NULL,folder BIGINT NOT NULL,media BIGINT NOT NULL,
            added DOUBLE PRECISION NOT NULL DEFAULT 0);
          CREATE TEMP TABLE wg_personal_covers(folder BIGINT PRIMARY KEY,uid BIGINT NOT NULL);
          CREATE TEMP TABLE wg_quota(uid BIGINT NOT NULL,day DATE NOT NULL,
            used INT NOT NULL DEFAULT 0,PRIMARY KEY(uid,day));
          CREATE TEMP TABLE wg_download_tickets(token TEXT PRIMARY KEY,uid BIGINT NOT NULL,
            ids BIGINT[] NOT NULL,day DATE NOT NULL,expires DOUBLE PRECISION NOT NULL,
            consumed BOOLEAN NOT NULL DEFAULT FALSE);
        """)
        await self.conn.execute("""
          INSERT INTO wg_folders(id,peer,name,public_name,preferred_uid,source_uid,complete)
          VALUES
            (1,-100111,'Padre','Galería Unificada',101,101,TRUE),
            (2,-100222,'Fuente hija',NULL,101,101,TRUE),
            (3,-100333,'Otra galería','Otra',NULL,NULL,TRUE);
          INSERT INTO wg_folder_sources(parent_folder,source_folder,created)
          VALUES (1,2,1);
          INSERT INTO wg_user_prefs(uid,mode) VALUES(701111,'selected');
          INSERT INTO wg_user_folders(uid,folder) VALUES(701111,1);
          INSERT INTO wg_media(folder,message,kind,mime,filename,date,size,state,thumb_key,
                               file_key,label,grouped,ocr_text)
          VALUES
            (1,55,'photo','image/jpeg','root.jpg',100,1000,'ready',
             'root-thumb','root-file','captionroot','400','ocrraiz'),
            (2,55,'video','video/mp4','child.mp4',101,2000,'ready',
             'child-thumb','child-file','captionside','400','codigosecretoocr'),
            (2,56,'photo','image/jpeg','child.jpg',102,1500,'ready',
             'second-thumb','second-file','albumsecond','400','');
          INSERT INTO wg_replies(folder,parent_message,reply_message,words)
          VALUES (1,55,99,'respuestaunicaoriginal'),
                 (2,55,99,'comentarioreply');
          INSERT INTO wg_personal_folders(id,uid,name) VALUES(8,701111,'Favoritos personales');
          INSERT INTO wg_personal_items(uid,folder,media,added) VALUES(701111,8,2,1);
        """)
        self.db=TestDB(self.conn)
        self.g=N(db=lambda:self.db,
                 entitled=AsyncMock(return_value=True),
                 free_granted=AsyncMock(return_value=False),
                 storage=N(ready=True),
                 s=N(cipher=Fernet(Fernet.generate_key())),
                 log=AsyncMock())
        async def permitted(uid,folder):
            return await GalleryCore.permitted(self.g,uid,folder)
        self.g.permitted=permitted
        for target in ("web_galerias.core.is_admin","web_galerias.permissions.is_admin"):
            p=patch(target,lambda uid:False)
            p.start();self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_media_ids_are_distinct_even_when_message_ids_match_between_groups(self):
        rows=await self.conn.fetch("SELECT id,folder,message FROM wg_media WHERE message=55 ORDER BY folder")
        self.assertEqual([(r["folder"],r["message"]) for r in rows],[(1,55),(2,55)])
        self.assertNotEqual(rows[0]["id"],rows[1]["id"])

    async def test_user_sees_one_folder_with_both_sources_and_no_child_card(self):
        results=await GalleryCore.folder_list(self.g,701111)
        self.assertEqual(len(results),1)
        self.assertEqual(results[0]["id"],1)
        self.assertEqual(results[0]["photos"],2)
        self.assertEqual(results[0]["videos"],1)

    async def test_selected_only_user_inherits_parent_folder_permissions(self):
        self.assertTrue(await self.g.permitted(701111,1))
        self.assertTrue(await self.g.permitted(701111,2))
        self.assertFalse(await self.g.permitted(701111,3))
        self.assertFalse(await self.g.permitted(701112,2))

    async def test_captions_replies_ocr_album_searches_remain_source_isolated(self):
        await media_text.rebuild(self.g,1,[55])
        await media_text.rebuild(self.g,2,[55,56])
        original=await self.conn.fetchval("SELECT search_text FROM wg_media WHERE folder=1")
        child=await self.conn.fetchval("SELECT search_text FROM wg_media WHERE folder=2 AND message=55")
        self.assertIn("respuestaunicaoriginal",original)
        self.assertNotIn("comentarioreply",original)
        self.assertIn("comentarioreply",child)
        self.assertIn("codigosecretoocr",child)
        self.assertNotIn("respuestaunicaoriginal",child)
        for query,expected in (
            ("captionroot",{1}),
            ("captionside",{2,3}),
            ("comentarioreply",{2,3}),
            ("codigosecretoocr",{2,3}),
        ):
            result=await GalleryCore.list_items(
                self.g,701111,1,query,0,
                lambda uid,mid,kind:"fake-"+str(mid), "all")
            self.assertEqual({x["id"] for x in result["items"]},expected)

    async def test_moderation_and_playback_are_individually_identifiable(self):
        result=await GalleryCore.list_items(self.g,701111,1,"",0,
            lambda uid,mid,kind:"safe","all")
        self.assertEqual({x["id"] for x in result["items"]},{1,2,3})
        # Every item retains the exact source folder and message ID in PostgreSQL.
        row=await self.conn.fetchrow("SELECT folder,message FROM wg_media WHERE id=2")
        self.assertEqual((row["folder"],row["message"]),(2,55))

    async def test_favorites_progress_personal_folders_and_location_preserve_access(self):
        await user_features.set_favorite(
            self.g,701111,{"media":2,"favorite":True})
        favorites=await user_features.items(self.g,701111,{"kind":"all"})
        self.assertEqual([f["id"] for f in favorites["items"]],[2])
        progress=await user_features.save_progress(self.g,701111,{"media":2})
        self.assertEqual(progress["folder"],1)
        collections=await user_features.collections(self.g,701111)
        self.assertEqual(collections["resume"][0]["folder"],1)
        self.assertEqual(collections["favorites_count"],1)
        async with self.db.acquire() as conn:
            allowed=await personal.allowed_personal_media(conn,701111,[2])
        self.assertEqual([x["id"] for x in allowed],[2])
        personal_result=await personal.items(self.g,701111,8,"",0,"all",
            lambda uid,mid,kind:"safe")
        self.assertEqual([x["id"] for x in personal_result["items"]],[2])
        found=await location.locate(self.g,701111,{"media":2})
        self.assertEqual(found["folder"]["id"],1)
        self.assertEqual(found["focus"],2)
        self.assertIn(1,[x["id"] for x in found["items"]])

    async def test_source_media_can_be_downloaded_using_parent_permissions(self):
        with patch("web_galerias.download_plus.tier",
                   new=AsyncMock(return_value={"quota_unlimited":False,
                           "daily_limit":50,"tier":"basic"})):
            result=await downloads.reserve(self.g,701111,[2,3])
        self.assertEqual(result["count"],2)
        self.assertEqual(len(result["files"]),2)
        self.assertEqual(await self.conn.fetchval(
            "SELECT used FROM wg_quota WHERE uid=701111"),2)

    async def test_add_unlimited_groups_for_same_account_without_changing_parent(self):
        from web_galerias import admin
        with patch.object(admin,"linked_source_account",
                          new=AsyncMock(return_value=101)):
            for value in range(5):
                peer=-100500-value
                new=await folder_sources.add(self.g,701111,
                    {"folder":1,"peer":peer,"account_uid":"101",
                     "account_system":"descargas"})
                self.assertTrue(new["ok"])
                self.assertEqual(new["account_uid"],101)
        count=await self.conn.fetchval(
            "SELECT count(*) FROM wg_folder_sources WHERE parent_folder=1")
        self.assertEqual(count,6)
        rows=await self.conn.fetch(
            "SELECT peer,source_uid FROM wg_folders WHERE id IN "
            "(SELECT source_folder FROM wg_folder_sources WHERE parent_folder=1)")
        self.assertTrue(all(row["source_uid"]==101 for row in rows))
        saved=await self.conn.fetchrow("SELECT name,peer,source_uid FROM wg_folders WHERE id=1")
        self.assertEqual((saved["name"],saved["peer"],saved["source_uid"]),
                         ("Padre",-100111,101))
        with patch.object(admin,"linked_source_account",
                          new=AsyncMock(return_value=101)):
            with self.assertRaises(web.HTTPConflict):
                await folder_sources.add(self.g,701111,
                    {"folder":3,"peer":-100500,"account_uid":"101"})
        self.assertEqual(await self.conn.fetchval(
            "SELECT parent_folder FROM wg_folder_sources WHERE source_folder="
            "(SELECT id FROM wg_folders WHERE peer=-100500)"),1)


if __name__=="__main__":
    unittest.main()
