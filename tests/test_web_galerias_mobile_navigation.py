"""Comprobar navegación accesible en las Mini Apps de iPhone y Android."""
import re
import unittest
from pathlib import Path
from html.parser import HTMLParser

ROOT=Path(__file__).resolve().parents[1]/"web_galerias"

class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.buttons={}
        self.nav_classes=[]
        self.active_nav=None
        self.depth=0

    def handle_starttag(self, tag, attrs):
        data=dict(attrs)
        if tag=="nav" and "mobile-actions" in data.get("class","").split():
            self.active_nav={}
            self.nav_classes.append(self.active_nav)
            self.depth=1
            return
        if self.active_nav is not None:
            self.depth+=1
        if tag=="button" and data.get("id"):
            self.buttons.setdefault(data["id"],[]).append({
                "nav":self.active_nav,"attrs":data})
            if self.active_nav is not None:
                self.active_nav[data["id"]]=data

    def handle_endtag(self,tag):
        if self.active_nav is not None:
            self.depth-=1
            if self.depth==0:
                self.active_nav=None


class GalleryMobileNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html=(ROOT/"index.html").read_text(encoding="utf-8")
        cls.css=(ROOT/"style.css").read_text(encoding="utf-8")
        cls.js=(ROOT/"app.js").read_text(encoding="utf-8")
        cls.doc=Elements()
        cls.doc.feed(cls.html)

    def test_no_duplicate_controls(self):
        for control in ("go-intro","admin-open","admin-back","admin-refresh"):
            self.assertEqual(len(self.doc.buttons.get(control,[])),1,control)

    def test_bottom_buttons_instead_of_unreachable_header_icons(self):
        self.assertEqual(len(self.doc.nav_classes),2)
        self.assertEqual(set(self.doc.nav_classes[0]),{"go-intro","admin-open"})
        self.assertEqual(set(self.doc.nav_classes[1]),{"admin-back","admin-refresh"})
        headers=re.findall(r"<header\b[^>]*>.*?</header>",self.html,re.DOTALL)
        for header in headers:
            for identifier in ("go-intro","admin-open","admin-back","admin-refresh"):
                self.assertNotIn(f'id="{identifier}"',header)

    def test_accessibility_and_permissions_unchanged(self):
        for nav in self.doc.nav_classes:
            for control in nav.values():
                self.assertTrue(control.get("aria-label"))
                self.assertEqual(control.get("type"),"button")
        self.assertIn('id="admin-open" type="button" class="mobile-action mobile-action-primary hidden"',self.html)
        self.assertIn('$("admin-open").classList.toggle("hidden",!S.me.admin)',self.js)

    def test_touch_target_size_and_both_mobile_safe_areas(self):
        self.assertIn("min-height:54px",self.css)
        self.assertIn("env(safe-area-inset-bottom,0px)",self.css)
        self.assertIn("--tg-safe-area-inset-bottom",self.css)
        self.assertIn("--tg-content-safe-area-inset-bottom",self.css)
        self.assertIn("touch-action:manipulation",self.css)
        self.assertIn("#admin .container.editor",self.css)

    def test_button_events_preserved(self):
        for code in ('$("go-intro").onclick=()=>show("intro")',
                     '$("admin-open").onclick=async()=>',
                     '$("admin-back").onclick=()=>show("library")',
                     '$("admin-refresh").onclick=loadAdmin'):
            self.assertIn(code,self.js)


if __name__=="__main__":
    unittest.main()
