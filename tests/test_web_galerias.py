"""Pruebas de aislamiento y seguridad del piloto Web Galerías."""
import json
import time
import unittest
from pathlib import Path
from cryptography.fernet import Fernet
from aiohttp import web
from web_galerias.permissions import is_admin,is_tester,pilot_allowed,button_visible
from web_galerias.admin import cleaned_design
from web_galerias.downloads import temporary_link,validate_link
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
    def test_unlisted_user_is_not_pilot(self):
        self.assertFalse(is_admin(12345))
        self.assertFalse(is_tester(12345))
        self.assertFalse(pilot_allowed(12345))

if __name__=="__main__":unittest.main()
