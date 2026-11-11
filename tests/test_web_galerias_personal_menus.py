"""Pruebas de carpetas personales compactas, portadas privadas y arrastre múltiple."""
import base64
import io
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from aiohttp import web
from cryptography.fernet import Fernet
from PIL import Image
from web_galerias import personal
from web_galerias.downloads import temporary_link


ROOT=Path(__file__).resolve().parents[1]/"web_galerias"


def jpeg_image():
    image=Image.new("RGB",(200,150),(20,120,165))
    out=io.BytesIO()
    image.save(out,format="JPEG",quality=80)
    return out.getvalue()


class PersonalFolderCoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_photo_is_used_as_cover_when_user_has_not_selected_one(self):
        cipher=Fernet(Fernet.generate_key())
        db=SimpleNamespace(fetch=AsyncMock(return_value=[{
            "id":33,"name":"Personal 1","count":10,
            "cover_media":None,"cover_item":91,"has_upload":False
        }]))
        g=SimpleNamespace(db=lambda:db,s=SimpleNamespace(cipher=cipher))
        with patch("web_galerias.permissions.is_admin",return_value=False):
            result=await personal.folders(g,45678)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]["count"],10)
        self.assertFalse(result[0]["has_custom_cover"])
        self.assertIn("/web-galerias/media/91/thumb?t=",result[0]["cover"])
        self.assertNotIn("cover_item",result[0])
        sql=db.fetch.await_args.args[0]
        self.assertIn("WHEN m2.kind='photo' THEN 1",sql)
        self.assertIn("ORDER BY CASE WHEN p.cover_media=m2.id THEN 0",sql)
        self.assertIn("f2.active",sql)
        self.assertIn("i2.uid=$1",sql)

    async def test_custom_image_has_priority_over_first_photo(self):
        g=SimpleNamespace(db=lambda:SimpleNamespace(fetch=AsyncMock(return_value=[{
            "id":33,"name":"Personal 1","count":3,
            "cover_media":None,"cover_item":91,"has_upload":True
        }])),s=SimpleNamespace(cipher=Fernet(Fernet.generate_key())))
        result=(await personal.folders(g,98765))[0]
        self.assertTrue(result["has_custom_cover"])
        self.assertIn("/web-galerias/personal-cover/33?t=",result["cover"])
        self.assertNotIn("91/thumb",result["cover"])

    async def test_create_folder_and_ten_selected_items_in_one_transaction(self):
        ids=list(range(1,11))
        @asynccontextmanager
        async def transaction():yield
        conn=SimpleNamespace(
            fetchval=AsyncMock(side_effect=[0,33]),
            fetch=AsyncMock(return_value=[{"id":i} for i in ids]),
            executemany=AsyncMock(),
            transaction=transaction)
        @asynccontextmanager
        async def acquire():yield conn
        g=SimpleNamespace(db=lambda:SimpleNamespace(acquire=acquire))
        with patch("web_galerias.permissions.is_admin",return_value=False):
            result=await personal.operation(g,45678,"personal-create",{"name":"Nueva","ids":ids})
        self.assertEqual(result,{"ok":True,"id":33,"name":"Nueva","count":10})
        rows=conn.executemany.await_args.args[1]
        self.assertEqual([item[2] for item in rows],ids)
        self.assertEqual({item[1] for item in rows},{33})

    async def test_cannot_create_with_eleven_files_or_unauthorized_source(self):
        g=SimpleNamespace(db=AsyncMock())
        with self.assertRaises(web.HTTPBadRequest):
            await personal.operation(g,45678,"personal-create",
                {"name":"No","ids":list(range(11))})
        @asynccontextmanager
        async def transaction():yield
        conn=SimpleNamespace(fetchval=AsyncMock(return_value=0),
                             fetch=AsyncMock(return_value=[]),
                             transaction=transaction)
        @asynccontextmanager
        async def acquire():yield conn
        g=SimpleNamespace(db=lambda:SimpleNamespace(acquire=acquire))
        with patch("web_galerias.permissions.is_admin",return_value=False):
            with self.assertRaises(web.HTTPForbidden):
                await personal.operation(g,45678,"personal-create",
                    {"name":"No","ids":[1]})

    async def test_upload_custom_cover_is_private_and_does_not_modify_gallery_media(self):
        sample=base64.b64encode(jpeg_image()).decode()
        @asynccontextmanager
        async def transaction():yield
        conn=SimpleNamespace(fetchval=AsyncMock(return_value=1),
                             execute=AsyncMock(),transaction=transaction)
        @asynccontextmanager
        async def acquire():yield conn
        db=SimpleNamespace(fetchval=AsyncMock(return_value=1),acquire=acquire)
        g=SimpleNamespace(db=lambda:db)
        result=await personal.operation(g,98765,"personal-cover-upload",
             {"personal_folder":33,"image":sample})
        self.assertEqual(result,{"ok":True})
        statements=[call.args[0] for call in conn.execute.await_args_list]
        self.assertTrue(any("INSERT INTO wg_personal_covers" in q for q in statements))
        self.assertTrue(any("SET cover_media=NULL" in q for q in statements))
        self.assertTrue(all("wg_media" not in q and "wg_folder_covers" not in q
                            for q in statements))
        stored=conn.execute.await_args_list[0].args[3]
        self.assertTrue(stored.startswith(b"\xff\xd8\xff"))
        db.fetchval.return_value=None
        with self.assertRaises(web.HTTPForbidden):
            await personal.operation(g,55555,"personal-cover-upload",
               {"personal_folder":33,"image":sample})

    async def test_bad_cover_image_rejected(self):
        db=SimpleNamespace(fetchval=AsyncMock(return_value=1))
        g=SimpleNamespace(db=lambda:db)
        for raw in ("notBase64!!","A"*350001,base64.b64encode(b"not image").decode()):
            with self.subTest(raw=raw[:10]):
                with self.assertRaises(web.HTTPBadRequest):
                    await personal.operation(g,12345,"personal-cover-upload",
                        {"personal_folder":33,"image":raw})

    async def test_select_existing_photo_requires_access_to_folder(self):
        @asynccontextmanager
        async def transaction():yield
        conn=SimpleNamespace(fetchval=AsyncMock(return_value=None),
                             execute=AsyncMock(),transaction=transaction)
        @asynccontextmanager
        async def acquire():yield conn
        g=SimpleNamespace(db=lambda:SimpleNamespace(acquire=acquire))
        with patch("web_galerias.permissions.is_admin",return_value=False):
            with self.assertRaises(web.HTTPForbidden):
                await personal.operation(g,12345,"personal-set-cover",
                    {"personal_folder":33,"media":99})
        conn.execute.assert_not_awaited()

    async def test_private_cover_endpoint_checks_owner_and_token(self):
        cipher=Fernet(Fernet.generate_key())
        data=jpeg_image()
        pool=SimpleNamespace(fetchrow=AsyncMock(return_value={
           "mime":"image/jpeg","body":data
        }))
        g=SimpleNamespace(s=SimpleNamespace(cipher=cipher),
            db=lambda:pool,entitled=AsyncMock(return_value=True))
        token=temporary_link(g,12345,33,"personal-cover")
        req=SimpleNamespace(match_info={"folder":"33"},query={"t":token})
        result=await personal.serve_cover(g,req)
        self.assertEqual(result.content_type,"image/jpeg")
        self.assertEqual(result.body,data)
        self.assertIn("p.uid=$2",pool.fetchrow.await_args.args[0])
        req.match_info={"folder":"34"}
        with self.assertRaises(web.HTTPForbidden):
            await personal.serve_cover(g,req)
        g.entitled.return_value=False
        req.match_info={"folder":"33"}
        with self.assertRaises(web.HTTPForbidden):
            await personal.serve_cover(g,req)


class PersonalMenusVisualTests(unittest.TestCase):
    def test_menus_are_compact_with_new_folder_first_and_gallery_below(self):
        html=(ROOT/"index.html").read_text()
        js=(ROOT/"app.js").read_text()
        css=(ROOT/"style.css").read_text()
        library=html.split('id="personal-section"',1)[1].split('class="quota"',1)[0]
        album=html.split('id="folder-drop-tray"',1)[1].split('id="album-grid"',1)[0]
        self.assertLess(library.index('id="create-personal-folder"'),
                        library.index('id="personal-folder-entries"'))
        self.assertLess(album.index('id="album-create-folder"'),
                        album.index('id="folder-drop-targets"'))
        self.assertIn('id="folder-drop-toggle"',html)
        self.assertIn('id="personal-folders-toggle"',html)
        self.assertIn('class="folder-drop-menu hidden"',html)
        self.assertIn('class="personal-folders-menu hidden"',html)
        self.assertIn("max-height:min(49dvh,420px)",css)
        self.assertIn('className="personal-folder-entry"',js)
        self.assertIn('function setDropMenuOpen(open)',js)

    def test_drag_preview_shows_count_and_scrolls_blue_menu(self):
        html=(ROOT/"index.html").read_text()
        js=(ROOT/"app.js").read_text()
        css=(ROOT/"style.css").read_text()
        for value in ('id="drag-preview"','id="drag-preview-image"',
                      'id="drag-preview-count"'):
            self.assertIn(value,html)
        for code in ('showDragPreview(m,ids,startX,startY)',
                     'textContent=String(ids.length)',
                     'dragHoverAt(e.clientX,e.clientY)',
                     'requestAnimationFrame(dragMenuScroll)',
                     'menu.scrollTop+=delta',
                     'next.classList.add("drag-target")',
                     'dataset.folderDrop==="new")createPersonalFolder(ids)',
                     'api("personal-create",{name,ids:selected})'):
            self.assertIn(code,js)
        self.assertIn("background:#197cdf",css)
        self.assertIn("opacity:.68",css)
        self.assertIn("pointer-events:none",css)

    def test_personal_cover_can_be_changed_from_phone_or_existing_photo(self):
        js=(ROOT/"app.js").read_text()
        html=(ROOT/"index.html").read_text()
        for value in ('id="personal-cover-file"','id="viewer-set-cover"',
                      'api("personal-cover-upload"','api("personal-set-cover"',
                      'api("personal-cover-reset"'):
            self.assertIn(value,html+js)

if __name__=="__main__":unittest.main()
