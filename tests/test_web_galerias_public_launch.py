"""Public launch, reversible free-access and single Web promo tests."""
import json
import os
import time
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch

from aiohttp import web
from web_galerias import core,moderation,intro_video


class PublicGalleryLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_launch_is_not_free_access_for_any_telegram_id(self):
        db=N(fetchval=AsyncMock(return_value=None),
             fetchrow=AsyncMock(return_value=None))
        g=N(db=lambda:db,free_granted=AsyncMock(return_value=False))
        with patch.dict(os.environ,{"GALERIAS_PUBLIC":"1"}),patch.object(
                core,"is_admin",return_value=False):
            self.assertFalse(await core.GalleryCore.entitled(g,733332211))
        self.assertIn("wg_subscriptions",db.fetchrow.await_args.args[0])
        db.execute=AsyncMock()
        g.entitled=AsyncMock(return_value=False)
        g.design=AsyncMock(return_value={})
        g.storage=N(ready=True)
        with patch.dict(os.environ,{"GALERIAS_PUBLIC":"1"}):
            response=await core.GalleryCore.bootstrap(g,733332211)
        self.assertTrue(response["checkout_enabled"])
        self.assertFalse(response["allowed"])
        self.assertFalse(response["free_access"])

    async def test_admin_reversible_grants_do_not_modify_stripe_or_telegram(self):
        db=N(execute=AsyncMock(),fetchval=AsyncMock(return_value=None))
        g=N(db=lambda:db,log=AsyncMock(),free_granted=AsyncMock(return_value=True))
        with patch.object(moderation,"is_admin",side_effect=lambda uid:uid==123):
            denied=moderation.operation(g,555,"admin-free-access-set",{
                "telegram_id":99999999,"enabled":True,
                "confirm":"CAMBIAR_ACCESO_GRATIS"})
            with self.assertRaises(web.HTTPForbidden):
                await denied
            grant=await moderation.operation(g,123,"admin-free-access-set",{
                "telegram_id":99999999,"enabled":True,
                "confirm":"CAMBIAR_ACCESO_GRATIS"})
            revoke=await moderation.operation(g,123,"admin-free-access-set",{
                "telegram_id":99999999,"enabled":False,
                "confirm":"CAMBIAR_ACCESO_GRATIS"})
            status=await moderation.operation(g,123,"admin-free-access-status",{
                "telegram_id":99999999})
        self.assertTrue(grant["free_access"])
        self.assertFalse(revoke["free_access"])
        self.assertTrue(status["free_access"])
        sql="\n".join(call.args[0] for call in db.execute.await_args_list)
        self.assertIn("wg_free_access",sql)
        self.assertNotIn("wg_subscriptions",sql)
        self.assertNotIn("descarga_accounts",sql)
        self.assertNotIn("wg_media",sql)
        self.assertEqual(g.log.await_count,2)

    async def test_explicit_grant_overrides_legacy_file_for_reversible_removal(self):
        db=N(fetchval=AsyncMock(side_effect=[False,True,None]))
        g=N(db=lambda:db)
        with patch.object(core,"free_access",return_value=True):
            self.assertFalse(await core.GalleryCore.free_granted(g,777))
            self.assertTrue(await core.GalleryCore.free_granted(g,777))
            self.assertTrue(await core.GalleryCore.free_granted(g,777))

    async def test_promo_media_is_exactly_one_of_photo_or_video(self):
        db=N(fetchval=AsyncMock(side_effect=[None,{
            "key":"web-galerias/web-promo/123.mp4",
            "mime":"video/mp4","size":900000,"updated":time.time()
        }]))
        g=N(db=lambda:db)
        photo=await intro_video.public_promo_status(g,N())
        video=await intro_video.public_promo_status(g,N())
        self.assertEqual(json.loads(photo.body),{"kind":"photo"})
        self.assertEqual(json.loads(video.body),{"kind":"video"})
        self.assertEqual(photo.headers["Cache-Control"],"no-store")
        self.assertLess(0,intro_video.MAX_WEB_PROMO_OUTPUT)
        self.assertLess(intro_video.MAX_WEB_PROMO_OUTPUT,20*1024*1024)

    def test_launch_and_promo_assets_keep_payment_flow_separate(self):
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]/"web_galerias"
        app=(root/"app.py").read_text()
        js=(root/"app.js").read_text()
        html=(root/"index.html").read_text()
        video=(root/"intro_video.py").read_text()
        self.assertIn('"/web-galerias/promo.json"',app)
        self.assertIn('X-Intro-Kind',js)
        self.assertIn('id="upload-web-promo"',html)
        self.assertIn('kind=="web-promo"',video)
        self.assertIn('"web_promo" if kind=="web-promo" else "intro_promo"',video)
        self.assertIn('id="admin-free-grant"',html)
        self.assertIn('id="admin-free-revoke"',html)
        self.assertIn('admin-free-access-set', (root/"admin_tools.js").read_text())
        self.assertIn("GALERIAS_PUBLIC", (root/"payments.py").read_text())


if __name__=="__main__":
    unittest.main()
