"""Three independently editable gallery library headings, no intro or quota effects."""
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch

from aiohttp import web
from web_galerias import admin,fast_intro
from web_galerias.core import DEFAULTS,ASSET_MIME,MAX_ASSET

PAGE=(Path(__file__).resolve().parents[1]/"web_galerias"/"index.html").read_text("utf-8")

class LibraryIdentityTests(unittest.IsolatedAsyncioTestCase):
    def test_three_distinct_defaults_while_intro_title_stays_independent(self):
        self.assertEqual(DEFAULTS["title"],"Web Galerías")
        for key,txt in [("library_brand","Web Galerías"),
                        ("library_eyebrow","MI COLECCIÓN"),
                        ("library_heading","Mis galerías")]:
            self.assertEqual(DEFAULTS[key],txt)
            self.assertEqual(DEFAULTS[key+"_mode"],"text")
            self.assertIn(key.replace("_","-"),ASSET_MIME)
            self.assertEqual(MAX_ASSET[key.replace("_","-")],5_000_000)
            self.assertEqual(PAGE.count('id="edit-'+key+'"'),1)
            self.assertEqual(PAGE.count('id="edit-'+key+'_mode"'),1)
            self.assertEqual(PAGE.count('id="upload-'+key.replace("_","-")+'"'),1)

    def test_arbitrary_html_is_escaped_before_any_customer_uses_it(self):
        malicious='Mi <script>alert("bad")</script> & "galería"'
        result=fast_intro.fast_intro_html(PAGE,{
            "library_brand":malicious,
            "library_eyebrow":"NUEVA COLECCIÓN",
            "library_heading":"Archivos personales",
            "title":"La portada sigue independiente",
        },[])
        self.assertIn("NUEVA COLECCIÓN",result)
        self.assertIn("Archivos personales",result)
        self.assertIn("La portada sigue independiente",result)
        self.assertNotIn('<script>alert("bad")</script>',result)
        self.assertIn("&lt;script&gt;",result)
        self.assertIn('id="brand-name"',result)

    def test_first_paint_reuses_saved_pictures_without_fetching_private_data(self):
        result=fast_intro.fast_intro_html(PAGE,{
            "library_brand_mode":"image",
            "library_eyebrow_mode":"image",
            "library_heading_mode":"image",
            "library_brand_image_height":80,
            "library_eyebrow_image_height":36,
            "library_heading_image_height":120,
        },["library-brand","library-eyebrow","library-heading"])
        for key,size in [("library_brand",80),("library_eyebrow",36),("library_heading",120)]:
            self.assertIn('data-library-image="'+key+'"',result)
            self.assertIn('src="/web-galerias/assets/'+key.replace("_","-")+'"',result)
            self.assertIn('height="'+str(size)+'"',result)
        self.assertEqual(result.count('data-library-image='),3)
        self.assertNotIn('class="eyebrow" id="collection-eyebrow" aria-label="MI COLECCIÓN"',result)
        self.assertNotIn("tgWebAppData",result)
        self.assertNotIn('"allowed"',result)

    def test_absent_uploaded_image_falls_back_to_selected_text(self):
        result=fast_intro.fast_intro_html(PAGE,{
            "library_heading_mode":"image","library_heading":"NUEVO CATÁLOGO"
        },[])
        self.assertIn('>NUEVO CATÁLOGO</h1>',result)
        self.assertNotIn('data-library-image=',result)

    def test_text_limits_image_modes_and_sizes_are_validated(self):
        valid=admin.cleaned_design({
            "library_brand":"Nombre","library_heading_mode":"image",
            "library_eyebrow_image_height":44,"library_heading_image_height":100})
        self.assertEqual(valid["library_brand"],"Nombre")
        for patch_data in ({"library_brand_mode":"script"},
                           {"library_heading_image_height":999},
                           {"library_eyebrow":None},
                           {"library_brand":"X"*161}):
            with self.subTest(patch=patch_data),self.assertRaises(web.HTTPBadRequest):
                admin.cleaned_design(patch_data)

    async def test_uploaded_image_required_to_avoid_blank_heading(self):
        db=N(fetchval=AsyncMock(return_value=False),execute=AsyncMock())
        g=N(db=lambda:db,design=AsyncMock(return_value=dict(DEFAULTS)),log=AsyncMock())
        with patch.object(admin,"is_admin",return_value=True):
            with self.assertRaises(web.HTTPBadRequest):
                await admin.admin_op(g,1234,"admin-design",{
                    "design":{"library_heading_mode":"image"}
                })
        db.execute.assert_not_awaited()
        self.assertEqual(db.fetchval.await_args.args[1],"library-heading")

    async def test_old_editor_preserves_new_independent_branding(self):
        saved=dict(DEFAULTS)
        saved.update(library_brand="Nombre nuevo",
                     library_eyebrow="GALERÍA PRIVADA",library_heading="Mi catálogo")
        db=N(fetchval=AsyncMock(),execute=AsyncMock())
        g=N(db=lambda:db,design=AsyncMock(return_value=saved),log=AsyncMock())
        with patch.object(admin,"is_admin",return_value=True):
            result=await admin.admin_op(g,1234,"admin-design",{
                "design":{"title":"Titular de portada"}
            })
        self.assertEqual(result["design"]["title"],"Titular de portada")
        self.assertEqual(result["design"]["library_brand"],"Nombre nuevo")
        self.assertEqual(result["design"]["library_eyebrow"],"GALERÍA PRIVADA")
        self.assertEqual(result["design"]["library_heading"],"Mi catálogo")
        self.assertNotIn("UPDATE wg_media",db.execute.await_args.args[0])

if __name__=="__main__":
    unittest.main()
