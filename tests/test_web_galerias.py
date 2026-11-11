"""Pruebas de aislamiento y seguridad del piloto Web Galerías."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from pathlib import Path
from cryptography.fernet import Fernet
from aiohttp import web
from web_galerias.permissions import is_admin,is_tester,pilot_allowed,button_visible
from web_galerias.admin import cleaned_design,admin_op
from web_galerias.downloads import temporary_link,validate_link,safe_filename
from web_galerias.personal import folder_name,media_filter,items as personal_items
from web_galerias.core import DEFAULTS,normal,identifier

class FakeService:
    def __init__(self): self.cipher=Fernet(Fernet.generate_key())

class LinkTests(unittest.TestCase):
    def setUp(self):
        self.g=type("G",(),{"s":FakeService()})()
    def test_media_token_is_user_item_and_purpose_bound(self):
        token=temporary_link(self.g,7539025033,77,"view")
        self.assertEqual(validate_link(self.g,token,"view")["uid"],7539025033)
        self.assertEqual(validate_link(self.g,token,"view")["item"],77)
        with self.assertRaises(web.HTTPForbidden):
            validate_link(self.g,token,"thumb")
    def test_no_plaintext_identifier_in_signed_token(self):
        token=temporary_link(self.g,7539025033,77,"view")
        self.assertNotIn("7539025033",token)
    def test_bad_token_rejected(self):
        with self.assertRaises(web.HTTPForbidden):
            validate_link(self.g,"bad","view")

class DesignTests(unittest.TestCase):
    def test_default_title_and_fixed_limit(self):
        self.assertEqual(DEFAULTS["title"],"Web Galerías")
        self.assertEqual(cleaned_design({"title":"Mi Galería"})["daily_limit"],100)
    def test_colors_whitelisted(self):
        with self.assertRaises(web.HTTPBadRequest):
            cleaned_design({"accent":"red; background:url()"})
    def test_code_cannot_be_injected(self):
        with self.assertRaises(web.HTTPBadRequest):
            cleaned_design({"anything":"x"})
    def test_shapes_are_editable(self):
        self.assertEqual(cleaned_design({"tile_shape":"rectangle"})["tile_shape"],"rectangle")
    def test_invalid_shape_rejected(self):
        with self.assertRaises(web.HTTPBadRequest):
            cleaned_design({"tile_shape":"circle"})
    def test_cannot_erase_limit(self):
        with self.assertRaises(web.HTTPBadRequest):
            cleaned_design({"daily_limit":0})
    def test_normalized_search(self):
        self.assertEqual(normal("Pínar del Río"),"pinar del rio")
    def test_nonpositive_id_rejected(self):
        with self.assertRaises(web.HTTPBadRequest):identifier(0)

class AccessTests(unittest.TestCase):
    def test_current_admin_is_recognized(self):
        self.assertTrue(is_admin(7539025033))
        self.assertTrue(pilot_allowed(7539025033))
        self.assertTrue(button_visible(7539025033))
    def test_exact_three_testers_are_authorized_and_wrong_ids_revoked(self):
        for uid in (1609522430,5662710947,7536216348):
            self.assertTrue(is_tester(uid))
            self.assertTrue(pilot_allowed(uid))
            self.assertTrue(button_visible(uid))
        for uid in (8193438800,8829300345):
            self.assertFalse(is_tester(uid))
            self.assertFalse(pilot_allowed(uid))
            self.assertFalse(button_visible(uid))

    def test_unlisted_user_is_not_pilot(self):
        self.assertFalse(is_admin(12345))
        self.assertFalse(is_tester(12345))
        self.assertFalse(pilot_allowed(12345))

class PersonalFoldersTests(unittest.IsolatedAsyncioTestCase):
    def test_names_are_clean_and_limited(self):
        self.assertEqual(folder_name("  Viajes   Cuba  "),"Viajes Cuba")
        for value in ("", " "*8, "a"*61, "\x00test", None):
            with self.assertRaises(web.HTTPBadRequest):
                folder_name(value)

    def test_filters_are_restricted_to_photo_video_all(self):
        for kind in ("all","photo","video"):
            self.assertEqual(media_filter(kind),kind)
        for kind in ("GIF","document","admin",""):
            with self.assertRaises(web.HTTPBadRequest):
                media_filter(kind)

    async def test_no_other_users_personal_folder_is_visible(self):
        pool=SimpleNamespace(fetchval=AsyncMock(return_value=None),
                             fetch=AsyncMock())
        g=SimpleNamespace(db=lambda:pool)
        with self.assertRaises(web.HTTPForbidden):
            await personal_items(g,12345,777,"",0,"all",lambda *x:"")
        pool.fetch.assert_not_awaited()

    def test_save_links_are_short_lived_and_purpose_bound(self):
        g=type("G",(),{"s":FakeService()})()
        token=temporary_link(g,7539025033,77,"save")
        self.assertEqual(validate_link(g,token,"save")["item"],77)
        with self.assertRaises(web.HTTPForbidden):
            validate_link(g,token,"view")

    def test_attachment_filenames_are_sanitized(self):
        self.assertEqual(safe_filename("../../foto 1.jpg"),"foto_1.jpg")
        self.assertEqual(safe_filename("","video/mp4"),"video.mp4")

class IOSGalleryUXTests(unittest.TestCase):
    def test_watermark_longpress_filters_and_folders_available(self):
        root=Path(__file__).resolve().parents[1]/"web_galerias"
        js=(root/"app.js").read_text(encoding="utf-8")
        html=(root/"index.html").read_text(encoding="utf-8")
        for expected in ("Búscame en Google","hoo.be/Cubanas",
                         "attachLongPress","pointerdown","pointermove",
                         'data-kind="photo"','data-kind="video"',
                         'id="create-personal-folder"','id="folder-drop-targets"'):
            self.assertIn(expected,js+html)
        self.assertIn("navigator.share({files})",js)
        self.assertIn("tg.downloadFile",js)
        self.assertIn('id="selection-organize"',html)

    def test_single_file_download_never_redirects_to_cloudflare(self):
        p=Path(__file__).resolve().parents[1]/"web_galerias"/"downloads.py"
        src=p.read_text(encoding="utf-8")
        start=src.index("async def download(g,request):")
        self.assertIn("return await stream_object(",src[start:])
        self.assertNotIn("raise web.HTTPFound",src[start:])
        self.assertIn("max(0,100-used-len(ids))" if False else
                      '"remaining":100-used-len(ids)',src)

class WatermarkEditorTests(unittest.IsolatedAsyncioTestCase):
    def test_defaults_match_requested_mark_and_work_on_both_media(self):
        self.assertTrue(DEFAULTS["wm_enabled"])
        self.assertEqual(DEFAULTS["wm_line1"],"Búscame en Google")
        self.assertEqual(DEFAULTS["wm_line2"],"hoo.be/Cubanas")
        self.assertEqual(DEFAULTS["wm_position"],"center")
        self.assertEqual(DEFAULTS["wm_opacity"],30)
        self.assertEqual(DEFAULTS["wm_size"],20)

    def test_watermark_is_validated_separately_from_other_design(self):
        valid={"wm_enabled":False,"wm_line1":"","wm_line2":"Texto libre",
               "wm_position":"bottom","wm_opacity":52,"wm_size":28,
               "wm_color":"#92abC1"}
        design=cleaned_design(valid)
        for key,value in valid.items():
            self.assertEqual(design[key],value)
        invalid=(
            ("wm_enabled","false"),("wm_enabled",1),
            ("wm_line1","a"*91),("wm_line1","línea\\notra"),
            ("wm_position","left"),("wm_position",[]),
            ("wm_opacity",-1),("wm_opacity",101),("wm_opacity",True),
            ("wm_size",11),("wm_size",43),("wm_size",28.5),
            ("wm_color","red"),("wm_color","#123456;url()"),
        )
        for key,value in invalid:
            with self.subTest(key=key,value=value):
                with self.assertRaises(web.HTTPBadRequest):
                    cleaned_design({key:value})

    def make_admin(self):
        db=SimpleNamespace(execute=AsyncMock())
        current={**DEFAULTS,"title":"Galerías especiales",
                 "accent":"#112233","wm_line1":"Texto anterior",
                 "wm_size":26}
        g=SimpleNamespace(db=lambda:db,design=AsyncMock(return_value=current),
                          log=AsyncMock())
        return g,db,current

    async def test_watermark_only_updates_its_own_fields(self):
        g,db,current=self.make_admin()
        patch_data={key:current[key] for key in (
            "wm_enabled","wm_line1","wm_line2","wm_position","wm_opacity",
            "wm_size","wm_color")}
        patch_data["wm_line1"]="Nueva marca"
        patch_data["wm_position"]="top"
        patch_data["wm_enabled"]=False
        result=await admin_op(g,7539025033,"admin-watermark",{"watermark":patch_data})
        self.assertTrue(result["ok"])
        self.assertEqual(result["design"]["title"],current["title"])
        self.assertEqual(result["design"]["accent"],current["accent"])
        self.assertEqual(result["design"]["wm_line1"],"Nueva marca")
        self.assertFalse(result["design"]["wm_enabled"])
        self.assertEqual(json.loads(db.execute.await_args.args[1])["wm_position"],"top")
        g.log.assert_awaited_once_with(7539025033,"admin-watermark")

    async def test_old_editor_save_does_not_reset_watermark(self):
        g,db,current=self.make_admin()
        result=await admin_op(g,7539025033,"admin-design",
                              {"design":{"title":"Nombre nuevo"}})
        self.assertEqual(result["design"]["title"],"Nombre nuevo")
        self.assertEqual(result["design"]["wm_line1"],"Texto anterior")
        self.assertEqual(result["design"]["wm_size"],26)

    async def test_watermark_rejects_nonadmin_and_incomplete_patch(self):
        g,db,current=self.make_admin()
        with self.assertRaises(web.HTTPForbidden):
            await admin_op(g,123456,"admin-watermark",{"watermark":{}})
        db.execute.assert_not_awaited()
        with self.assertRaises(web.HTTPBadRequest):
            await admin_op(g,7539025033,"admin-watermark",
                           {"watermark":{"wm_line1":"Solo uno"}})
        db.execute.assert_not_awaited()

    def test_frontend_has_realtime_preview_and_separate_save(self):
        root=Path(__file__).resolve().parents[1]/"web_galerias"
        html=(root/"index.html").read_text(encoding="utf-8")
        js=(root/"app.js").read_text(encoding="utf-8")
        for control in ("wm-enabled","wm-line1","wm-line2","wm-position",
                        "wm-opacity","wm-size","wm-color"):
            self.assertIn('id="edit-'+control+'"',html)
        self.assertIn('id="wm-preview-text"',html)
        self.assertIn('id="save-watermark"',html)
        self.assertIn('api("admin-watermark"',js)
        self.assertIn('renderWatermark($("viewer-watermark"),S.me?.design)',js)
        self.assertIn('input.addEventListener("input",previewWatermark)',js)


if __name__=="__main__":unittest.main()
