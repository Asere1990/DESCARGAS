"""Fast intro tests: public branding can paint before private signed bootstrap."""
import io
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock

from aiohttp import web
from PIL import Image

from web_galerias import admin,fast_intro
from web_galerias.app import intro_page_headers,HEADERS

ROOT=Path(__file__).resolve().parents[1]/"web_galerias"
PAGE=(ROOT/"index.html").read_text(encoding="utf-8")


class FirstPaintTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_current_image_and_button_text_before_any_user_lookup(self):
        result=fast_intro.fast_intro_html(PAGE,{
            "title":"📸 𝐆𝐀𝐋𝐄𝐑𝐈𝐀𝐒 𝐂𝐔𝐁𝐀𝐍𝐀𝐒®",
            "button_label":"ENTRAR A GALERIAS CUBANAS",
            "header_mode":"image","button_mode":"text",
            "intro_caption":"Galerías privadas",
            "title_image_height":160,"button_color":"#123ABC",
        },["header"])
        self.assertIn("fast-identity",result)
        self.assertIn("data-fast-title",result)
        self.assertIn('src="/web-galerias/assets/header-fast"',result)
        self.assertIn('loading="eager" decoding="async"',result)
        self.assertIn("ENTRAR A GALERIAS CUBANAS</button>",result)
        self.assertIn('<html lang="es" style="--button:#123ABC;--accent:#2196f3;--bg:#0b0d12;--text:#ffffff"><head>',result)
        self.assertIn('id="enter" class="primary" disabled',result)
        self.assertIn('data-fast-title-height="160"',result)
        self.assertIn('height="160"',result)
        self.assertIn('href="/web-galerias/assets/header-fast"',result)
        self.assertNotIn('href="/web-galerias/assets/button-label-fast"',result)
        self.assertNotIn('id="intro-headline" class="intro-headline">Web Galerías</div>',result)
        self.assertNotIn('"allowed":true',result)
        self.assertNotIn("tgWebAppData=",result)

    def test_first_color_has_strict_inline_style_hash_without_weakening_csp(self):
        import base64
        import hashlib
        page=fast_intro.fast_intro_html(PAGE,{"button_color":"#0A8BCF"},[])
        policy=intro_page_headers(page)["Content-Security-Policy"]
        expected=base64.b64encode(
            hashlib.sha256(b"--button:#0A8BCF;--accent:#2196f3;--bg:#0b0d12;--text:#ffffff").digest()).decode("ascii")
        self.assertIn("'unsafe-hashes' 'sha256-"+expected+"'",policy)
        self.assertNotIn("'unsafe-inline'",policy)
        self.assertEqual(intro_page_headers(PAGE),HEADERS)

    def test_custom_label_image_is_eager_and_payment_gate_locked(self):
        source=fast_intro.fast_intro_html(PAGE,{
            "title":"Galería","button_label":"VER GALERÍA",
            "header_mode":"text","button_mode":"image",
            "button_image_height":34
        },["button-label-image"])
        self.assertIn('data-fast-button',source)
        self.assertIn('data-fast-button-height="34"',source)
        self.assertIn('height="34"',source)
        self.assertIn('href="/web-galerias/assets/button-label-fast"',source)
        self.assertNotIn('href="/web-galerias/assets/header-fast"',source)
        self.assertIn('disabled aria-label="VER GALERÍA"',source)

    def test_public_identity_does_not_allow_html_injection(self):
        text='</div><script>alert(1)</script> & "quoted"'
        source=fast_intro.fast_intro_html(PAGE,{
            "title":text,"button_label":text,"intro_caption":text,
            "header_mode":"text","button_mode":"text",
            "button_color":"red;url(bad)"},
            [])
        self.assertNotIn('<script>alert(1)</script>',source)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;",source)
        self.assertIn("&#x27;" if False else '&quot;quoted&quot;',source)
        self.assertNotIn("--button:red;url(bad)",source)
        self.assertIn('style="--button:#198ee8;--accent:#2196f3;--bg:#0b0d12;--text:#ffffff"',source)

    def test_unavailable_metadata_falls_back_to_original_atomic_login(self):
        page=fast_intro.fast_intro_html(PAGE,{"title":"TEST"},[])
        self.assertIn("fast-identity",page)
        self.assertEqual(fast_intro.fast_intro_html(
            "not a full page",{"title":"TEST"},[]),"not a full page")

    async def test_public_identity_fetch_uses_one_current_snapshot_no_private_data(self):
        # The exact saved public design, asset revisions and poster revision
        # are consistent in ONE SQL statement even during an admin edit.
        db=N(fetchrow=AsyncMock(return_value={
          "design":{"header_mode":"image","button_mode":"text","title":"Welcome"},
          "promo_updated":"1234.5",
          "assets":{"header":1111.5,"intro-legacy-poster":1000.0}}))
        g=N(ready=True,design=AsyncMock(),db=lambda:db)
        result=await fast_intro.render_fast_intro(g,PAGE)
        self.assertIn('data-fast-title',result)
        self.assertIn('src="/web-galerias/assets/header-fast?v=1111500"',result)
        self.assertIn('href="/web-galerias/assets/header-fast?v=1111500"',result)
        self.assertIn('data-hero-revision=',result)
        query,keys=db.fetchrow.await_args.args
        self.assertIn("jsonb_object_agg",query)
        self.assertIn("WHERE key=ANY($1::text[])",query)
        self.assertIn("key='design'",query)
        self.assertIn("key='intro_promo'",query)
        self.assertNotIn("wg_subscriptions",query)
        self.assertNotIn("descarga_accounts",query)
        self.assertEqual(keys,["header","button-label-image",
            "library-brand","library-eyebrow","library-heading",
            "intro-photo","intro","intro-legacy-poster"])
        db.fetchrow.assert_awaited_once()
        g.design.assert_not_awaited()
        not_ready=N(ready=False,db=lambda:db)
        self.assertEqual(await fast_intro.render_fast_intro(not_ready,PAGE),PAGE)
        db.fetchrow.assert_awaited_once()

    def test_every_new_editor_revision_is_present_on_first_frame(self):
        design={"title":"Nueva portada","intro_label":"NUEVO CONTENIDO",
                "intro_caption":"Cambios publicados",
                "intro_fit":"contain","button_color":"#112233",
                "accent":"#445566","background":"#010203","text_color":"#EEDDCC"}
        revisions={"header":1234.8,"button-label-image":2234.1}
        page=fast_intro.fast_intro_html(
            PAGE,design,["header","button-label-image"],revisions,"ab12cd34ef56")
        self.assertIn("<title>Nueva portada</title>",page)
        self.assertIn("NUEVO CONTENIDO",page)
        self.assertIn("Cambios publicados",page)
        self.assertIn("intro-fit-contain",page)
        self.assertIn("data-hero-revision=\"ab12cd34ef56\"",page)
        self.assertEqual(page.count('/assets/intro-poster?v=ab12cd34ef56'),3)
        self.assertIn('style="--button:#112233;--accent:#445566;--bg:#010203;--text:#EEDDCC"',page)
        self.assertIn("'unsafe-hashes'",intro_page_headers(page)["Content-Security-Policy"])
        self.assertNotIn("'unsafe-inline'",intro_page_headers(page)["Content-Security-Policy"])

    async def test_fast_header_is_smaller_without_touching_original_asset(self):
        source=Image.effect_noise((1250,650),45).convert("RGB")
        payload=io.BytesIO()
        source.save(payload,"PNG",optimize=True)
        original=payload.getvalue()
        self.assertGreater(len(original),120000)
        db=N(fetchrow=AsyncMock(return_value={
            "mime":"image/png","body":original,"updated":1000.125}))
        g=N(db=lambda:db)
        req=N(match_info={"key":"header-fast"},headers={
            "Accept":"image/avif,image/webp,image/*"})
        result=await admin.asset(g,req)
        self.assertEqual(result.status,200)
        self.assertEqual(result.content_type,"image/webp")
        self.assertLess(len(result.body),len(original))
        with Image.open(io.BytesIO(result.body)) as image:
            self.assertLessEqual(image.size[0],2048)
            self.assertLessEqual(image.size[1],780)
        self.assertEqual(db.fetchrow.return_value["body"],original)
        self.assertIn("Vary",result.headers)
        repeated=N(match_info={"key":"header-fast"},
                   headers={"Accept":"image/webp",
                            "If-None-Match":result.headers["ETag"]})
        reused=await admin.asset(g,repeated)
        self.assertEqual(reused.status,304)
        full=N(match_info={"key":"header"},headers={"Accept":"image/webp"})
        untouched=await admin.asset(g,full)
        self.assertEqual(untouched.body,original)
        self.assertEqual(untouched.content_type,"image/png")


if __name__=="__main__":
    unittest.main()
