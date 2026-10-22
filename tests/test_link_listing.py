import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from telethon import types,errors
from descargas_app import link_listing as l
import test_group_download as helpers


def message(mid,kind=None,gid=None):
    return NS(id=mid,media=None,grouped_id=gid,photo=kind=='photo',gif=kind=='gif',sticker=kind=='sticker',document=NS(mime_type=kind) if kind and '/' in kind else None)


class LinkListingTests(unittest.IsolatedAsyncioTestCase):
    def setup_service(self,pages):
        s=helpers.GroupExportTests().service()
        s.repo.pool.fetchval.side_effect=None;s.repo.pool.fetchval.return_value=0
        s.resolve.return_value=types.Channel(id=123,title='Grupo',photo=types.ChatPhotoEmpty(),date=None)
        pages=iter(pages)
        async def history(*args,**kwargs):
            for m in next(pages):yield m
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),iter_messages=history)
        s.client=lambda session:client
        return s,client

    async def listing(self,s,arg='12 -1000000000123'):
        with patch.object(l,'authorized',return_value=True),patch.object(l,'ensure_allowed',AsyncMock()),patch.object(l,'require_entity'):
            await l.run(s,99,arg)

    async def test_filters_types_and_deduplicates_albums(self):
        s,c=self.setup_service([[message(1),message(2,'photo'),message(3,'video/mp4',77),message(4,'photo',77),message(5,'gif'),message(6,'application/pdf'),message(7,'image/jpeg'),message(8,'sticker')]])
        await self.listing(s)
        text='\n'.join(call.args[1] for call in s.tell.call_args_list)
        self.assertIn('4 enlaces',text)
        for mid in (2,3,5,7):self.assertIn(f'https://t.me/c/0000000123/{mid}',text)
        for mid in (1,4,6,8):self.assertNotIn(f'https://t.me/c/0000000123/{mid}',text)
        s.repo.pool.execute.assert_not_awaited()
        c.disconnect.assert_awaited_once()

    async def test_album_crossing_history_pages_is_listed_once(self):
        first=[message(i) for i in range(1,99)]+[message(99,'photo',88),message(100,'video/mp4',88)]
        s,c=self.setup_service([first,[message(101,'photo',88),message(102,'photo')]])
        await self.listing(s)
        text='\n'.join(call.args[1] for call in s.tell.call_args_list)
        self.assertIn('2 enlaces',text)
        self.assertNotIn('/101',text)
        self.assertEqual(c.disconnect.await_count,2)

    async def test_nonadmin_does_not_read_session(self):
        s,c=self.setup_service([[]])
        with patch.object(l,'authorized',return_value=False):await l.run(s,99,'12 -100123')
        s.repo.account.assert_not_awaited();c.connect.assert_not_awaited()

    async def test_flood_preserves_partial_continuation_and_wait(self):
        s,c=self.setup_service([[]]);c.connect.side_effect=errors.FloodWaitError(request=None,capture=25)
        await self.listing(s,'12 -1000000000123 55')
        s.repo.set_setting.assert_awaited_once()
        self.assertIn('/enlace 12 -1000000000123 55',s.tell.call_args.args[1])
        c.disconnect.assert_awaited_once()

    async def test_router_sends_private_admin_listing_to_task(self):
        from descargas_app.admin_panel import AdminPanel
        s,c=self.setup_service([[]]);panel=AdminPanel(s)
        with patch('descargas_app.admin_panel.authorized',return_value=True),patch.object(l,'run',AsyncMock()) as run:
            await panel.run(99,99,'enlace','12 -1000000000123')
            run.assert_awaited_once_with(s,99,'12 -1000000000123')
