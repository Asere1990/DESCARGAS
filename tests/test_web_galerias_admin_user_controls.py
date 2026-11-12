"""Isolated tests for Web Galerías admin user controls and online heartbeat."""
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from aiohttp import web

from web_galerias import moderation,core
from web_galerias.app import Gallery


class GalleryAdminControls(unittest.IsolatedAsyncioTestCase):
    async def test_user_routes_require_existing_admin_policy(self):
        g=N(db=lambda:None)
        names=["admin-users-page","admin-user-access-status",
               "admin-user-access-change","admin-user-personal-folders",
               "admin-user-personal-items","admin-user-personal-remove",
               "admin-user-personal-delete"]
        with patch.object(moderation,"is_admin",return_value=False):
            for name in names:
                with self.subTest(name=name),self.assertRaises(web.HTTPForbidden):
                    await moderation.operation(g,444,name,{})

    async def test_paginated_paid_online_roster_does_not_fetch_full_profiles(self):
        db=N(fetch=AsyncMock(return_value=[
            {"uid":50,"name":"Luisa","last_seen":1800.0,
             "paid":True,"online":True,"blocked":False,"is_free":False},
            {"uid":49,"name":"Rosa","last_seen":1700.0,
             "paid":False,"online":False,"blocked":True,"is_free":False}]))
        g=N(db=lambda:db)
        with patch.object(moderation,"is_admin",return_value=True):
            result=await moderation.operation(g,1,"admin-users-page",
                                               {"query":"lui"})
        self.assertEqual(len(result["users"]),2)
        self.assertEqual(result["users"][0]["name"],"Luisa")
        self.assertTrue(result["users"][0]["paid"])
        self.assertTrue(result["users"][0]["online"])
        self.assertTrue(result["users"][1]["blocked"])
        self.assertIsNone(result["next"])
        sql=db.fetch.await_args.args[0]
        self.assertIn("LIMIT 41",sql)
        self.assertIn("wg_gallery_presence",sql)
        self.assertNotIn("SELECT *",sql)
        self.assertEqual(db.fetch.await_args.args[3],"lui")

    async def test_user_access_withdrawal_is_reversible_and_never_touches_stripe(self):
        class Connection:
            execute=AsyncMock()
            @asynccontextmanager
            async def transaction(self):
                yield
        conn=Connection()
        @asynccontextmanager
        async def acquire():
            yield conn
        db=N(fetchval=AsyncMock(return_value=True),acquire=acquire)
        g=N(db=lambda:db,log=AsyncMock())
        with patch.object(moderation,"is_admin",side_effect=lambda u:int(u)==1):
            res=await moderation.operation(g,1,"admin-user-access-change",
                {"telegram_id":55,"blocked":True,"confirm":"CAMBIAR_ACCESO_GALERIAS"})
            self.assertTrue(res["blocked"])
            res=await moderation.operation(g,1,"admin-user-access-change",
                {"telegram_id":55,"blocked":False,"confirm":"CAMBIAR_ACCESO_GALERIAS"})
            self.assertFalse(res["blocked"])
        sql="\n".join(call.args[0] for call in conn.execute.await_args_list)
        self.assertIn("wg_access_blocks",sql)
        self.assertIn("wg_gallery_presence",sql)
        self.assertNotIn("wg_subscriptions",sql)
        self.assertNotIn("descarga_accounts",sql)
        self.assertNotIn("DELETE FROM wg_users",sql)
        self.assertEqual(g.log.await_count,2)

    async def test_personal_folder_item_removal_does_not_delete_gallery_originals(self):
        class Connection:
            fetchval=AsyncMock(side_effect=[1,1])
            execute=AsyncMock()
            @asynccontextmanager
            async def transaction(self):
                yield
        conn=Connection()
        @asynccontextmanager
        async def acquire():
            yield conn
        g=N(db=lambda:N(acquire=acquire),log=AsyncMock())
        with patch.object(moderation,"is_admin",return_value=True):
            result=await moderation.operation(g,1,"admin-user-personal-remove",
                {"telegram_id":55,"personal_folder":6,"ids":[300],
                 "confirm":"QUITAR_DE_CARPETA"})
        self.assertEqual(result["removed"],1)
        updates="\n".join(call.args[0] for call in conn.execute.await_args_list)
        self.assertIn("DELETE FROM wg_personal_items",updates)
        self.assertNotIn("DELETE FROM wg_media",updates)
        self.assertNotIn("wg_r2_deletion_queue",updates)
        self.assertNotIn("descarga_accounts",updates)

    async def test_withdrawal_cannot_block_admin_or_change_without_confirmation(self):
        g=N(db=lambda:None)
        with patch.object(moderation,"is_admin",side_effect=lambda u:int(u)==1):
            with self.assertRaises(web.HTTPForbidden):
                await moderation.operation(g,1,"admin-user-access-change",
                    {"telegram_id":1,"blocked":True,"confirm":"CAMBIAR_ACCESO_GALERIAS"})
            with self.assertRaises(web.HTTPBadRequest):
                await moderation.operation(g,1,"admin-user-access-change",
                    {"telegram_id":55,"blocked":True})

    async def test_heartbeat_only_from_authorized_gallery_view(self):
        db=N(execute=AsyncMock())
        app=N(auth=AsyncMock(return_value=55),db=lambda:db,
              entitled=AsyncMock(return_value=True))
        req=N(content_length=100,json=AsyncMock(return_value={"op":"presence","view":"album"}))
        self.assertEqual(await Gallery.api(app,req),{"ok":True})
        self.assertIn("wg_gallery_presence",db.execute.await_args.args[0])
        app.entitled=AsyncMock(return_value=False)
        with self.assertRaises(web.HTTPForbidden):
            await Gallery.api(app,req)

    async def test_removed_tester_cannot_bypass_block_with_free_access(self):
        db=N(fetchval=AsyncMock(return_value=True))
        fake=N(db=lambda:db,free_granted=AsyncMock(return_value=True))
        with patch.object(core,"is_admin",return_value=False):
            self.assertFalse(await core.GalleryCore.entitled(fake,55))
        db.fetchval.assert_awaited()

    def test_ui_keeps_deletion_admin_only_and_customer_download_limit(self):
        root=Path(__file__).resolve().parents[1]
        markup=(root/"web_galerias/index.html").read_text()
        js=(root/"web_galerias/app.js").read_text()
        extras=(root/"web_galerias/admin_tools.js").read_text()
        css=(root/"web_galerias/style.css").read_text()
        for control in ("selection-admin-delete","viewer-admin-delete",
                        "admin-users-query","admin-user-profile",
                        "admin-media-query","admin-media-kind"):
            self.assertIn('id="'+control+'"',markup)
        self.assertIn("S.me?.admin?100:10",js)
        self.assertIn("if(!S.me?.admin||state.mediaBusy)return false;",extras)
        self.assertIn('box.id="admin-user-personal-items"',extras)
        self.assertIn("gallery-viewer-open #album #folder-drop-tray",css)
        self.assertIn("admin-users-page",extras)
        self.assertNotIn("admin-user-selector",markup)


if __name__=="__main__":
    unittest.main()
