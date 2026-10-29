from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock, patch
from telethon import types, functions
from descargas_app import panel_purchases as p


def photo():
    return types.MessageMediaPhoto(photo=types.Photo(id=4, access_hash=5, file_reference=b'',
        date=datetime.now(timezone.utc), sizes=[types.PhotoSize('m', 100, 100, 100)], dc_id=2))


def transaction(**changes):
    values=dict(id='buy-1', amount=types.StarsAmount(-100, 0), date=datetime.now(timezone.utc),
        peer=types.StarsTransactionPeer(types.PeerChannel(7)), msg_id=12, extended_media=[photo()])
    values.update(changes)
    return types.StarsTransaction(**values)


class PurchasesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entity=types.Channel(id=7, title='Canal', photo=types.ChatPhotoEmpty(),
            date=datetime.now(timezone.utc), access_hash=9, broadcast=True)
        self.client=AsyncMock(return_value=N(history=[transaction()], users=[], chats=[self.entity], next_offset='page2'))
        @asynccontextmanager
        async def account(actor, uid):
            yield self.client, 3
        self.panel=N(s=N(), account=account,
            pack=lambda purpose, **values:json.dumps(dict(purpose=purpose, **values)),
            unpack=self.unpack)
        self.guard=AsyncMock()
        self.account_guard=AsyncMock()
        for name, value in [('require_source', self.guard), ('require_account', self.account_guard), ('require_entity', lambda *args:None)]:
            patcher=patch.object(p, name, value);patcher.start();self.addCleanup(patcher.stop)

    def unpack(self, token, purpose):
        value=json.loads(token)
        if value['purpose'] != purpose:raise ValueError('wrong purpose')
        return value

    async def test_purchased_album_and_original_link(self):
        self.client.return_value.history=[transaction(extended_media=[photo(),photo()])]
        data=await p.listing(self.panel, 11, 22)
        self.assertEqual(len(data['items'][0]['media']), 2)
        self.assertEqual(data['items'][0]['link'], 'https://t.me/c/7/12')
        self.assertTrue(self.client.call_args.args[0].outbound)
        self.assertIsInstance(self.client.call_args.args[0].peer, types.InputPeerSelf)
        self.assertEqual(self.client.call_args.args[0].limit,50)
        self.assertNotIn('amount',data['items'][0])

    async def test_excludes_nonpurchases_refunds_pending_and_failed(self):
        self.client.return_value.history=[transaction(**change) for change in (
            {'refund':True},{'pending':True},{'failed':True},{'extended_media':[]},
            {'amount':types.StarsAmount(100,0)})]
        data=await p.listing(self.panel,11,22)
        self.assertEqual(data['items'],[])
        self.assertTrue(data['next'])

    async def test_protected_source_omitted(self):
        self.guard.side_effect=ValueError('protected')
        self.assertEqual((await p.listing(self.panel,11,22))['items'],[])

    async def test_cursor_scoped_to_actor_account_generation(self):
        data=await p.listing(self.panel,11,22)
        for key,value in [('actor',12),('uid',23),('generation',4)]:
            cursor=json.loads(data['next']);cursor[key]=value
            with self.assertRaises(ValueError):await p.listing(self.panel,11,22,json.dumps(cursor))
        await p.listing(self.panel,11,22,data['next'])
        self.assertEqual(self.client.call_args.args[0].offset,'page2')

    async def test_media_rechecks_transaction_without_needing_original_message(self):
        data=await p.listing(self.panel,11,22)
        result=await p.resolve(self.panel,11,22,3,self.client,self.entity,data['items'][0]['token'],0)
        self.assertIsNotNone(result.photo)
        self.assertIsInstance(self.client.call_args.args[0],functions.payments.GetStarsTransactionsByIDRequest)
        self.assertEqual(self.client.call_args.args[0].id[0].id,'buy-1')
        self.account_guard.assert_awaited()

    async def test_rejects_cross_account_and_source_tokens(self):
        data=await p.listing(self.panel,11,22);token=data['items'][0]['token']
        for actor,uid,generation in [(12,22,3),(11,23,3),(11,22,4)]:
            with self.assertRaises(ValueError):await p.resolve(self.panel,actor,uid,generation,self.client,self.entity,token,0)
        other=types.User(id=9,first_name='Other')
        with self.assertRaises(ValueError):await p.resolve(self.panel,11,22,3,self.client,other,token,0)

    async def test_rejects_refunded_missing_and_invalid_index_on_view(self):
        data=await p.listing(self.panel,11,22);token=data['items'][0]['token']
        for history,index in [([transaction(refund=True)],0),([],0),([transaction()],-1),([transaction()],1)]:
            self.client.return_value.history=history
            with self.assertRaises(ValueError):await p.resolve(self.panel,11,22,3,self.client,self.entity,token,index)

    async def test_no_preview_or_temporary_media(self):
        self.assertIsNone(p.media_message(types.MessageMediaPaidMedia(stars_amount=1,extended_media=[])))
        self.assertIsNone(p.media_message(types.MessageMediaPhoto(photo=photo().photo,ttl_seconds=10)))
        self.assertIsNone(p.message_link(types.User(id=1),12))

    async def test_rechecks_account_after_listing(self):
        self.account_guard.side_effect=ValueError('revoked')
        with self.assertRaises(ValueError):await p.listing(self.panel,11,22)


if __name__ == '__main__':unittest.main()
