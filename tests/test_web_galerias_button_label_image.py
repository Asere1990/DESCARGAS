"""Regression tests for optional title and CTA image labels in Web Galerías."""
import base64
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch

from aiohttp import web
from web_galerias import admin
from web_galerias.core import DEFAULTS,ASSET_MIME,MAX_ASSET


class TitleAndButtonImageTests(unittest.IsolatedAsyncioTestCase):
    def test_button_image_is_optional_and_does_not_change_visual_defaults(self):
        self.assertEqual(DEFAULTS["button_mode"],"text")
        self.assertEqual(DEFAULTS["header_mode"],"text")
        self.assertEqual(admin.cleaned_design({"button_mode":"image"})["button_mode"],"image")
        self.assertEqual(admin.cleaned_design({"button_mode":"text"})["button_mode"],"text")
        self.assertEqual(admin.cleaned_design({"header_mode":"image"})["header_mode"],"image")
        for invalid in ("link","gif","file",None):
            with self.subTest(invalid=invalid),self.assertRaises(web.HTTPBadRequest):
                admin.cleaned_design({"button_mode":invalid})
        self.assertEqual(ASSET_MIME["button-label-image"],
                         {"image/jpeg","image/png","image/webp"})
        self.assertEqual(MAX_ASSET["button-label-image"],3_000_000)
        self.assertEqual(DEFAULTS["button_color"],"#198ee8")
        self.assertEqual(DEFAULTS["title_image_height"],120)
        self.assertEqual(DEFAULTS["button_image_height"],30)
        for key,lo,hi in (("title_image_height",40,220),
                          ("button_image_height",12,48)):
            for accepted in (lo,hi):
                with self.subTest(key=key,accepted=accepted):
                    self.assertEqual(admin.cleaned_design({key:accepted})[key],accepted)
            for rejected in (lo-1,hi+1,None,"100",True,12.5):
                with self.subTest(key=key,rejected=rejected):
                    with self.assertRaises(web.HTTPBadRequest):
                        admin.cleaned_design({key:rejected})

    async def test_legacy_editor_keeps_existing_image_sizes(self):
        original={**DEFAULTS,"title_image_height":190,"button_image_height":46}
        db=N(execute=AsyncMock(),fetchval=AsyncMock())
        g=N(db=lambda:db,design=AsyncMock(return_value=original),log=AsyncMock())
        with patch.object(admin,"is_admin",return_value=True):
            result=await admin.admin_op(g,333,"admin-design",{
                "design":{"title":"Otro título","button_mode":"text","header_mode":"text"}
            })
        self.assertEqual(result["design"]["title_image_height"],190)
        self.assertEqual(result["design"]["button_image_height"],46)
        self.assertEqual(result["design"]["title"],"Otro título")
        db.fetchval.assert_not_awaited()
        self.assertIn("wg_config",db.execute.await_args.args[0])

    async def test_button_image_requires_saved_asset_and_does_not_modify_payments(self):
        db=N(fetchval=AsyncMock(return_value=None),execute=AsyncMock())
        g=N(db=lambda:db,design=AsyncMock(return_value=dict(DEFAULTS)),log=AsyncMock())
        data={**DEFAULTS,"button_mode":"image"}
        with patch.object(admin,"is_admin",return_value=True):
            with self.assertRaises(web.HTTPBadRequest):
                await admin.admin_op(g,333,"admin-design",{"design":data})
            db.execute.assert_not_awaited()
            db.fetchval.return_value=1
            result=await admin.admin_op(g,333,"admin-design",{"design":data})
        self.assertTrue(result["ok"])
        self.assertEqual(result["design"]["button_mode"],"image")
        self.assertIn("button-label-image",db.fetchval.await_args.args)
        self.assertIn("wg_config",db.execute.await_args.args[0])
        self.assertNotIn("wg_subscriptions",db.execute.await_args.args[0])
        self.assertNotIn("descarga_accounts",db.execute.await_args.args[0])

    async def test_title_image_uses_existing_header_asset_and_rejects_missing(self):
        db=N(fetchval=AsyncMock(return_value=0),execute=AsyncMock())
        g=N(db=lambda:db,design=AsyncMock(return_value=dict(DEFAULTS)),log=AsyncMock())
        with patch.object(admin,"is_admin",return_value=True):
            with self.assertRaises(web.HTTPBadRequest):
                await admin.admin_op(g,333,"admin-design",
                    {"design":{**DEFAULTS,"header_mode":"image"}})
        db.execute.assert_not_awaited()
        self.assertEqual(db.fetchval.await_args.args[1],"header")

    async def test_admin_can_upload_button_label_image_using_existing_asset_path(self):
        blob=b"\x89PNG\r\n\x1a\n"+b"test-safe-image-payload"
        class Conn:
            execute=AsyncMock()
            @asynccontextmanager
            async def transaction(self):
                yield
        c=Conn()
        c.fetch=AsyncMock(return_value=[{
            "part":0,"total":1,"key":"button-label-image","mime":"image/png","body":blob}])
        @asynccontextmanager
        async def acquire():
            yield c
        g=N(db=lambda:N(acquire=acquire),log=AsyncMock())
        data={"key":"button-label-image","mime":"image/png","upload":"a"*32,
              "part":0,"total":1,"data":base64.b64encode(blob).decode()}
        result=await admin.upload(g,333,data)
        self.assertTrue(result["complete"])
        saves=[call.args for call in c.execute.await_args_list
               if "INSERT INTO wg_assets" in call.args[0]]
        self.assertEqual(len(saves),1)
        self.assertEqual(saves[0][1],"button-label-image")
        self.assertEqual(saves[0][2],"image/png")
        self.assertEqual(saves[0][3],blob)

    def test_only_button_label_changes_but_checkout_and_title_remain_correct(self):
        root=Path(__file__).resolve().parents[1]/"web_galerias"
        html=(root/"index.html").read_text(encoding="utf-8")
        js=(root/"app.js").read_text(encoding="utf-8")
        css=(root/"style.css").read_text(encoding="utf-8")
        self.assertEqual(html.count('id="edit-header_mode"'),1)
        self.assertEqual(html.count('id="edit-button_mode"'),1)
        self.assertEqual(html.count('id="upload-header"'),1)
        self.assertEqual(html.count('id="upload-button-label-image"'),1)
        self.assertIn('class="primary">ENTRAR A LA GALERÍA</button>',html)
        self.assertIn('"logo","header","button-label-image"',js)
        self.assertIn('"library-brand","library-eyebrow","library-heading"',js)
        self.assertIn('"intro-photo","intro","web-promo"',js)
        self.assertIn('const buttonReady=renderEntryButtonLabel(',js)
        self.assertNotIn('COMPRAR MEMBRESÍA',js)
        self.assertIn('if(S.me.allowed){show("library");return;}',js)
        self.assertIn('button.replaceChildren(image)',js)
        self.assertIn('button.setAttribute("aria-label",text)',js)
        self.assertIn('const imageUrl="/web-galerias/assets/button-label-fast"',js)
        self.assertIn('image.src=imageUrl;',js)
        self.assertIn('.enter-label-image',css)
        self.assertIn('height:var(--entry-label-image-height,30px)',css)
        self.assertIn('height:56px;min-height:56px',css)
        self.assertIn('max-height:48px',css)
        for key in ("title_image_height","button_image_height"):
            self.assertEqual(html.count('id="edit-'+key+'"'),1)
            self.assertIn('design[key]=Number($("edit-"+key).value)',js)
        self.assertIn('function updateEditorImageSizes()',js)
        self.assertIn('image.style.height=height+"px"',js)
        self.assertIn('button.style.setProperty("--entry-label-image-height",height+"px")',js)
        self.assertIn('id="edit-title-image-preview"',html)
        self.assertIn('id="edit-button-image-preview"',html)
        self.assertIn('background:var(--button)',css)


if __name__=="__main__":
    unittest.main()
