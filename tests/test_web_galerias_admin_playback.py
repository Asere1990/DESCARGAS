"""User-account admin playback refresh and original-gallery authorization."""
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch

from aiohttp import web
from web_galerias import admin_account


class AdminPlaybackTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, media_state="ready", file_key="folders/7/5/original.mp4"):
        records=[
            {"uid":987654321,"name":"Cliente"},
            {"id":55,"folder":7,"kind":"video","mime":"video/mp4",
             "state":media_state,"file_key":file_key,
             "thumb_key":"folders/7/5/thumb.jpg","active":True}
        ]
        db=N(fetchrow=AsyncMock(side_effect=records))
        gallery=N(db=lambda:db,
                 entitled=AsyncMock(return_value=True),
                 permitted=AsyncMock(return_value=True))
        return gallery,db

    async def test_stale_preview_becomes_playable_with_signed_admin_link(self):
        gallery,db=self._client()
        with patch.object(admin_account,"is_admin",return_value=True), \
             patch.object(admin_account,"temporary_link",
                 side_effect=lambda g,actor,media,kind:
                     f"admin={actor}&media={media}&kind={kind}"):
            data=await admin_account.operation(
                gallery,111,"admin-view-media-status",
                {"telegram_id":987654321,"media":55})
        self.assertTrue(data["available"])
        self.assertEqual(data["kind"],"video")
        self.assertEqual(data["mime"],"video/mp4")
        self.assertIn("admin=111",data["view"])
        self.assertIn("media=55",data["view"])
        self.assertNotIn("admin=987654321",data["view"])
        gallery.permitted.assert_awaited_once_with(111,7)
        gallery.entitled.assert_awaited_once_with(987654321)

    async def test_when_original_still_pending_never_claim_it_is_playable(self):
        gallery,db=self._client(media_state="preview",file_key=None)
        with patch.object(admin_account,"is_admin",return_value=True), \
             patch.object(admin_account,"temporary_link",
                side_effect=lambda g,actor,media,kind:"admin-token"):
            data=await admin_account.operation(
                gallery,111,"admin-view-media-status",
                {"telegram_id":987654321,"media":55})
        self.assertFalse(data["available"])
        self.assertIsNone(data["view"])
        self.assertIsNotNone(data["thumb"])

    async def test_pending_if_ready_row_does_not_have_real_storage_key(self):
        gallery,_=self._client(media_state="ready",file_key=None)
        with patch.object(admin_account,"is_admin",return_value=True), \
             patch.object(admin_account,"temporary_link",return_value="token"):
            data=await admin_account.operation(
                gallery,111,"admin-view-media-status",
                {"telegram_id":987654321,"media":55})
        self.assertFalse(data["available"])
        self.assertIsNone(data["view"])

    async def test_user_cannot_check_another_user_media(self):
        gallery,_=self._client()
        with patch.object(admin_account,"is_admin",return_value=False):
            with self.assertRaises(web.HTTPForbidden):
                await admin_account.operation(
                    gallery,222,"admin-view-media-status",
                    {"telegram_id":987654321,"media":55})

    async def test_actor_must_still_be_allowed_access_to_file(self):
        gallery,_=self._client()
        gallery.permitted.return_value=False
        with patch.object(admin_account,"is_admin",return_value=True):
            with self.assertRaises(web.HTTPForbidden):
                await admin_account.operation(
                    gallery,111,"admin-view-media-status",
                    {"telegram_id":987654321,"media":55})

    async def test_id_must_exist_and_cannot_probe_missing_file(self):
        gallery,db=self._client()
        db.fetchrow.side_effect=[{"uid":987654321,"name":"Cliente"},None]
        with patch.object(admin_account,"is_admin",return_value=True):
            with self.assertRaises(web.HTTPNotFound):
                await admin_account.operation(
                    gallery,111,"admin-view-media-status",
                    {"telegram_id":987654321,"media":55})


if __name__=="__main__":
    unittest.main()
