"""Administrator views the actual customer Web Galerías using verified own identity."""
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch
from aiohttp import web
from web_galerias import admin_account


class AdminAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_user_cannot_read_or_edit_another_account(self):
        g=N(db=lambda:None)
        with patch.object(admin_account,"is_admin",return_value=False):
            for op in ("admin-view-bootstrap","admin-view-collections",
                       "admin-view-items","admin-view-favorites",
                       "admin-view-favorite-set","admin-view-personal-delete"):
                with self.subTest(op=op),self.assertRaises(web.HTTPForbidden):
                    await admin_account.operation(g,55,op,{"telegram_id":777})

    async def test_gallery_items_are_exact_user_scope_and_user_signed_media(self):
        db=N(fetchrow=AsyncMock(return_value={"uid":777,"name":"Cliente"}))
        g=N(db=lambda:db,entitled=AsyncMock(return_value=True),
            list_items=AsyncMock(return_value={"items":[],"next":None}))
        with patch.object(admin_account,"is_admin",return_value=True):
            with patch.object(admin_account,"temporary_link",
                 side_effect=lambda _,uid,item,kind:f"{uid}-{item}-{kind}") as token:
                await admin_account.operation(g,42,"admin-view-items",
                    {"telegram_id":777,"folder":17,"kind":"all","cursor":0})
                signer=g.list_items.await_args.args[4]
                self.assertEqual(signer(777,55,"thumb"),"777-55-thumb")
        self.assertEqual(g.list_items.await_args.args[0:2],(777,17))
        g.entitled.assert_awaited_once_with(777)

    async def test_admin_bootstrap_is_same_as_real_user_including_200_plus_and_account_data(self):
        customer={
            "admin": False, "allowed": True, "quota_tier": "linked",
            "quota_daily_limit": 200, "quota_unlimited": False,
            "quota_plus_available": True,
            "quota_link_bot": "https://t.me/Descargas_Robot",
            "quota_link_miniapp": "https://descargas.example/",
            "remaining": 183, "used": 17,
            "folders": [{"id": 4, "name": "Galería", "photos": 20,"videos": 10}],
            "personal_folders": [{"id": 6, "name": "Fotos", "count": 22}],
            "design": {"title": "Web Galerías"},
            "free_access": False,
        }
        db=N(fetchrow=AsyncMock(return_value={"uid":777,"name":"Cliente"}),
             execute=AsyncMock())
        g=N(db=lambda:db,bootstrap=AsyncMock(return_value=dict(customer)))
        with patch.object(admin_account,"is_admin",return_value=True):
            data=await admin_account.operation(
                g,42,"admin-view-bootstrap",{"telegram_id":777})
        g.bootstrap.assert_awaited_once_with(777,track_visit=False)
        for key,value in customer.items():
            self.assertEqual(data[key],value,key)
        self.assertFalse(data["admin"])  # User role, not a fake admin profile.
        self.assertTrue(data["admin_view"])
        self.assertTrue(data["admin_controls"])
        self.assertEqual(data["owner_telegram_id"],777)
        db.execute.assert_not_awaited()  # Do not count admin as user activity.

    async def test_personal_actions_explicitly_use_target_and_audit_real_admin(self):
        db=N(fetchrow=AsyncMock(return_value={"uid":777,"name":"Cliente"}))
        g=N(db=lambda:db,entitled=AsyncMock(return_value=True),log=AsyncMock())
        with patch.object(admin_account,"is_admin",return_value=True),\
             patch.object(admin_account.personal,"operation",
                 new=AsyncMock(return_value={"ok":True})) as operation:
            result=await admin_account.operation(g,42,
                "admin-view-personal-rename",{"telegram_id":777,
                   "personal_folder":12,"name":"Fotos nuevas"})
        self.assertTrue(result["ok"])
        self.assertEqual(operation.await_args.args[1:3],(777,"personal-rename"))
        self.assertEqual(g.log.await_args.args[0],42)

    async def test_target_without_access_is_not_silently_impersonated(self):
        db=N(fetchrow=AsyncMock(return_value={"uid":777,"name":"Cliente"}))
        g=N(db=lambda:db,entitled=AsyncMock(return_value=False))
        with patch.object(admin_account,"is_admin",return_value=True):
            with self.assertRaises(web.HTTPForbidden):
                await admin_account.operation(g,42,"admin-view-items",
                    {"telegram_id":777,"folder":1})

    async def test_favorite_changes_use_selected_user_not_viewing_admin(self):
        db=N(fetchrow=AsyncMock(return_value={"uid":777,"name":"Cliente"}))
        g=N(db=lambda:db,entitled=AsyncMock(return_value=True),log=AsyncMock())
        with patch.object(admin_account,"is_admin",return_value=True),\
             patch.object(admin_account.user_features,"set_favorite",
                 new=AsyncMock(return_value={"ok":True})) as write:
            await admin_account.operation(g,42,"admin-view-favorite-set",
                {"telegram_id":777,"media":55,"favorite":True})
        self.assertEqual(write.await_args.args[1],777)
        self.assertEqual(g.log.await_args.args[0],42)


if __name__=="__main__":
    unittest.main()
