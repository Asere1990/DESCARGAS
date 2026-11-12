"""Administrative user mirror: permission isolation and read-only download history."""
import unittest
from datetime import date
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch

from aiohttp import web
from web_galerias import admin_mirror


class AdminMirrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_mirror_never_allows_a_regular_user_to_select_another_id(self):
        g=N(db=lambda:None)
        with patch.object(admin_mirror,"is_admin",return_value=False):
            for op in ("admin-mirror-overview","admin-mirror-folders",
                       "admin-mirror-folder-items","admin-mirror-favorites",
                       "admin-mirror-download-dates","admin-mirror-download-items"):
                with self.subTest(op=op), self.assertRaises(web.HTTPForbidden):
                    await admin_mirror.operation(
                        g,123,op,{"telegram_id":777})

    async def test_plan_and_quota_are_the_target_users_not_the_admins(self):
        db=N(
          fetchrow=AsyncMock(side_effect=[
              {"uid":777,"name":"Ejemplo"},
              {"files":10,"batches":3}]),
          fetchval=AsyncMock(side_effect=[17,5,9,2]))
        g=N(db=lambda:db)
        with patch.object(admin_mirror,"is_admin",return_value=True):
            with patch.object(admin_mirror.download_plus,"tier",
                    new=AsyncMock(return_value={
                        "tier":"linked","daily_limit":200,
                        "quota_unlimited":False})):
                result=await admin_mirror.operation(
                    g,42,"admin-mirror-overview",{"telegram_id":777})
        self.assertEqual(result["tier"],"linked")
        self.assertEqual(result["used"],17)
        self.assertEqual(result["remaining"],183)
        self.assertEqual(result["today_requested"],5)
        self.assertEqual(result["total_requested"],10)
        self.assertEqual(result["favorites"],9)
        self.assertEqual(result["folders"],2)
        self.assertEqual(db.fetchval.await_args_list[0].args[1],777)

    async def test_folder_previews_are_re_signed_for_the_admin_not_the_owner(self):
        db=N(fetchrow=AsyncMock(return_value={"uid":777,"name":"Ejemplo"}))
        g=N(db=lambda:db)
        folders=[
          {"id":15,"name":"Fotos","count":22,
           "cover":"/web-galerias/personal-cover/15?t=PRIVATE-OWNER-TOKEN"},
          {"id":16,"name":"Videos","count":2,
           "cover":"/web-galerias/media/505/thumb?t=PRIVATE-OWNER-TOKEN"}]
        with patch.object(admin_mirror,"is_admin",return_value=True):
            with patch.object(admin_mirror.personal,"folders",
                    new=AsyncMock(return_value=folders)) as actual:
                with patch.object(admin_mirror,"temporary_link",
                        side_effect=lambda g,u,i,k:f"ADMIN_{u}_{i}_{k}"):
                    res=await admin_mirror.operation(
                      g,42,"admin-mirror-folders",{"telegram_id":777})
        actual.assert_awaited_once_with(g,777)
        self.assertIn("ADMIN_42_15_admin-personal-cover",res["folders"][0]["cover"])
        self.assertIn("ADMIN_42_505_thumb",res["folders"][1]["cover"])
        self.assertNotIn("PRIVATE-OWNER-TOKEN",str(res))

    async def test_favorites_remain_user_scoped_but_use_admin_media_tokens(self):
        db=N(fetchrow=AsyncMock(return_value={"uid":777,"name":"Ejemplo"}))
        g=N(db=lambda:db)
        favorite={"id":9,"kind":"photo",
                  "thumb":"/web-galerias/media/9/thumb?t=USER",
                  "view":"/web-galerias/media/9/view?t=USER"}
        with patch.object(admin_mirror,"is_admin",return_value=True):
            with patch.object(admin_mirror.user_features,"items",
                 new=AsyncMock(return_value={"items":[favorite],"next":None})) as actual:
                with patch.object(admin_mirror,"temporary_link",
                    side_effect=lambda g,u,i,k:f"ADMIN_{u}_{i}_{k}"):
                    result=await admin_mirror.operation(
                        g,42,"admin-mirror-favorites",
                        {"telegram_id":777,"kind":"all","query":""})
        self.assertEqual(actual.await_args.args[1],777)
        self.assertIn("ADMIN_42_9_thumb",result["items"][0]["thumb"])
        self.assertIn("ADMIN_42_9_view",result["items"][0]["view"])
        self.assertNotIn("t=USER",str(result))

    async def test_date_groups_and_day_items_preserve_history_without_bearer_tokens(self):
        day=date(2026,11,12)
        media={"id":505,"kind":"video","filename":"video.mp4","state":"ready",
               "thumb_key":"thumbnail","file_key":"media","expires":1800000300.0}
        db=N(fetchrow=AsyncMock(return_value={"uid":777,"name":"Ejemplo"}),
             fetch=AsyncMock(side_effect=[
                 [{"day":day,"requests":2,"files":4}],[media]]))
        g=N(db=lambda:db)
        with patch.object(admin_mirror,"is_admin",return_value=True):
            with patch.object(admin_mirror,"temporary_link",
                 side_effect=lambda g,u,i,k:f"ADMIN_{u}_{i}_{k}"):
                dates=await admin_mirror.operation(g,42,
                    "admin-mirror-download-dates",{"telegram_id":777})
                items=await admin_mirror.operation(g,42,
                    "admin-mirror-download-items",
                    {"telegram_id":777,"date":"2026-11-12","offset":0})
        self.assertEqual(dates["dates"],[
            {"date":"2026-11-12","files":4,"requests":2}])
        self.assertEqual(items["items"][0]["requested_at"],1800000000)
        self.assertIn("ADMIN_42_505_view",items["items"][0]["view"])
        self.assertNotIn("token",str(items))
        sqls=[call.args[0] for call in db.fetch.await_args_list]
        self.assertIn("GROUP BY day",sqls[0])
        self.assertIn("UNNEST(t.ids)",sqls[1])
        self.assertIn("LIMIT 41 OFFSET",sqls[1])
        self.assertNotIn("t.token AS",sqls[1])

    def test_invalid_calendar_dates_and_offsets_are_rejected(self):
        for value in ("2026-02-30","2026/11/12","yesterday",["2026-11-12"]):
            with self.subTest(value=value), self.assertRaises(web.HTTPBadRequest):
                admin_mirror._date(value)
        self.assertEqual(admin_mirror._date("2026-11-12"),date(2026,11,12))


if __name__=="__main__":
    unittest.main()
