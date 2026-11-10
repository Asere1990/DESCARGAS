"""Los nombres internos y el estado del respaldo no deben salir a clientes."""
import unittest
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
