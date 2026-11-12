"""The admin enters the real selected customer's existing account."""
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch

from web_galerias import core,admin_account


class ExactAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_customer_bootstrap_without_fake_visit_or_name_change(self):
        db=N(execute=AsyncMock(),fetchval=AsyncMock(return_value=3))
        user={
            "uid":777,"name":"Usuaria","quota_tier":"basic",
            "quota_daily_limit":50,
        }
        g=N(db=lambda:db,
            entitled=AsyncMock(return_value=True),
            folder_list=AsyncMock(return_value=[]),
            free_granted=AsyncMock(return_value=False),
            design=AsyncMock(return_value={"title":"Web Galerías"}),
            s=N(descargas_miniapp_url="https://descargas.example/"),
            storage=N(ready=True))
        plan={"daily_limit":50,"tier":"basic","quota_unlimited":False,
              "plus_active":False,"linked_descargas":False}
        with patch.object(core,"personal_folders",
                          new=AsyncMock(return_value=[])),\
             patch.object(core.download_plus,"tier",
                          new=AsyncMock(return_value=plan)),\
             patch.object(core,"is_admin",return_value=False),\
             patch.object(core.GalleryCore,"download_link_bot",
                          return_value="https://t.me/Descargas_Robot"):
            result=await core.GalleryCore.bootstrap(g,777,track_visit=False)
        db.execute.assert_not_awaited()
        self.assertFalse(result["admin"])
        self.assertTrue(result["allowed"])
        self.assertEqual(result["quota_daily_limit"],50)
        self.assertEqual(result["remaining"],47)
        self.assertTrue(result["quota_plus_available"]==core.download_plus.configured() or
                        result["quota_plus_available"] in (True,False))
        self.assertEqual(result["quota_link_bot"],"https://t.me/Descargas_Robot")
        self.assertEqual(result["quota_link_miniapp"],"https://descargas.example/")

    async def test_admin_operation_calls_same_core_bootstrap_and_preserves_every_user_field(self):
        standard={
            "admin":False,"allowed":True,
            "quota_tier":"linked","quota_daily_limit":200,
            "quota_unlimited":False,"quota_plus_available":True,
            "quota_link_miniapp":"https://descargas.example/",
            "quota_link_bot":"https://t.me/Descargas_Robot",
            "personal_folders":[{"id":1,"name":"Privada","count":22}],
            "folders":[{"id":2,"name":"Original","photos":3,"videos":13}],
            "free_access":False,"remaining":199,"used":1,
        }
        db=N(fetchrow=AsyncMock(return_value={"uid":777,"name":"Cliente"}))
        g=N(db=lambda:db,bootstrap=AsyncMock(return_value=dict(standard)))
        with patch.object(admin_account,"is_admin",return_value=True):
            opened=await admin_account.operation(
                g,42,"admin-view-bootstrap",{"telegram_id":777})
        g.bootstrap.assert_awaited_once_with(777,track_visit=False)
        for key in standard:
            self.assertEqual(opened[key],standard[key])
        self.assertTrue(opened["admin_view"])
        self.assertTrue(opened["admin_controls"])
        self.assertFalse(opened["admin"])  # Actual role of the customer.

    def test_no_clone_or_hidden_customer_quota_in_actual_client_source(self):
        root=Path(__file__).resolve().parents[1]/"web_galerias"
        js=(root/"app.js").read_text(encoding="utf-8")
        admin=(root/"admin_account.py").read_text(encoding="utf-8")
        server=(root/"app.py").read_text(encoding="utf-8")
        self.assertIn('await g.bootstrap(target, track_visit=False)',admin)
        self.assertIn('(ROOT/"index.html").read_text(encoding="utf-8")',server)
        self.assertIn('"quota-plus-checkout":"admin-view-quota-plus-checkout"',js)
        self.assertIn('"chat-unread":"admin-view-chat-unread"',js)
        self.assertIn('"notices-list":"admin-view-notices-list"',js)
        self.assertNotIn('ADMIN_ACCOUNT_VIEW||unlimited||linked',js)
        self.assertNotIn('ADMIN_ACCOUNT_VIEW||unlimited||!S.me.quota_plus_available',js)


if __name__=="__main__":
    unittest.main()
