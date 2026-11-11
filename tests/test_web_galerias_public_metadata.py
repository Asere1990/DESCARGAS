"""Los nombres internos y el estado del respaldo no deben salir a clientes."""
import unittest
from aiohttp import web
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from web_galerias.core import GalleryCore
from web_galerias.admin import admin_op


class PublicMetadataTests(unittest.IsolatedAsyncioTestCase):
    def make_core(self):
        db=SimpleNamespace(execute=AsyncMock(),fetchval=AsyncMock(return_value=None),
                           fetch=AsyncMock(return_value=[]))
        core=GalleryCore(SimpleNamespace(repo=SimpleNamespace(pool=db)))
        core.ready=True
        core.db=lambda:db
        core.entitled=AsyncMock(return_value=True)
        core.design=AsyncMock(return_value={})
        core.folder_list=AsyncMock(return_value=[{
            "id":7,"name":"Fotos públicas de Cuba","photos":4,"videos":0,
            "pending":550,"complete":False,"error":"RateLimit"}])
        return core,db

    async def test_test_user_sees_only_public_name_and_media_counts(self):
        core,db=self.make_core()
        result=await core.bootstrap(5662710947)
        folder=result["folders"][0]
        self.assertEqual(folder["name"],"Fotos públicas de Cuba")
        self.assertEqual(folder["photos"],4)
        self.assertEqual(folder["videos"],0)
        for hidden in ("pending","complete","error","peer","source_uid"):
            self.assertNotIn(hidden,folder)

    async def test_free_tester_matches_paying_customer_exactly(self):
        # La autorización puede provenir de acceso.txt o del pago, pero una vez
        # autorizado el cliente recibe idénticos datos, cuotas y carpetas.
        core,db=self.make_core()
        core.folder_list=AsyncMock(side_effect=lambda uid:[{
            "id":7,"name":"Fotos públicas de Cuba","photos":4,"videos":0,
            "pending":550,"complete":False,"error":"RateLimit"}])
        pilot_user=await core.bootstrap(5662710947)
        paying_user=await core.bootstrap(1600000000)
        self.assertEqual(pilot_user,paying_user)
        self.assertNotIn("tester",pilot_user)
        self.assertTrue(pilot_user["allowed"])
        self.assertFalse(pilot_user["admin"])
        self.assertEqual(pilot_user["remaining"],100)
        self.assertEqual(len(pilot_user["folders"]),1)

    def test_public_ui_has_no_tester_label(self):
        from pathlib import Path
        script=(Path(__file__).resolve().parents[1]/"web_galerias"/"app.js").read_text()
        # El aviso de la portada permanece oculto también para el cliente
        # con acceso verificado. Los detalles de cobro solo salen en Checkout.
        self.assertNotIn("Compra segura mediante Stripe",script)
        self.assertNotIn("cuando Stripe esté configurado",script)
        self.assertIn('$("intro-notice").textContent="";',script)
        html=(Path(__file__).resolve().parents[1]/"web_galerias"/"index.html").read_text()
        self.assertIn('id="intro-notice" class="quiet hidden"',html)
        self.assertNotIn('S.me.tester',script)
        self.assertNotIn('"Usuario de prueba"',script)

    async def test_admin_sees_backup_metadata(self):
        core,db=self.make_core()
        result=await core.bootstrap(7539025033)
        folder=result["folders"][0]
        self.assertEqual(folder["pending"],550)
        self.assertEqual(folder["error"],"RateLimit")
        self.assertFalse(folder["complete"])

    async def test_folder_listing_uses_only_public_name(self):
        core,db=self.make_core()
        db.fetch.return_value=[]
        await GalleryCore.folder_list(core,5662710947)
        sql=db.fetch.await_args.args[0]
        self.assertIn("NULLIF(f.public_name,'')",sql)
        self.assertNotIn("f.name",sql)
        self.assertIn("Galería ",sql)

    async def test_add_folder_keeps_distinct_internal_and_client_names(self):
        core,db=self.make_core()
        db.fetchrow=AsyncMock(return_value={"id":7})
        core.log=AsyncMock()
        with patch("web_galerias.admin.is_admin",return_value=True):
            result=await admin_op(core,7539025033,"admin-folder",{
                "peer":-1001234567890,
                "name":"Viva Cuba",
                "public_name":"Fotos de Cuba"})
        self.assertEqual(result["id"],7)
        sql=db.fetchrow.await_args.args
        self.assertIn("name,public_name,created",sql[0])
        self.assertEqual(sql[2:4],("Viva Cuba","Fotos de Cuba"))

    async def test_creating_gallery_with_linked_account_stores_preferred_uid(self):
        core,db=self.make_core()
        db.fetchval=AsyncMock(return_value=True)
        db.fetchrow=AsyncMock(return_value={"id":8})
        core.log=AsyncMock()
        with patch("web_galerias.admin.is_admin",return_value=True):
            result=await admin_op(core,7539025033,"admin-folder",{
                "peer":-1001234567890,"name":"Viva Cuba",
                "public_name":"Fotos de Cuba","account_uid":"1234567890"})
        self.assertEqual(result["id"],8)
        self.assertIn("descarga_accounts",db.fetchval.await_args.args[0])
        sql=db.fetchrow.await_args.args
        self.assertIn("preferred_uid,source_uid",sql[0])
        self.assertEqual(sql[-1],1234567890)
        self.assertIn("complete=CASE WHEN",sql[0])
        self.assertIn("scan_cursor=CASE WHEN",sql[0])
        self.assertNotIn("DELETE FROM wg_media",sql[0])

    async def test_cannot_assign_account_without_linked_session(self):
        core,db=self.make_core()
        db.fetchval=AsyncMock(return_value=False)
        db.fetchrow=AsyncMock()
        core.log=AsyncMock()
        with patch("web_galerias.admin.is_admin",return_value=True):
            with self.assertRaises(web.HTTPBadRequest):
                await admin_op(core,7539025033,"admin-folder",{
                    "peer":-1001234567890,"name":"Cuba",
                    "public_name":"Cuba","account_uid":"111111111"})
            self.assertFalse(db.fetchrow.await_count)
            self.assertEqual(db.execute.await_count,0)

    async def test_switching_source_rescans_without_erasing_original_media(self):
        core,db=self.make_core()
        db.fetchval=AsyncMock(return_value=1)
        db.fetchrow=AsyncMock(return_value={"id":7})
        core.log=AsyncMock()
        with patch("web_galerias.admin.is_admin",return_value=True):
            result=await admin_op(core,7539025033,"admin-folder-source",{
                "folder":7,"account_uid":"1234567890"})
        self.assertEqual(result,{"ok":True,"changed":True,"account_uid":1234567890})
        query=db.fetchrow.await_args.args[0]
        for expected in ("preferred_uid=$2","source_uid=$2",
                         "scan_cursor=0","high_water=0","complete=FALSE",
                         "telegram_photos_total=NULL","telegram_videos_total=NULL"):
            self.assertIn(expected,query)
        self.assertNotIn("wg_media",query)
        self.assertNotIn("DELETE",query)
        statements=[call.args[0] for call in db.execute.await_args_list]
        self.assertTrue(any("UPDATE wg_media SET state='pending'" in sql
                            and "WHERE folder=$1 AND state='missing'" in sql
                            for sql in statements))
        self.assertTrue(all("DELETE FROM wg_media" not in sql for sql in statements))
        self.assertTrue(all("state='ready'" not in sql for sql in statements))
        core.log.assert_awaited_once_with(7539025033,"admin-folder-source")

    async def test_public_name_changes_do_not_rename_internal_folder(self):
        core,db=self.make_core()
        core.log=AsyncMock()
        with patch("web_galerias.admin.is_admin",return_value=True):
            await admin_op(core,7539025033,"admin-folder-update",{
                "folder":7,"field":"public_name","name":"Nueva carpeta pública"})
        sql=db.execute.await_args.args[0]
        self.assertIn("SET public_name=$2",sql)
        self.assertNotIn("SET name=$2",sql)

    async def test_admin_cannot_create_nonempty_folder_without_internal_name(self):
        core,db=self.make_core()
        core.log=AsyncMock()
        with patch("web_galerias.admin.is_admin",return_value=True):
            from aiohttp import web
            with self.assertRaises(web.HTTPBadRequest):
                await admin_op(core,7539025033,"admin-folder",{
                    "peer":-1001234567890,
                    "name":"",
                    "public_name":"Fotos de Cuba"})
        self.assertFalse(hasattr(db,"fetchrow") and getattr(db.fetchrow,"await_count",0))


if __name__=="__main__":unittest.main()
