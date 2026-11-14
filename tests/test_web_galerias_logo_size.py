"""Logo size belongs only to the Web Galerías visual design, not uploads or R2."""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from aiohttp import web
from web_galerias import admin
from web_galerias.core import DEFAULTS


class GalleryLogoSizeTests(unittest.IsolatedAsyncioTestCase):
    def test_default_size_matches_existing_logo_and_range_is_bounded(self):
        self.assertEqual(DEFAULTS["logo_size"],49)
        for size in (24,25,49,96,160,200):
            with self.subTest(size=size):
                self.assertEqual(admin.cleaned_design({"logo_size":size})["logo_size"],size)
        for invalid in (-1,0,23,201,999,True,None,"49",49.5):
            with self.subTest(size=invalid),self.assertRaises(web.HTTPBadRequest):
                admin.cleaned_design({"logo_size":invalid})

    async def test_old_admin_editor_keeps_a_previously_saved_logo_size(self):
        old={**DEFAULTS,"logo_size":147}
        db=N(execute=AsyncMock(),fetchval=AsyncMock())
        g=N(db=lambda:db,design=AsyncMock(return_value=old),log=AsyncMock())
        with patch.object(admin,"is_admin",return_value=True):
            saved=await admin.admin_op(g,1123,"admin-design",{"design":{"title":"Nombre nuevo"}})
        self.assertEqual(saved["design"]["logo_size"],147)
        self.assertEqual(saved["design"]["title"],"Nombre nuevo")
        payload=json.loads(db.execute.await_args.args[1])
        self.assertEqual(payload["logo_size"],147)
        db.fetchval.assert_not_awaited()

    async def test_new_size_saved_without_changing_any_uploaded_asset(self):
        existing={**DEFAULTS,"logo_size":49}
        db=N(execute=AsyncMock(),fetchval=AsyncMock())
        g=N(db=lambda:db,design=AsyncMock(return_value=existing),log=AsyncMock())
        with patch.object(admin,"is_admin",return_value=True):
            response=await admin.admin_op(g,1123,"admin-design",{
                "design":{"title":existing["title"],"logo_size":132}})
        self.assertTrue(response["ok"])
        self.assertEqual(response["design"]["logo_size"],132)
        sql,payload=db.execute.await_args.args
        self.assertIn("wg_config",sql)
        self.assertNotIn("wg_assets",sql)
        self.assertNotIn("wg_media",sql)
        self.assertNotIn("wg_subscriptions",sql)
        self.assertEqual(json.loads(payload)["logo_size"],132)

    def test_logo_asset_is_unchanged_in_uploaded_files(self):
        html=(Path(__file__).resolve().parents[1]/"web_galerias"/"index.html").read_text("utf-8")
        self.assertEqual(html.count('id="upload-logo"'),1)
        self.assertEqual(html.count('id="edit-logo_size"'),1)
        self.assertEqual(html.count('id="edit-logo-image-preview"'),1)
        self.assertIn('min="24" max="200" step="1" value="49"',html)
        self.assertIn('Tamaño del logo',html)


if __name__=="__main__":
    unittest.main()
