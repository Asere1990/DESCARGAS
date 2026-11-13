"""Favorites tests use only a local isolated Postgres; never Telegram or production."""
import json
import os
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from urllib.parse import urlparse

from cryptography.fernet import Fernet

from descargas_app.pg_store import Repository
from descargas_app import panel_favorites as fav

URL=os.environ.get('TEST_DATABASE_URL','')


@unittest.skipUnless(URL,'Local isolated PostgreSQL test database required')
class FavoritesDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed=urlparse(URL)
        if parsed.hostname not in ('localhost','127.0.0.1') or parsed.path!='/descargas_test':
            raise RuntimeError('Favorite tests refuse production databases')
        self.repo=Repository(URL,Fernet(Fernet.generate_key()))
        await self.repo.start()
        await self.repo.pool.execute('TRUNCATE descarga_panel_favorites, descarga_panel_favorite_accounts')
        await self.repo.save_account(22,'dummy-session',{'name':'Cuenta de prueba'})
        row=await self.repo.account(22)
        self.generation=row['generation']
        self.p=N(s=N(repo=self.repo,bot_id=7),
                 unpack=lambda token,purpose:json.loads(token),
                 pack=lambda purpose,**data:json.dumps({'purpose':purpose,**data}))
        self.favorite=fav.PanelFavorites(self.p)
        self.guard=patch.object(fav,'authorized',lambda actor:actor in (111,222))
        self.account_guard=patch.object(fav,'require_account',new_callable=AsyncMock)
        self.source_guard=patch.object(fav,'require_source',new_callable=AsyncMock)
        for guard in (self.guard,self.account_guard,self.source_guard):
            guard.start()
            self.addCleanup(guard.stop)

    async def asyncTearDown(self):
        await self.repo.pool.execute('TRUNCATE descarga_panel_favorites, descarga_panel_favorite_accounts')
        await self.repo.close()

    def request(self,kind='group'):
        return {'account':22,'peer':json.dumps({
            'purpose':'peer','actor':111,'uid':22,'generation':self.generation,
            'kind':'channel','id':1234,'hash':4567}),
            'type':kind,'name':'Mi grupo Favorito','account_name':'Cuenta de prueba',
            'username':'grupo_prueba','favorite':True}

    async def test_bookmarks_are_encrypted_and_do_not_change_connected_sessions(self):
        before=(await self.repo.account(22))['session']
        self.assertEqual((await self.favorite.toggle(111,self.request()))['favorite'],True)
        result=await self.favorite.listing(111)
        self.assertEqual(len(result['items']),1)
        item=result['items'][0]
        self.assertEqual(item['account_id'],'22')
        self.assertEqual(item['id'],'-1000000001234')
        self.assertEqual(item['type'],'group')
        self.assertTrue(item['available'])
        self.assertIn('peer',item['handle'])
        blob=await self.repo.pool.fetchval('SELECT payload FROM descarga_panel_favorites')
        self.assertNotIn(b'Mi grupo Favorito',bytes(blob))
        self.assertEqual((await self.repo.account(22))['session'],before)
        await self.favorite.remove(111,22,-1000000001234)
        self.assertEqual((await self.favorite.listing(111))['items'],[])
        self.assertEqual((await self.repo.account(22))['session'],before)

    async def test_bookmarks_are_private_to_each_administrator(self):
        await self.favorite.toggle(111,self.request())
        self.assertEqual((await self.favorite.listing(222))['items'],[])
        self.assertEqual(len((await self.favorite.listing(111))['items']),1)

    async def test_reconnected_account_has_to_refresh_chat_before_open(self):
        await self.favorite.toggle(111,self.request())
        # The real disconnect/reconnect flow invalidates the old generation.
        await self.repo.cancel_user(22)
        await self.repo.erase_session(22)
        await self.repo.save_account(22,'another-session',{'name':'Nueva sesión'})
        item=(await self.favorite.listing(111))['items'][0]
        self.assertFalse(item['available'])
        self.assertIsNone(item['handle'])
        # An old bookmark can always be removed without opening the account.
        await self.favorite.remove(111,22,-1000000001234)
        self.assertEqual((await self.favorite.listing(111))['items'],[])

    async def test_account_favorite_is_private_encrypted_and_never_alters_session(self):
        before=(await self.repo.account(22))['session']
        data={'account':22,'favorite':True,'name':'Cuenta principal','username':'cuenta'}
        self.assertEqual((await self.favorite.toggle_account(111,data)),{'favorite':True})
        self.assertEqual((await self.favorite.account_listing(222)),[])
        account=(await self.favorite.account_listing(111))[0]
        self.assertEqual(account['id'],'22')
        self.assertEqual(account['name'],'Cuenta principal')
        self.assertTrue(account['available'])
        result=await self.favorite.listing(111)
        self.assertEqual(result['items'],[],'Account favorites never become chat bookmarks')
        self.assertEqual(len(result['accounts']),1)
        payload=await self.repo.pool.fetchval(
            'SELECT payload FROM descarga_panel_favorite_accounts WHERE bot_id=7 AND actor=111')
        self.assertNotIn(b'Cuenta principal',bytes(payload))
        self.assertEqual((await self.repo.account(22))['session'],before)
        await self.repo.erase_session(22)
        stale=(await self.favorite.account_listing(111))[0]
        self.assertFalse(stale['available'])
        self.assertEqual(stale['id'],'22')
        self.assertEqual((await self.favorite.toggle_account(111,{
            'account':22,'favorite':False})),{'favorite':False})
        self.assertEqual(await self.favorite.account_listing(111),[])

    async def test_account_bookmark_requires_valid_actor_account_and_boolean(self):
        with self.assertRaises(Exception):
            await self.favorite.toggle_account(111,{'account':22,'favorite':'yes'})
        with self.assertRaises(ValueError):
            await self.favorite.toggle_account(111,{'account':99999999,
                'favorite':True,'name':'No existe'})
        with self.assertRaises(Exception):
            await self.favorite.toggle_account(333,{'account':22,'favorite':True})
        self.assertEqual(await self.favorite.account_listing(111),[])

    async def test_invalid_peer_scope_does_not_write(self):
        payload=self.request()
        fake=json.loads(payload['peer'])
        fake['actor']=222
        payload['peer']=json.dumps(fake)
        with self.assertRaises(Exception):
            await self.favorite.toggle(111,payload)
        self.assertEqual((await self.favorite.listing(111))['items'],[])
