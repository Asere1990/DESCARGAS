import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telethon import types
from descargas_app import group_download as g

@asynccontextmanager
async def lock(*args, **kwargs):
    yield

class GroupExportTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        pool=NS(fetchrow=AsyncMock(return_value=None),execute=AsyncMock())
        repo=NS(pool=pool,account=AsyncMock(return_value={'session':b'x','generation':4}),
            account_lock=lock,media_lock=lock,media_cache=AsyncMock(return_value=None),
            media_publishing=AsyncMock(),media_ready=AsyncMock(),register_origins=AsyncMock(),pace=AsyncMock())
        return NS(bot_id=7,almacen=-100999,repo=repo,tell=AsyncMock(),stopping=False,
            get_session=AsyncMock(return_value='session'),resolve=AsyncMock(),source_items=AsyncMock())

    def job(self):
        return dict(bot_id=7,uid=12,source=-1000000000123,actor=99,generation=4,cursor=4,state='running',sending=False)

    async def test_only_private_admin_can_start_or_stop(self):
        s=self.service()
        with patch.object(g,'authorized',return_value=False):
            self.assertTrue(await g.command(s,dict(text='/descargar 12 -1000000000123',chat={'type':'private'},**{'from':{'id':99}})))
        s.repo.account.assert_not_awaited()
        s.repo.pool.execute.assert_not_awaited()
        s.tell.assert_not_awaited()

    async def test_restart_upsert_preserves_saved_cursor_and_total(self):
        s=self.service()
        s.repo.pool.fetchrow.return_value={**self.job(),'cursor':81,'total':50}
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,dict(text='/descargar 12 -1000000000123',chat={'type':'private'},**{'from':{'id':99}}))
        sql=s.repo.pool.execute.call_args.args[0].split('DO UPDATE SET')[1]
        self.assertNotIn('cursor=',sql)
        self.assertNotIn('total=',sql)

    async def test_uncertain_upload_cannot_be_replayed(self):
        s=self.service();s.repo.pool.fetchrow.return_value={**self.job(),'sending':True}
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,dict(text='/descargar 12 -1000000000123',chat={'type':'private'},**{'from':{'id':99}}))
        s.repo.pool.execute.assert_not_awaited()
        self.assertIn('revisión',s.tell.call_args.args[1])

    async def test_stop_does_not_delete_progress(self):
        s=self.service();s.repo.pool.fetchrow.return_value={'cursor':20}
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,dict(text='/stop 12 -1000000000123',chat={'type':'private'},**{'from':{'id':99}}))
        sql=s.repo.pool.fetchrow.call_args.args[0]
        self.assertIn("state='paused'",sql)
        self.assertNotIn('cursor=',sql)

    async def test_revoked_session_prevents_more_source_reads(self):
        s=self.service();s.repo.pool.fetchrow.return_value=self.job()
        s.repo.account.return_value={'session':b'x','generation':5}
        with patch.object(g,'authorized',return_value=True):
            with self.assertRaisesRegex(ValueError,'desconectada'):
                await g.checked(s,self.job())
        s.resolve.assert_not_awaited()

    async def test_cached_content_is_not_uploaded_again(self):
        s=self.service();s.repo.media_cache.return_value={'state':'ready'}
        item=NS(file=NS(size=1),photo=NS(id=3),document=None)
        result=await g.archive_items(s,self.job(),None,(-1000000000123,5),[item],'Persona')
        self.assertEqual(result,0)
        s.repo.media_publishing.assert_not_awaited()

    async def test_album_cursor_saved_once_then_end_notifies_admin(self):
        s=self.service();job=self.job()
        s.resolve.return_value=types.Channel(id=123,title='Origen',photo=types.ChatPhotoEmpty(),date=None,megagroup=True)
        items=[NS(id=i,photo=True,document=None,media=None) for i in [5,6,7]]
        async def history(*args,**kwargs):
            self.assertEqual(kwargs['min_id'],4)
            self.assertTrue(kwargs['reverse'])
            for m in items:yield m
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=NS(first_name='A',last_name='B')),iter_messages=history)
        s.client=lambda session:client
        s.source_items.return_value=((-1000000000123,888),items)
        s.repo.pool.fetchrow.return_value={'total':3}
        with patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'require_entity'),\
            patch.object(g,'archive_items',AsyncMock(return_value=3)) as archive,\
            patch.object(g,'save_cursor',AsyncMock()) as save:
            await g.step(s,job)
        archive.assert_awaited_once()
        self.assertEqual([call.args[2:] for call in save.await_args_list],[(5,3),(6,0),(7,0)])
        self.assertEqual(s.tell.call_args.args[0],99)
        self.assertIn('completada',s.tell.call_args.args[1])
        client.disconnect.assert_awaited_once()

    async def test_pause_while_preparing_preserves_previous_cursor(self):
        s=self.service();job=self.job()
        s.resolve.return_value=types.Channel(id=123,title='Origen',photo=types.ChatPhotoEmpty(),date=None)
        async def history(*args,**kwargs):yield NS(id=5,photo=True,document=None,media=None)
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=NS(first_name='A',last_name=None)),iter_messages=history)
        s.client=lambda session:client
        s.source_items.return_value=((-1000000000123,5),[NS(id=5)])
        with patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'require_entity'),\
            patch.object(g,'archive_items',AsyncMock(return_value=None)),patch.object(g,'save_cursor',AsyncMock()) as save:
            await g.step(s,job)
        save.assert_not_awaited()
        s.tell.assert_not_awaited()


    async def test_archive_preserves_caption_entities_and_origin(self):
        s=self.service();job=self.job()
        item=NS(id=5,message='Texto original',entities=[object()],file=NS(size=1),photo=NS(id=3),document=None)
        sent=NS(id=51)
        s.bot=NS(get_input_entity=AsyncMock(return_value='almacen'),send_file=AsyncMock(return_value=sent))
        attribution={'caption':'Usuario e ID','caption_entities':[]}
        with patch.object(g,'prepare_album',AsyncMock(return_value=['media'])),\
            patch.object(g,'checked',AsyncMock(return_value=True)),\
            patch.object(g,'archive_attribution',AsyncMock(return_value=attribution)),\
            patch.object(g,'append_archive_source',AsyncMock()) as origin,\
            patch.object(g,'apply_archive_caption',AsyncMock()) as caption:
            result=await g.archive_items(s,job,None,(job['source'],5),[item],'Persona')
        self.assertEqual(result,1)
        self.assertEqual(s.bot.send_file.call_args.args[0],'almacen')
        self.assertEqual(s.bot.send_file.call_args.kwargs['caption'],'Texto original')
        self.assertEqual(s.bot.send_file.call_args.kwargs['formatting_entities'],item.entities)
        origin.assert_awaited_once_with(attribution,item,job['source'])
        caption.assert_awaited_once_with(s,51,attribution,sent)
        s.repo.register_origins.assert_awaited_once_with(7,-100999,[51],12,job['source'])
        self.assertIn('sending=TRUE',s.repo.pool.execute.call_args.args[0])

    async def test_ambiguous_send_never_marks_cache_ready(self):
        s=self.service();job=self.job()
        item=NS(id=5,message='Caption',entities=[],file=NS(size=1),photo=NS(id=3),document=None)
        s.bot=NS(get_input_entity=AsyncMock(return_value='almacen'),send_file=AsyncMock(side_effect=TimeoutError()))
        with patch.object(g,'prepare_album',AsyncMock(return_value=['media'])),\
            patch.object(g,'checked',AsyncMock(return_value=True)),\
            patch.object(g,'archive_attribution',AsyncMock(return_value={})),\
            patch.object(g,'append_archive_source',AsyncMock()):
            with self.assertRaises(TimeoutError):
                await g.archive_items(s,job,None,(job['source'],5),[item],'Persona')
        s.repo.media_publishing.assert_awaited_once()
        s.repo.media_ready.assert_not_awaited()
        self.assertIn('sending=TRUE',s.repo.pool.execute.call_args.args[0])

    async def test_live_group_list_excludes_private_left_and_migrated_dialogs(self):
        s=self.service()
        channel=types.Channel(id=123,title='Canal A',photo=types.ChatPhotoEmpty(),date=None,broadcast=True)
        group=types.Chat(id=22,title='Grupo B',photo=types.ChatPhotoEmpty(),participants_count=1,date=None,version=1)
        left=types.Channel(id=124,title='Fuera',photo=types.ChatPhotoEmpty(),date=None,left=True)
        migrated=types.Chat(id=23,title='Anterior',photo=types.ChatPhotoEmpty(),participants_count=1,date=None,version=1,migrated_to=types.InputChannel(456,0))
        async def dialogs():
            for entity in [types.User(id=1,first_name='Privado'),channel,group,left,migrated]:
                yield NS(entity=entity)
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=NS(id=12)),iter_dialogs=dialogs)
        s.client=lambda session:client
        with patch.object(g,'authorized',return_value=True):
            await g.list_groups(s,99,'12')
        text=''.join(call.args[1] for call in s.tell.await_args_list)
        self.assertIn('1. Canal A',text)
        self.assertIn('2. Grupo B',text)
        self.assertIn('/descargar 12 -1000000000123',text)
        self.assertIn('/descargar 12 -22',text)
        self.assertNotIn('Privado',text)
        self.assertNotIn('Fuera',text)
        self.assertNotIn('Anterior',text)
        client.disconnect.assert_awaited_once()

    async def test_live_group_list_splits_large_lists_without_losing_rows(self):
        s=self.service()
        async def dialogs():
            for i in range(1,130):
                yield NS(entity=types.Channel(id=i,title='Grupo '+str(i)+' '+('A'*100),photo=types.ChatPhotoEmpty(),date=None,megagroup=True))
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),get_me=AsyncMock(return_value=NS(id=12)),iter_dialogs=dialogs)
        s.client=lambda session:client
        with patch.object(g,'authorized',return_value=True):
            await g.list_groups(s,99,'12')
        texts=[call.args[1] for call in s.tell.await_args_list]
        self.assertGreater(len(texts),1)
        self.assertTrue(all(len(t.encode('utf-16-le'))//2<4096 for t in texts))
        self.assertIn('129. Grupo 129',''.join(texts))
        self.assertIn('Total: 129',''.join(texts))

    async def test_group_argument_routes_to_live_account_listing(self):
        from descargas_app.admin_panel import AdminPanel
        s=self.service();panel=AdminPanel(s)
        panel.groups=AsyncMock()
        with patch('descargas_app.admin_panel.authorized',return_value=True),patch.object(g,'list_groups',AsyncMock()) as listing:
            await panel.run(99,99,'grupos','12')
            listing.assert_awaited_once_with(s,99,'12')
            panel.groups.assert_not_awaited()
            await panel.run(99,99,'grupos','')
            panel.groups.assert_awaited_once_with(99,20)
