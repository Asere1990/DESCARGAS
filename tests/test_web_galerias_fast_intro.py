"""Fast intro tests: public branding can paint before private signed bootstrap."""
import io
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock

from aiohttp import web
from PIL import Image

from web_galerias import admin,fast_intro

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
        self.assertIn('<html lang="es" style="--button:#123ABC"><head>',result)
        self.assertIn('id="enter" class="primary" disabled',result)
        self.assertIn('data-fast-title-height="160"',result)
        self.assertIn('height="160"',result)
        self.assertIn('href="/web-galerias/assets/header-fast"',result)
        self.assertNotIn('href="/web-galerias/assets/button-label-fast"',result)
        self.assertNotIn('id="intro-headline" class="intro-headline">Web Galerías</div>',result)
        self.assertNotIn('"allowed":true',result)
        self.assertNotIn("tgWebAppData=",result)

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
        self.assertIn('style="--button:#198ee8"',source)

    def test_unavailable_metadata_falls_back_to_original_atomic_login(self):
        page=fast_intro.fast_intro_html(PAGE,{"title":"TEST"},[])
        self.assertIn("fast-identity",page)
        self.assertEqual(fast_intro.fast_intro_html(
            "not a full page",{"title":"TEST"},[]),"not a full page")

    async def test_public_identity_fetch_reads_only_design_and_asset_keys(self):
        db=N(fetch=AsyncMock(return_value=[{"key":"header"}]))
        g=N(ready=True,design=AsyncMock(return_value={
              "header_mode":"image","button_mode":"text","title":"Welcome"
            }),db=lambda:db)
        result=await fast_intro.render_fast_intro(g,PAGE)
        self.assertIn('data-fast-title',result)
        self.assertIn('WHERE key=ANY($1::text[])',db.fetch.await_args.args[0])
        self.assertEqual(db.fetch.await_args.args[1],
                         ["header","button-label-image"])
        g.design.assert_awaited_once_with()
        db.fetch.assert_awaited_once()
        not_ready=N(ready=False,design=AsyncMock())
        self.assertEqual(await fast_intro.render_fast_intro(not_ready,PAGE),PAGE)
        not_ready.design.assert_not_awaited()

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
