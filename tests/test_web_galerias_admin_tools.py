"""CUBANITAS admin moderation and original-gallery positioning safety tests."""
import asyncio
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch

from aiohttp import web
from web_galerias import moderation,location


def fake_db(rows):
    c=SimpleNamespace(fetch=AsyncMock(return_value=rows),execute=AsyncMock())
    @asynccontextmanager
    async def transaction():
        yield
    @asynccontextmanager
    async def acquire():
        yield c
    c.transaction=transaction
    return SimpleNamespace(acquire=acquire),c


class ModeratorTests(unittest.IsolatedAsyncioTestCase):
    def test_tombstone_and_durable_r2_queue_separate_from_telegram(self):
        self.assertIn("wg_media_removals",moderation.SCHEMA)
        self.assertIn("wg_r2_deletion_queue",moderation.SCHEMA)
        self.assertNotIn("descarga_",moderation.SCHEMA)
        self.assertNotIn("DELETE FROM wg_media",Path("web_galerias/moderation.py").read_text())

    async def test_customer_cannot_moderate_or_read_other_user_profiles(self):
        g=SimpleNamespace(db=lambda:None)
        for op in ("admin-media-list","admin-media-remove","admin-active-users",
                   "admin-user-profile","admin-media-r2-status"):
            with patch.object(moderation,"is_admin",return_value=False):
                with self.assertRaises(web.HTTPForbidden):
                    await moderation.operation(g,999999,op,{})

    async def test_hide_two_files_preserves_cloudflare(self):
        rows=[
          {"id":111,"folder":9,"message":123,"thumb_key":"folders/9/123/thumb.jpg",
           "file_key":"folders/9/123/original.jpg"},
          {"id":222,"folder":9,"message":124,"thumb_key":"folders/9/124/thumb.jpg",
           "file_key":"folders/9/124/original.jpg"},
        ]
        db,c=fake_db(rows)
        g=SimpleNamespace(db=lambda:db,log=AsyncMock(),
                          storage=SimpleNamespace(safe_key=lambda k:k))
        with patch.object(moderation,"is_admin",return_value=True):
            answer=await moderation.operation(g,7539025033,"admin-media-remove",{
                "ids":[111,222],"remove_cloudflare":False,
                "confirm":"CONFIRMAR_ELIMINACION"})
        self.assertEqual(answer,{"ok":True,"removed":2,"cloudflare_queued":0})
        updates=[call.args[0] for call in c.execute.await_args_list]
        self.assertTrue(any("UPDATE wg_media SET state='removed'" in sql for sql in updates))
        self.assertEqual(sum("wg_media_removals" in sql for sql in updates),2)
        self.assertFalse(any("wg_r2_deletion_queue" in sql for sql in updates))
        g.log.assert_awaited_once()

    async def test_explicit_cloudflare_checkbox_creates_durable_deletion_queue(self):
        row={"id":111,"folder":9,"message":123,
             "thumb_key":"folders/9/123/thumb.jpg",
             "file_key":"folders/9/123/original.jpg"}
        db,c=fake_db([row])
        g=SimpleNamespace(db=lambda:db,log=AsyncMock(),
                          storage=SimpleNamespace(safe_key=lambda k:k))
        with patch.object(moderation,"is_admin",return_value=True):
            answer=await moderation.operation(g,7539025033,"admin-media-remove",{
                "ids":[111],"remove_cloudflare":True,
                "confirm":"CONFIRMAR_ELIMINACION"})
        self.assertEqual(answer["cloudflare_queued"],2)
        self.assertEqual(sum("wg_r2_deletion_queue" in call.args[0]
                for call in c.execute.await_args_list),2)

    async def test_delete_requires_explicit_checkbox_and_confirmation(self):
        g=SimpleNamespace(db=lambda:None)
        with patch.object(moderation,"is_admin",return_value=True):
            for bad in (
                {"ids":[1],"remove_cloudflare":"yes","confirm":"CONFIRMAR_ELIMINACION"},
                {"ids":[1],"remove_cloudflare":False},
                {"ids":list(range(1,202)),"remove_cloudflare":False,
                 "confirm":"CONFIRMAR_ELIMINACION"},
            ):
                with self.assertRaises(web.HTTPBadRequest):
                    await moderation.operation(g,7539025033,"admin-media-remove",bad)

    async def test_original_gallery_context_is_private_and_ordered(self):
        record={"id":50,"folder":9,"name":"Galería Nueve","active":True}
        newer=[{"id":51,"kind":"photo","mime":"image/jpeg","filename":"51.jpg",
                 "date":0,"width":0,"height":0,"size":4,"state":"ready",
                 "thumb_key":None,"file_key":"a"}]
        older=[{"id":50,"kind":"photo","mime":"image/jpeg","filename":"50.jpg",
                 "date":0,"width":0,"height":0,"size":4,"state":"ready",
                 "thumb_key":None,"file_key":"b"}]
        db=SimpleNamespace(fetchrow=AsyncMock(return_value=record),
                           fetch=AsyncMock(side_effect=[newer,older]))
        g=SimpleNamespace(db=lambda:db,permitted=AsyncMock(return_value=True),
                          s=SimpleNamespace(cipher=SimpleNamespace(encrypt=lambda x:x)))
        answer=await location.locate(g,8129084202,{"media":50})
        self.assertEqual([i["id"] for i in answer["items"]],[51,50])
        self.assertEqual(answer["focus"],50)
        self.assertEqual(answer["folder"]["id"],9)
        self.assertEqual(answer["folder"]["name"],"Galería Nueve")
        g.permitted.assert_awaited_once_with(8129084202,9)

    async def test_original_location_denies_unauthorized_folder(self):
        db=SimpleNamespace(fetchrow=AsyncMock(return_value={
            "id":50,"folder":9,"name":"Private","active":True}))
        g=SimpleNamespace(db=lambda:db,permitted=AsyncMock(return_value=False))
        with self.assertRaises(web.HTTPForbidden):
            await location.locate(g,8129084202,{"media":50})

    def test_long_press_keeps_drag_for_normal_gallery_and_hides_origin_for_multi(self):
        script=Path("web_galerias/app.js").read_text()
        extras=Path("web_galerias/admin_tools.js").read_text()
        self.assertIn("S.folder?.personal||S.search.trim()",script)
        self.assertIn("showDragPreview(m,ids,startX,startY)",script)
        self.assertIn("if(ids.length===1)",extras)
        self.assertIn('choice("↓ Descargar al teléfono"',extras)
        self.assertIn('choice("📁 Mover a una carpeta"',extras)
        self.assertIn("admin-user-profile",extras)
        self.assertIn("admin-media-remove",extras)


if __name__=="__main__":
    unittest.main()
