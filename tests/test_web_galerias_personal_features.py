"""Secure user-owned favorites and resume location regression checks."""
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch

from aiohttp import web
from web_galerias import user_features,location


class PersonalFeaturesTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_has_per_user_keys_cascade_and_rls(self):
        sql=user_features.SCHEMA
        for name in ("wg_favorites","wg_view_progress","PRIMARY KEY(uid,media)",
                     "PRIMARY KEY(uid,folder)","ON DELETE CASCADE",
                     "ENABLE ROW LEVEL SECURITY","REVOKE ALL"):
            self.assertIn(name,sql)

    async def test_grant_is_not_required_to_unfavorite_own_item(self):
        db=N(execute=AsyncMock(),fetchrow=AsyncMock())
        g=N(db=lambda:db,permitted=AsyncMock(return_value=False))
        result=await user_features.set_favorite(g,808,
                                                 {"media":1234,"favorite":False})
        self.assertEqual(result,{"ok":True,"favorite":False,"media":1234})
        self.assertIn("WHERE uid=$1 AND media=$2",db.execute.await_args.args[0])
        self.assertEqual(db.execute.await_args.args[1:],(808,1234))
        db.fetchrow.assert_not_awaited()
        g.permitted.assert_not_awaited()

    async def test_favorite_requires_live_permission_and_visible_source(self):
        db=N(execute=AsyncMock(),fetchrow=AsyncMock(return_value={"folder":10}))
        g=N(db=lambda:db,permitted=AsyncMock(return_value=False))
        with self.assertRaises(web.HTTPForbidden):
            await user_features.set_favorite(g,808,{"media":1234,"favorite":True})
        db.execute.assert_not_awaited()
        g.permitted.assert_awaited_once_with(808,10)
        g.permitted.return_value=True
        result=await user_features.set_favorite(g,808,
                                                 {"media":1234,"favorite":True})
        self.assertTrue(result["favorite"])
        self.assertIn("ON CONFLICT(uid,media) DO NOTHING",db.execute.await_args.args[0])
        self.assertEqual(db.execute.await_args.args[1:3],(808,1234))
        with self.assertRaises(web.HTTPBadRequest):
            await user_features.set_favorite(g,808,{"media":1234,"favorite":"yes"})

    async def test_progress_saves_only_for_permitted_media_in_source_folder(self):
        db=N(execute=AsyncMock(),fetchrow=AsyncMock(return_value={"folder":10}))
        g=N(db=lambda:db,permitted=AsyncMock(return_value=True))
        data=await user_features.save_progress(g,808,{"media":7345})
        self.assertEqual(data["folder"],10)
        self.assertEqual(data["media"],7345)
        self.assertIn("ON CONFLICT(uid,folder)",db.execute.await_args.args[0])
        self.assertEqual(db.execute.await_args.args[1:4],(808,10,7345))
        g.permitted.return_value=False
        with self.assertRaises(web.HTTPForbidden):
            await user_features.save_progress(g,808,{"media":7346})
        self.assertEqual(db.execute.await_count,1)

    async def test_resumes_and_favorite_totals_filter_hidden_or_removed_galleries(self):
        db=N(fetch=AsyncMock(return_value=[{
            "folder":10,"media":1234,"name":"Galería original","updated":123.0
        }]),fetchval=AsyncMock(return_value=8))
        g=N(db=lambda:db)
        with patch("web_galerias.permissions.is_admin",return_value=False):
            data=await user_features.collections(g,808)
        self.assertEqual(data["favorites_count"],8)
        self.assertEqual(data["resume"][0]["media"],1234)
        query=db.fetch.await_args.args[0]
        self.assertIn("p.mode",query)
        self.assertIn("f.active",query)
        self.assertIn("m.state IN ('preview','ready')",query)
        self.assertIn("r.uid=$1",query)
        self.assertEqual(db.fetch.await_args.args[1:],(808,False))
        self.assertEqual(db.fetchval.await_args.args[1:],(808,False))

    async def test_favorites_list_only_for_current_user_and_respects_pagination(self):
        row={"id":400,"folder":10,"kind":"photo","mime":"image/jpeg",
             "filename":"x.jpg","date":12.0,"width":5,"height":6,
             "size":1024,"state":"ready","thumb_key":"thumb"}
        db=N(fetch=AsyncMock(return_value=[row]))
        g=N(db=lambda:db)
        with patch("web_galerias.permissions.is_admin",return_value=False),patch(
             "web_galerias.downloads.temporary_link",return_value="signed"):
            data=await user_features.items(g,808,
                {"cursor":0,"kind":"all","query":""})
        self.assertEqual(len(data["items"]),1)
        media=data["items"][0]
        self.assertTrue(media["favorite"])
        self.assertEqual(media["folder"],10)
        self.assertEqual(media["thumb"],
                         "/web-galerias/media/400/thumb?t=signed")
        self.assertEqual(data["next"],None)
        sql=db.fetch.await_args.args[0]
        self.assertIn("fav.uid=$1",sql)
        self.assertIn("wg_user_folders",sql)
        self.assertIn("LIMIT 61",sql)
        self.assertEqual(db.fetch.await_args.args[1:],(808,False,"all","",2**63-1))

    async def test_inaccessible_resume_cannot_skip_existing_locate_security(self):
        db=N(fetchrow=AsyncMock(return_value={
            "id":1234,"folder":20,"name":"Reservado","active":True
        }),fetch=AsyncMock())
        g=N(db=lambda:db,permitted=AsyncMock(return_value=False))
        with self.assertRaises(web.HTTPForbidden):
            await location.locate(g,808,{"media":1234})
        db.fetch.assert_not_awaited()

    def test_frontend_has_continue_and_heart_without_changing_download_limits(self):
        root=Path(__file__).resolve().parents[1]/"web_galerias"
        markup=(root/"index.html").read_text(encoding="utf-8")
        js=(root/"app.js").read_text(encoding="utf-8")
        style=(root/"style.css").read_text(encoding="utf-8")
        for name in ("more-content","open-favorites",
                     "viewer-favorite","favorites-counter","close-viewer"):
            self.assertIn('id="'+name+'"',markup)
        self.assertIn('api("user-collections")',js)
        self.assertIn('api("progress-save",{media})',js)
        self.assertIn('api("favorite-set",{media:item.id,favorite:next})',js)
        self.assertIn('api("locate-media",{media})',js)
        self.assertIn('S.folder.favorites?"favorites-items":"items"',js)
        self.assertIn('if(id==="library"&&S.me?.allowed)',js)
        self.assertIn('S.me?.admin?100:10',js)
        self.assertIn('.favorite-mark',style)
        self.assertNotIn('id="resume-feature"',markup)
        self.assertIn('className="folder-continue"',js)
        self.assertIn('function backToViewedMedia()',js)
        self.assertIn('api("offers-link")',js)
        self.assertIn('#library .more-content-entry{',style)


if __name__=="__main__":
    unittest.main()
