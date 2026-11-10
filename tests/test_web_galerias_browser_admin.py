"""Autenticación segura y persistente de admins.txt desde el navegador/PWA.

No utiliza claves o sesiones de Telegram ni modifica DESCARGAS.
"""
import asyncio
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from aiohttp import web
from web_galerias import admin_browser
from web_galerias.core import GalleryCore
from web_galerias.app import app_icon


class Request:
    def __init__(self,body=None,token="",origin="https://cloudflare-t2gf.onrender.com",
                 host="cloudflare-t2gf.onrender.com",web_header="1"):
        self.host=host
        self.remote="198.51.100.41"
        self.headers={"X-Gallery-Web":web_header,"Origin":origin,"Sec-Fetch-Site":"same-origin"}
        self.content_length=None
        self.cookies=({admin_browser.COOKIE_NAME:token} if token else {})
        self._data=body
        self._metadata={}
    async def json(self):
        return self._data
    def __getitem__(self,key):
        return self._metadata[key]
    def __setitem__(self,key,value):
        self._metadata[key]=value
    def get(self,key,default=None):
        return self._metadata.get(key,default)


class BrowserAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env=patch.dict(os.environ,{
            "PASSWORD":"Ejemplo contraseña de prueba 2026",
            "DESCARGAS_SESSION_KEY":"Solo para tests: secreto del servicio",
        })
        self.env.start()
        self.db=SimpleNamespace(execute=AsyncMock(),fetchrow=AsyncMock(return_value=None))
        self.g=SimpleNamespace(db=lambda:self.db)

    async def asyncTearDown(self):
        self.env.stop()

    async def test_admin_login_issues_secure_cookie_not_password(self):
        response=await admin_browser.login(self.g,Request({
            "uid":"7539025033","password":"Ejemplo contraseña de prueba 2026"}))
        name=admin_browser.COOKIE_NAME
        self.assertIn(name,response.cookies)
        self.assertTrue(response.cookies[name]["secure"])
        self.assertTrue(response.cookies[name]["httponly"])
        self.assertEqual(response.cookies[name]["samesite"],"Strict")
        self.assertEqual(response.cookies[name]["path"],"/web-galerias")
        self.assertEqual(int(response.cookies[name]["max-age"]),admin_browser.SESSION_SECONDS)
        self.assertNotIn("Ejemplo contraseña de prueba 2026",
                         str(self.db.execute.await_args_list))
        self.assertNotIn("7539025033",response.cookies[name].value)

    async def test_test_user_cannot_authenticate_even_with_admin_password(self):
        with self.assertRaises(web.HTTPUnauthorized):
            await admin_browser.login(self.g,Request({
                "uid":"5662710947","password":"Ejemplo contraseña de prueba 2026"}))
        self.assertTrue(any("wg_admin_login_attempts" in call.args[0]
                            for call in self.db.execute.await_args_list))
        self.assertFalse(any("INSERT INTO wg_admin_sessions" in call.args[0]
                             for call in self.db.execute.await_args_list))

    async def test_wrong_password_does_not_create_session(self):
        with self.assertRaises(web.HTTPUnauthorized):
            await admin_browser.login(self.g,Request({"uid":"7539025033","password":"wrong"}))
        self.assertFalse(any("INSERT INTO wg_admin_sessions" in call.args[0]
                             for call in self.db.execute.await_args_list))

    async def test_missing_password_configuration_fails_closed(self):
        with patch.dict(os.environ,{"PASSWORD":""}):
            with self.assertRaises(web.HTTPServiceUnavailable):
                await admin_browser.login(self.g,Request({
                    "uid":"7539025033","password":""}))

    async def test_cookie_authorizes_only_current_admin(self):
        token="A"*43
        now=time.time()
        self.db.fetchrow.return_value={
            "uid":7539025033,"password_version":admin_browser.password_version(),
            "last_seen":now,"expires_at":now+86400}
        self.assertEqual(await admin_browser.authenticate(self.g,Request(token=token)),
                         7539025033)
        with patch("web_galerias.admin_browser.is_admin",return_value=False):
            with self.assertRaises(web.HTTPUnauthorized):
                await admin_browser.authenticate(self.g,Request(token=token))

    async def test_password_change_revokes_existing_browser_session(self):
        token="A"*43
        now=time.time()
        old=admin_browser.password_version()
        self.db.fetchrow.return_value={
            "uid":7539025033,"password_version":old,
            "last_seen":now,"expires_at":now+86400}
        with patch.dict(os.environ,{"PASSWORD":"different password"}):
            with self.assertRaises(web.HTTPUnauthorized):
                await admin_browser.authenticate(self.g,Request(token=token))

    async def test_expired_cookie_is_rejected(self):
        now=time.time()
        self.db.fetchrow.return_value={
            "uid":7539025033,"password_version":admin_browser.password_version(),
            "last_seen":now-86400,"expires_at":now-2}
        with self.assertRaises(web.HTTPUnauthorized):
            await admin_browser.authenticate(self.g,Request(token="A"*43))

    async def test_cookie_renews_for_active_browser(self):
        now=time.time()
        self.db.fetchrow.return_value={
            "uid":7539025033,"password_version":admin_browser.password_version(),
            "last_seen":now-2*86400,"expires_at":now+86400}
        request=Request(token="A"*43)
        uid=await admin_browser.authenticate(self.g,request)
        self.assertEqual(uid,7539025033)
        self.assertEqual(request["wg_admin_renew"],"A"*43)
        self.assertIn("UPDATE wg_admin_sessions",self.db.execute.await_args.args[0])

    async def test_cross_origin_and_missing_header_cannot_use_cookie(self):
        with self.assertRaises(web.HTTPForbidden):
            await admin_browser.authenticate(self.g,Request(
                token="A"*43,origin="https://malicious.example"))
        with self.assertRaises(web.HTTPForbidden):
            await admin_browser.authenticate(self.g,Request(
                token="A"*43,web_header=""))
        self.db.fetchrow.assert_not_awaited()

    async def test_logout_revokes_single_device_cookie(self):
        now=time.time()
        self.db.fetchrow.return_value={
            "uid":7539025033,"password_version":admin_browser.password_version(),
            "last_seen":now,"expires_at":now+86400}
        response=await admin_browser.logout(self.g,Request(token="A"*43))
        self.assertIn("DELETE FROM wg_admin_sessions",self.db.execute.await_args.args[0])
        self.assertIn(admin_browser.COOKIE_NAME,response.cookies)

    async def test_telegram_initdata_is_kept_as_independent_auth(self):
        core=GalleryCore(SimpleNamespace(token="123:fake",repo=SimpleNamespace(pool=self.db)))
        core.ready=True
        # Con datos Telegram incorrectos no se permite fallback a cookie.
        req=Request(token="A"*43)
        req.headers["X-Telegram-Init-Data"]="invalid Telegram initData"
        with self.assertRaises(web.HTTPUnauthorized):
            await core.auth(req)
        self.db.fetchrow.assert_not_awaited()

    def test_home_screen_icons_are_png(self):
        for size in (180,192,512):
            self.assertTrue(app_icon(size).startswith(b"\x89PNG\r\n\x1a\n"))


if __name__=="__main__":
    unittest.main()
