"""Paid-media reconciliation is account-bound and never unlocks Telegram previews."""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from telethon import functions, types
from descargas_app.paid_media import purchased_items, reveal_purchased


def photo():
    return types.MessageMediaPhoto(photo=types.Photo(
        id=41, access_hash=42, file_reference=b'ref',
        date=datetime.now(timezone.utc),
        sizes=[types.PhotoSize('m', 64, 64, 512)], dc_id=2))


def post(preview=True):
    media=(types.MessageExtendedMediaPreview(w=64, h=64) if preview
           else types.MessageExtendedMedia(media=photo()))
    return types.Message(id=77, peer_id=types.PeerChannel(7),
        date=datetime.now(timezone.utc), message='Álbum',
        media=types.MessageMediaPaidMedia(stars_amount=10,
            extended_media=[media]))


class PaidReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entity=types.Channel(id=7,title='Canal',photo=types.ChatPhotoEmpty(),
            date=datetime.now(timezone.utc),access_hash=55,broadcast=True)

    async def test_refresh_returns_only_verified_own_paid_media(self):
        m=post()
        update=types.UpdateMessageExtendedMedia(peer=types.PeerChannel(7),msg_id=77,
            extended_media=[types.MessageExtendedMedia(media=photo()),
                            types.MessageExtendedMedia(media=photo())])
        client=AsyncMock(return_value=SimpleNamespace(updates=[update]))
        await reveal_purchased(client,self.entity,[m],{},scope=(22,3))
        self.assertIsInstance(client.call_args.args[0],functions.messages.GetExtendedMediaRequest)
        self.assertEqual(client.call_args.args[0].id,[77])
        self.assertEqual(len(purchased_items(m)),2)
        self.assertIsNotNone(purchased_items(m)[0].photo)

    async def test_unpaid_preview_is_never_exposed_and_negative_lookups_are_bounded(self):
        m=post()
        client=AsyncMock(return_value=SimpleNamespace(updates=[]))
        cache={}
        await reveal_purchased(client,self.entity,[m],cache,scope=(22,3))
        await reveal_purchased(client,self.entity,[m],cache,scope=(22,3))
        client.assert_awaited_once()
        with self.assertRaisesRegex(ValueError,'todavía no tiene acceso'):
            purchased_items(m)

    async def test_wrong_channel_update_cannot_override_paid_preview(self):
        m=post()
        spoofed=types.UpdateMessageExtendedMedia(peer=types.PeerChannel(99),msg_id=77,
            extended_media=[types.MessageExtendedMedia(media=photo())])
        client=AsyncMock(return_value=SimpleNamespace(updates=[spoofed]))
        await reveal_purchased(client,self.entity,[m])
        with self.assertRaises(ValueError):purchased_items(m)

    async def test_already_unlocked_media_needs_no_rpc(self):
        m=post(preview=False)
        client=AsyncMock()
        await reveal_purchased(client,self.entity,[m])
        client.assert_not_awaited()
        self.assertIsNotNone(purchased_items(m)[0].photo)


if __name__=='__main__':unittest.main()
