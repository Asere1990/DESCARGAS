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
        for control in ("go-intro","open-support-chat","admin-open","admin-back","admin-refresh",
                        "back-library","select-mode","selection-cancel",
                        "selection-download","selection-original","close-viewer","viewer-download",
                        "viewer-prev","viewer-next"):
            self.assertEqual(len(self.doc.buttons.get(control,[])),1,control)

    def test_bottom_buttons_instead_of_unreachable_header_icons(self):
        self.assertEqual(len(self.doc.nav_classes),4)
        self.assertEqual(set(self.doc.nav_classes[0]),{"go-intro","open-support-chat","admin-open"})
        self.assertEqual(set(self.doc.nav_classes[1]),{"back-library","select-mode"})
        self.assertEqual(set(self.doc.nav_classes[2]),{"selection-cancel","selection-organize","selection-original","selection-admin-delete","selection-download"})
        self.assertIn('id="selection-admin-delete" type="button" class="mobile-action mobile-action-danger hidden"',self.html)
        self.assertIn('classList.toggle("hidden",!S.me?.admin)',self.js)
        self.assertEqual(set(self.doc.nav_classes[3]),{"admin-back","admin-refresh","browser-logout"})
        headers=re.findall(r"<header\b[^>]*>.*?</header>",self.html,re.DOTALL)
        for header in headers:
            for identifier in ("go-intro","open-support-chat","admin-open","admin-back","admin-refresh",
                               "back-library","select-mode","selection-cancel",
                               "selection-download","close-viewer","viewer-download",
                               "viewer-prev","viewer-next"):
                self.assertNotIn(f'id="{identifier}"',header)

    def test_library_brand_is_visible_below_swiftgram_toolbar(self):
        library=self.html.split('<section id="library"',1)[1].split('</section>',1)[0]
        header=library.split('<header',1)[1].split('</header>',1)[0]
        self.assertIn('library-safe-spacer',header)
        self.assertNotIn('id="brand-logo"',header)
        self.assertNotIn('id="brand-name"',header)
        self.assertNotIn('<button',header)
        self.assertEqual(library.count('id="brand-logo"'),1)
        self.assertEqual(library.count('id="brand-name"'),1)
        brand=library.index('class="library-branding"')
        eyebrow=library.index('class="eyebrow">MI COLECCIÓN')
        heading=library.index('<h1>Mis galerías</h1>')
        self.assertLess(brand,eyebrow)
        self.assertLess(eyebrow,heading)
        self.assertIn('#library .library-safe-spacer{',self.css)
        self.assertIn('min-height:70px;height:70px',self.css)
        self.assertIn('#library .library-branding{',self.css)
        self.assertIn('position:static;top:auto;z-index:auto',self.css)

    def test_video_has_no_middle_star_and_placeholder_does_not_overlay(self):
        intro=self.html.split('<section id="intro"',1)[1].split('</section>',1)[0]
        video=intro.split('class="intro-video-wrap"',1)[1].split('</div>',1)[0]
        self.assertNotIn('✦',video)
        self.assertNotIn('<span>✦</span>',self.html)
        self.assertIn('id="intro-placeholder" class="intro-placeholder"',intro)
        self.assertIn('class="intro-placeholder-label"',intro)
        self.assertIn('#intro .intro-placeholder[hidden]',self.css)
        self.assertIn('#intro.has-intro-video #intro-placeholder',self.css)
        self.assertIn('display:none!important',self.css)
        self.assertIn('video.hidden=false;placeholder.hidden=true;',self.js)
        self.assertIn('video.hidden=true;video.classList.remove("intro-playing");',self.js)
        self.assertIn('if(!posterReady())',self.js)
        self.assertIn('placeholder.hidden=false;',self.js)

    def test_filter_is_below_search_outside_swiftgram_header(self):
        album=self.html.split('<section id="album"',1)[1].split('</section>',1)[0]
        header=album.split('<header',1)[1].split('</header>',1)[0]
        self.assertNotIn('id="filter-toggle"',header)
        self.assertNotIn('<button',header)
        search_pos=album.index('id="query"')
        filter_pos=album.index('id="filter-toggle"')
        drop_tray_pos=album.index('id="folder-drop-tray"')
        self.assertLess(search_pos,filter_pos)
        self.assertLess(filter_pos,drop_tray_pos)
        self.assertEqual(album.count('id="filter-toggle"'),1)
        self.assertIn('class="album-filter-bar"',album)
        self.assertIn('aria-expanded="false"',album)
        for kind in ('all','photo','video'):
            self.assertIn('data-kind="'+kind+'"',album)
        self.assertIn('top:calc(100% + 6px)',self.css)
        self.assertIn('min-height:48px',self.css)
        self.assertIn('#album .folder-drop-tray{',self.css)
        self.assertIn('position:relative;top:auto;z-index:auto',self.css)

    def test_accessibility_and_permissions_unchanged(self):
        for nav in self.doc.nav_classes:
            for control in nav.values():
                self.assertTrue(control.get("aria-label"))
                self.assertEqual(control.get("type"),"button")
        self.assertIn('id="admin-open" type="button" class="mobile-action mobile-action-primary hidden"',self.html)
        self.assertIn('$("admin-open").classList.toggle("hidden",!S.me.admin||ADMIN_ACCOUNT_VIEW)',self.js)

    def test_touch_target_size_and_both_mobile_safe_areas(self):
        self.assertIn("min-height:54px",self.css)
        self.assertIn("env(safe-area-inset-bottom,0px)",self.css)
        self.assertIn("--tg-safe-area-inset-bottom",self.css)
        self.assertIn("--tg-content-safe-area-inset-bottom",self.css)
        self.assertIn("touch-action:manipulation",self.css)
        self.assertIn("#admin .container.editor",self.css)

    def test_album_and_viewer_controls_are_bottom_only(self):
        self.assertIn('id="album-actions" class="mobile-actions"',self.html)
        self.assertIn('id="selection-bar" class="mobile-actions selection-actions hidden"',self.html)
        viewer=self.html.split('<nav class="viewer-bottom"')[1].split("</nav>")[0]
        for control in ("close-viewer","viewer-prev","viewer-next","viewer-download"):
            self.assertIn(f'id="{control}"',viewer)
        self.assertIn("min-height:55px",self.css)

    def test_viewer_favorite_is_below_media_not_under_telegram_close(self):
        viewer=self.html.split('<section id="viewer"',1)[1].split('</section>',1)[0]
        top=viewer.split('<div class="viewer-top"',1)[1].split('</div>',1)[0]
        self.assertNotIn('id="viewer-favorite"',top)
        self.assertEqual(viewer.count('id="viewer-favorite"'),1)
        self.assertLess(viewer.index('id="viewer-stage"'),
                        viewer.index('id="viewer-favorite"'))
        self.assertLess(viewer.index('id="viewer-favorite"'),
                        viewer.index('<nav class="viewer-bottom"'))
        self.assertIn('#viewer > .viewer-favorite{',self.css)
        self.assertIn('bottom:calc(98px + max(env(safe-area-inset-bottom,0px)',self.css)
        self.assertIn('right:15px',self.css)
        self.assertIn('z-index:32',self.css)
        self.assertNotIn('#viewer .viewer-top .viewer-favorite{',self.css)
        # The existing favorite click handler and persistence are unchanged.
        self.assertIn('$("viewer-favorite").onclick=toggleViewerFavorite;',self.js)

    def test_non_admin_cannot_see_backup_status(self):
        self.assertIn('S.me?.admin && folder.pending',self.js)
        self.assertIn('addAdminDatum("Nombre público"',self.js)
        self.assertIn('id="new-public-name"',self.html)

    def test_admin_folder_stats_are_separate_labeled_rows(self):
        # No volver al párrafo unido por puntos intermedios en el panel admin.
        section=self.js.split("function previewAdmin(){",1)[1].split("const users=",1)[0]
        self.assertIn('className="admin-folder-data"',section)
        self.assertIn('className="admin-data-row"',section)
        self.assertIn('className="admin-data-label"',section)
        self.assertIn('className="admin-data-value"',section)
        for label in ("Nombre público","Origen de Telegram","Estado del respaldo",
                      "Archivos respaldados","Archivos pendientes","Mensajes recorridos",
                      "Historial inicial","Incidencias sin verificar"):
            self.assertIn('addAdminDatum("'+label+'"',section)
        self.assertNotIn('info.textContent="Nombre público:',section)
        self.assertIn("#admin .admin-data-row{",self.css)
        self.assertIn("flex-direction:column",self.css)
        self.assertIn("Solo indica que se alcanzó el final del historial inicial; el respaldo sigue aparte.",self.js)

    def test_admin_can_assign_telegram_account_when_adding_or_editing_group(self):
        self.assertIn('id="new-source-uid"',self.html)
        self.assertIn('id="new-source-uid" inputmode="text"',self.html)
        self.assertIn('DATOS:123456789',self.html)
        self.assertIn("Cuenta del historial",self.js)
        self.assertIn('api("admin-folder-source"',self.js)
        self.assertIn('api("admin-folder",{peer,name,public_name,...source_account})',self.js)
        self.assertIn('account_system',self.js)
        self.assertIn('f.preferred_uid',self.js)
        self.assertIn('f.source_uid',self.js)
        self.assertIn("Los archivos guardados en Cloudflare permanecerán intactos.",self.js)

    def test_browser_admin_logout_is_hidden_from_telegram_clients(self):
        self.assertIn('id="browser-logout" type="button" class="mobile-action hidden"',self.html)
        self.assertIn('if(BROWSER_ADMIN)',self.js)
        self.assertIn('credentials:"same-origin"',self.js)
        self.assertIn('id="browser-admin-id"',self.html)
        self.assertIn('id="browser-admin-password"',self.html)
        self.assertIn('rel="manifest"',self.html)
        self.assertIn('rel="apple-touch-icon"',self.html)

    def test_button_events_preserved(self):
        for code in ('$("go-intro").onclick=()=>show("intro")',
                     '$("open-support-chat").onclick=()=>openSupportChat()',
                     '$("admin-open").onclick=openAdminScreen',
                     '$("admin-back").onclick=()=>show("library")',
                     '$("admin-refresh").onclick=loadAdmin'):
            self.assertIn(code,self.js)


if __name__=="__main__":
    unittest.main()
