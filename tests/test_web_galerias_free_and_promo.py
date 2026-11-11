"""Focused regressions: free access, on-demand users, and animated promo GIF."""
import asyncio
import base64
import io
import json
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch

from aiohttp import web
from cryptography.fernet import Fernet
from PIL import Image

from web_galerias import admin,downloads,moderation,permissions,intro_video


class FreeAndPromo(unittest.IsolatedAsyncioTestCase):
    def test_free_access_never_includes_payment_gated_test_user(self):
        with patch.object(permissions,"is_tester",return_value=True),patch.object(
                permissions,"payment_test_user",return_value=False):
            self.assertTrue(permissions.free_access(333))
        with patch.object(permissions,"is_tester",return_value=True),patch.object(
                permissions,"payment_test_user",return_value=True):
            self.assertFalse(permissions.free_access(333))

    async def test_free_users_skip_quota_writes_and_have_unlimited_daily_downloads(self):
        class Conn:
            fetch=AsyncMock(return_value=[
                {"id":11,"size":5,"folder":2,"mime":"image/jpeg","filename":"a.jpg"},
                {"id":12,"size":6,"folder":2,"mime":"image/jpeg","filename":"b.jpg"},
            ])
            execute=AsyncMock()
            fetchval=AsyncMock(return_value=99)
            @asynccontextmanager
            async def transaction(self):
                yield
        c=Conn()
        @asynccontextmanager
        async def acquire():
            yield c
        g=N(entitled=AsyncMock(return_value=True),storage=N(ready=True),
            db=lambda:N(acquire=acquire),s=N(cipher=Fernet(Fernet.generate_key())))
        with patch.object(permissions,"free_access",return_value=True),patch.object(
                permissions,"is_admin",return_value=False):
            result=await downloads.reserve(g,333,[11,12])
        self.assertTrue(result["quota_unlimited"])
        self.assertIsNone(result["remaining"])
        self.assertEqual(result["count"],2)
        self.assertFalse(any("wg_quota" in call.args[0] for call in c.execute.await_args_list))
        c.fetchval.assert_not_awaited()
        c.execute.assert_awaited_once()

    async def test_paid_accounts_keep_100_per_day(self):
        class Conn:
            fetch=AsyncMock(return_value=[
                {"id":11,"size":5,"folder":2,"mime":"image/jpeg","filename":"a.jpg"},
                {"id":12,"size":5,"folder":2,"mime":"image/jpeg","filename":"b.jpg"},
            ])
            execute=AsyncMock()
            fetchval=AsyncMock(return_value=99)
            @asynccontextmanager
            async def transaction(self):
                yield
        c=Conn()
        @asynccontextmanager
        async def acquire():
            yield c
        g=N(entitled=AsyncMock(return_value=True),storage=N(ready=True),
            db=lambda:N(acquire=acquire),s=N(cipher=Fernet(Fernet.generate_key())))
        with patch.object(permissions,"free_access",return_value=False),patch.object(
                permissions,"is_admin",return_value=False):
            with self.assertRaises(web.HTTPForbidden):
                await downloads.reserve(g,444,[11,12])
        self.assertTrue(any("wg_quota" in call.args[0] for call in c.execute.await_args_list))
        c.fetchval.assert_awaited()

    async def test_gif_upload_keeps_animation_without_jpeg_conversion(self):
        images=[Image.new("RGB",(5,5),color) for color in ("red","blue")]
        buff=io.BytesIO()
        images[0].save(buff,format="GIF",save_all=True,append_images=images[1:],
                       duration=[100,200],loop=0)
        gif=buff.getvalue()
        self.assertGreater(len(gif),20)
        class Conn:
            execute=AsyncMock()
            @asynccontextmanager
            async def transaction(self):
                yield
        conn=Conn()
        conn.fetch=AsyncMock(return_value=[{
            "part":0,"total":1,"key":"intro-photo","mime":"image/gif","body":gif}])
        @asynccontextmanager
        async def acquire():
            yield conn
        db=N(acquire=acquire)
        g=N(db=lambda:db,log=AsyncMock())
        data={"key":"intro-photo","mime":"image/gif","upload":"a"*32,"part":0,
              "total":1,"data":base64.b64encode(gif).decode()}
        result=await admin.upload(g,333,data)
        self.assertTrue(result["complete"])
        inserts=[x.args for x in conn.execute.await_args_list
                 if "INSERT INTO wg_assets" in x.args[0]]
        self.assertEqual(len(inserts),1)
        self.assertEqual(inserts[0][2],"image/gif")
        self.assertEqual(inserts[0][3],gif)
        with Image.open(io.BytesIO(inserts[0][3])) as image:
            self.assertEqual(image.n_frames,2)

    async def test_promotional_gif_serves_same_origin_with_animated_mime(self):
        gif=b"GIF89a" + b"animated-content"
        db=N(fetchval=AsyncMock(return_value={"intro_media":"gif"}),
             fetchrow=AsyncMock(return_value={"body":gif,"mime":"image/gif"}))
        g=N(db=lambda:db)
        response=await intro_video.poster_asset(g,N())
        self.assertEqual(response.content_type,"image/gif")
        self.assertEqual(response.body,gif)
        self.assertIn("intro-photo",db.fetchrow.await_args.args[0])

    async def test_paid_stats_do_not_count_free_only_users(self):
        db=N(fetchrow=AsyncMock(return_value={"paid":5,"online":2}))
        g=N(db=lambda:db)
        with patch.object(moderation,"is_admin",return_value=True):
            value=await moderation.operation(g,333,"admin-user-stats",{})
        self.assertEqual(value,{"paid":5,"online":2})
        query=db.fetchrow.await_args.args[0]
        self.assertIn("wg_subscriptions",query)
        self.assertIn("wg_gallery_presence",query)
        self.assertIn("paid_until",query)

    def test_ui_is_gallery_first_and_requires_user_section_selection(self):
        root=Path(__file__).resolve().parents[1]/"web_galerias"
        html=(root/"index.html").read_text(encoding="utf-8")
        script=(root/"app.js").read_text(encoding="utf-8")
        extras=(root/"admin_tools.js").read_text(encoding="utf-8")
        self.assertLess(html.index('id="admin-folders"'),
                        html.index('id="admin-media-manager"'))
        self.assertIn('id="admin-user-section"',html)
        self.assertIn('id="admin-users-details" class="panel-card admin-users-details hidden"',html)
        self.assertIn('id="admin-user-permissions" class="panel-card hidden"',html)
        self.assertIn("showUserSectionMenu",extras)
        self.assertIn("free_access",script)
        self.assertIn('S.me?.free_access',script)
        self.assertNotIn('id="intro-logo"',html)
        self.assertIn('<option value="gif">GIF animado promocional</option>',html)
        self.assertIn("image/gif",html)


if __name__=="__main__":
    unittest.main()
