import unittest
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telethon import types, functions, errors
from descargas_app import group_download as g

@asynccontextmanager
async def lock(*args, **kwargs):
    yield

class GroupExportTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        pool=NS(fetchrow=AsyncMock(return_value=None),fetchval=AsyncMock(side_effect=lambda sql,*args: 1 if "UPDATE descarga_group_exports SET sending=TRUE" in sql else False),executemany=AsyncMock(),execute=AsyncMock())
        @asynccontextmanager
        async def acquire():yield pool
        pool.acquire=acquire
        repo=NS(pool=pool,setting=AsyncMock(return_value=None),set_setting=AsyncMock(),account=AsyncMock(return_value={'session':b'x','generation':4}),
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

    async def test_restart_upsert_preserves_cursor_and_saved_totals(self):
        s=self.service()
        s.repo.pool.fetchrow.return_value={**self.job(),'cursor':81,'total':50,'state':'paused'}
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,dict(text='/descargar 12 -1000000000123',chat={'type':'private'},**{'from':{'id':99}}))
        sql=s.repo.pool.execute.call_args.args[0].split('DO UPDATE SET')[1]
        self.assertIn('cursor=descarga_group_exports.cursor',sql)
        self.assertNotIn('ELSE 0',sql)
        self.assertIn("phase='count'",sql)
        self.assertNotIn("phase='index'",sql)
        self.assertNotIn('total=',sql)

    async def test_uncertain_upload_cannot_be_replayed(self):
        s=self.service();s.repo.pool.fetchrow.return_value={**self.job(),'sending':True}
        s.repo.pool.fetchval.side_effect=None;s.repo.pool.fetchval.return_value=0
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,dict(text='/descargar 12 -1000000000123',chat={'type':'private'},**{'from':{'id':99}}))
        s.repo.pool.execute.assert_not_awaited()
        self.assertIn('en curso',s.tell.call_args.args[1])

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
        s=self.service();s.repo.media_cache.return_value={'state':'ready','payload':b'cache'};s.repo.decode=lambda data:{'ids':[51]}
        item=NS(file=NS(size=1),photo=NS(id=3),document=None)
        result=await g.archive_items(s,self.job(),None,(-1000000000123,5),[item],'Persona')
        self.assertEqual(result,0)
        s.repo.media_publishing.assert_not_awaited()

    async def test_delivered_album_releases_account_before_next_message(self):
        s=self.service();job=self.job()
        s.resolve.return_value=types.Channel(id=123,title='Origen',photo=types.ChatPhotoEmpty(),date=None,megagroup=True)
        items=[NS(id=i,photo=True,document=None,media=None) for i in [5,6,7]]
        async def history(*args,**kwargs):
            self.assertEqual(kwargs['min_id'],4)
            self.assertTrue(kwargs['reverse'])
            for m in items:yield m
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=NS(first_name='A',last_name='B')),get_messages=AsyncMock(return_value=[]),iter_messages=history)
        s.client=lambda session:client
        s.source_items.return_value=((-1000000000123,888),items)
        s.repo.pool.fetchrow.return_value={'total':3}
        with patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'require_entity'),\
            patch.object(g,'archive_items',AsyncMock(return_value=3)) as archive,\
            patch.object(g,'save_cursor',AsyncMock()) as save:
            await g.step(s,job)
        archive.assert_awaited_once()
        self.assertEqual([call.args[2:] for call in save.await_args_list],[(5,3)])
        s.tell.assert_not_awaited()
        client.disconnect.assert_awaited_once()

    async def test_single_pass_skips_known_then_uploads_next_before_reading_more(self):
        s=self.service();job={**self.job(),'phase':'download'}
        s.resolve.return_value=types.Channel(id=123,title='Origen',photo=types.ChatPhotoEmpty(),date=None)
        items=[NS(id=i,photo=True,document=None,media=None) for i in [5,6,7]]
        async def history(*args,**kwargs):
            for item in items:yield item
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),get_me=AsyncMock(return_value=NS(first_name='A',last_name=None)),iter_messages=history,get_messages=AsyncMock())
        s.client=lambda session:client
        sequence=[]
        async def source(client,link):
            mid=int(link.rsplit('/',1)[1]);sequence.append(('read',mid))
            return (job['source'],mid),[items[mid-5]]
        async def archive(service,job,client,key,items,name):
            sequence.append(('check_archive',key[1]));return 0 if key[1]==5 else 1
        s.source_items.side_effect=source
        with patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'users_busy',AsyncMock(return_value=False)),patch.object(g,'require_entity'),patch.object(g,'archive_items',side_effect=archive),patch.object(g,'save_cursor',AsyncMock()) as save:
            await g.step(s,job)
        self.assertEqual(sequence,[('read',5),('check_archive',5),('read',6),('check_archive',6)])
        self.assertEqual([c.args[2:] for c in save.await_args_list],[(5,0),(6,1)])
        client.get_messages.assert_not_awaited()

    async def test_pause_while_preparing_preserves_previous_cursor(self):
        s=self.service();job=self.job()
        s.resolve.return_value=types.Channel(id=123,title='Origen',photo=types.ChatPhotoEmpty(),date=None)
        async def history(*args,**kwargs):yield NS(id=5,photo=True,document=None,media=None)
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=NS(first_name='A',last_name=None)),get_messages=AsyncMock(return_value=[]),iter_messages=history)
        s.client=lambda session:client
        s.source_items.return_value=((-1000000000123,5),[NS(id=5)])
        with patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'require_entity'),\
            patch.object(g,'archive_items',AsyncMock(return_value=None)),patch.object(g,'save_cursor',AsyncMock()) as save:
            await g.step(s,job)
        save.assert_not_awaited()
        s.tell.assert_not_awaited()


    async def test_archive_preserves_caption_entities_and_origin(self):
        s=self.service();job=self.job()
        item=NS(id=5,message='Texto original',entities=[types.MessageEntityBold(offset=0,length=5)],file=NS(size=1),photo=NS(id=3),document=None)
        sent=NS(id=51,photo=NS(id=333,dc_id=2,access_hash=777,file_reference=b'bot',sizes=[NS(type='y',size=1)]))
        s.bot=NS(get_input_entity=AsyncMock(return_value='almacen'),send_file=AsyncMock(return_value=sent))
        attribution={'caption':'Usuario e ID','caption_entities':[]}
        with patch.object(g,'prepare_album',AsyncMock(return_value=['media'])),\
            patch.object(g,'checked',AsyncMock(return_value=True)),\
            patch.object(g,'archive_attribution',AsyncMock(return_value=attribution)),\
            patch.object(g,'append_archive_source',AsyncMock()) as origin,\
            patch.object(g,'apply_archive_caption',AsyncMock()) as caption:
            result=await g.archive_items(s,job,None,(job['source'],5),[item],'Persona')
        self.assertEqual(result,1)
        reusable=s.repo.media_ready.call_args.kwargs['file_cache']
        self.assertEqual(reusable['bot_id'],7)
        self.assertEqual(reusable['items'][0]['caption'],'Texto original')
        self.assertTrue(reusable['items'][0]['file_id'])
        self.assertEqual(s.bot.send_file.call_args.args[0],'almacen')
        self.assertEqual(s.bot.send_file.call_args.kwargs['caption'],'Texto original')
        self.assertEqual(s.bot.send_file.call_args.kwargs['formatting_entities'],item.entities)
        origin.assert_awaited_once_with(attribution,item,job['source'])
        caption.assert_awaited_once_with(s,51,attribution,sent)
        s.repo.register_origins.assert_awaited_once_with(7,-100999,[51],12,job['source'])
        self.assertTrue(any('sending=TRUE' in call.args[0] for call in s.repo.pool.fetchval.await_args_list))

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
        self.assertTrue(any('sending=TRUE' in call.args[0] for call in s.repo.pool.fetchval.await_args_list))

    async def test_live_group_list_excludes_private_left_and_migrated_dialogs(self):
        s=self.service()
        s.repo.encode=lambda value:json.dumps(value).encode()
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
        with patch('descargas_app.moderation.authorized',return_value=True):
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

    async def test_live_group_list_splits_first_100_and_reports_full_total(self):
        s=self.service()
        s.repo.encode=lambda value:json.dumps(value).encode()
        async def dialogs():
            for i in range(1,130):
                yield NS(entity=types.Channel(id=i,title='Grupo '+str(i)+' '+('A'*100),photo=types.ChatPhotoEmpty(),date=None,megagroup=True))
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),get_me=AsyncMock(return_value=NS(id=12)),iter_dialogs=dialogs)
        s.client=lambda session:client
        with patch('descargas_app.moderation.authorized',return_value=True):
            await g.list_groups(s,99,'12')
        texts=[call.args[1] for call in s.tell.await_args_list]
        self.assertGreater(len(texts),1)
        self.assertTrue(all(len(t.encode('utf-16-le'))//2<4096 for t in texts))
        self.assertIn('100. Grupo 30',''.join(texts))
        self.assertNotIn('101. Grupo',''.join(texts))
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


    async def test_new_messages_arriving_during_upload_keep_scan_running(self):
        s=self.service();job=self.job()
        s.resolve.return_value=types.Channel(id=123,title='Origen',photo=types.ChatPhotoEmpty(),date=None)
        async def history(*args,**kwargs):yield NS(id=5,photo=True,document=None,media=None)
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),get_me=AsyncMock(return_value=NS(first_name='A',last_name='B')),get_messages=AsyncMock(return_value=[NS(id=6)]),iter_messages=history)
        s.client=lambda session:client
        s.source_items.return_value=((job['source'],5),[NS(id=5)])
        with patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'require_entity'),patch.object(g,'archive_items',AsyncMock(return_value=0)):
            await g.step(s,job)
        s.repo.pool.fetchrow.assert_not_awaited()
        s.tell.assert_not_awaited()


    def test_filter_parser_and_gif_is_not_video(self):
        self.assertEqual(g.parse_command('/descargar 12 -100123 video'),('descargar',12,-100123,'video'))
        self.assertEqual(g.parse_command('/descargar@Bot 12 -100123 fotos')[-1],'fotos')
        self.assertEqual(g.parse_command('/descargar 12 -100123')[-1],'all')
        self.assertIsNone(g.parse_command('/descargar 12 -100123 audio'))
        self.assertIsNone(g.parse_command('/stop 12 -100123 fotos'))
        self.assertEqual(g.media_kind(NS(gif=True,video=True,photo=None)),'gif')

    async def test_stale_send_unblocks_job_but_retains_uncertain_cache(self):
        s=self.service();s.repo.pool.fetchrow.return_value={**self.job(),'sending':True,'state':'paused'}
        s.repo.pool.fetchval.side_effect=lambda sql,*args: 1 if 'pg_try_advisory_lock' in sql else 0
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,dict(text='/descargar 12 -1000000000123 video',chat={'type':'private'},**{'from':{'id':99}}))
        sqls=[c.args[0] for c in s.repo.pool.execute.await_args_list]
        self.assertTrue(any('sending=FALSE' in sql for sql in sqls))
        self.assertFalse(any('descarga_media_cache' in sql for sql in sqls))
        self.assertEqual(s.repo.pool.execute.call_args.args[-1],'video')

    async def test_count_precedes_any_source_history_or_upload(self):
        s=self.service();job={**self.job(),'phase':'count','media_filter':'video'}
        s.resolve.return_value=types.Channel(id=123,title='Origen',photo=types.ChatPhotoEmpty(),date=None)
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=NS(first_name='A',last_name=None)),get_messages=AsyncMock(side_effect=[NS(total=10),NS(total=20),NS(total=3)]),iter_messages=AsyncMock())
        s.client=lambda session:client
        with patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'require_entity'):
            await g.step(s,job)
        self.assertIn('Fotos: 10\nVideos: 20\nGIF: 3',s.tell.call_args.args[1])
        client.iter_messages.assert_not_called();s.source_items.assert_not_awaited()
        self.assertIn("phase='download'",s.repo.pool.execute.call_args.args[0])

    async def test_rejected_send_clears_only_publishing_cache_and_respects_cooldown(self):
        s=self.service();job=self.job()
        item=NS(id=5,message='Caption',entities=[],file=NS(size=1),photo=NS(id=3),document=None)
        rpc=functions.messages.SendMediaRequest(peer=types.InputPeerSelf(),media=types.InputMediaEmpty(),message='')
        exc=errors.FloodWaitError(request=rpc,capture=120)
        s.bot=NS(get_input_entity=AsyncMock(return_value='almacen'),send_file=AsyncMock(side_effect=exc))
        with patch.object(g,'prepare_album',AsyncMock(return_value=['media'])),patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'archive_attribution',AsyncMock(return_value={})),patch.object(g,'append_archive_source',AsyncMock()):
            with self.assertRaises(errors.FloodWaitError):
                await g.archive_items(s,job,None,(job['source'],5),[item],'Persona')
        self.assertTrue(any("DELETE FROM descarga_media_cache WHERE key=$1 AND state='publishing'" in c.args[0] for c in s.repo.pool.execute.await_args_list))
        self.assertTrue(any('sending=FALSE' in c.args[0] for c in s.repo.pool.execute.await_args_list))
        s.repo.media_ready.assert_not_awaited()

    async def test_filtered_mixed_album_uploads_only_requested_type(self):
        s=self.service();job={**self.job(),'media_filter':'video'}
        s.resolve.return_value=types.Channel(id=123,title='Origen',photo=types.ChatPhotoEmpty(),date=None)
        items=[NS(id=5,photo=NS(id=3),document=None,video=None,gif=None,file=NS(size=1),media=None),
               NS(id=6,photo=None,document=NS(id=4),video=True,gif=None,file=NS(size=1),media=None)]
        async def history(*args,**kwargs):yield items[0]
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),get_me=AsyncMock(return_value=NS(first_name='A',last_name=None)),iter_messages=history)
        s.client=lambda session:client;s.source_items.return_value=((job['source'],888),items)
        with patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'require_entity'),patch.object(g,'archive_items',AsyncMock(return_value=1)) as archive:
            await g.step(s,job)
        self.assertEqual(archive.call_args.args[4],[items[1]])

    async def test_uncertain_file_cannot_be_uploaded_under_different_filter_key(self):
        s=self.service()
        s.repo.pool.fetchval.side_effect=lambda sql,*args: 1 if 'SELECT 1 FROM descarga_export_uncertain' in sql else False
        item=NS(id=5,photo=NS(id=3),document=None,file=NS(size=1))
        with self.assertRaisesRegex(ValueError,'otro filtro'):
            await g.archive_items(s,self.job(),None,(self.job()['source'],888),[item],'Persona')
        s.repo.media_publishing.assert_not_awaited()


    async def test_resume_sql_keeps_position_even_when_filter_changes(self):
        import sqlite3
        s=self.service()
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,dict(text='/descargar 12 -1000000000123',chat={'type':'private'},**{'from':{'id':99}}))
        sql,*args=s.repo.pool.execute.call_args.args
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute("CREATE TABLE descarga_group_exports(bot_id INTEGER,uid INTEGER,source INTEGER,actor INTEGER,generation INTEGER,state TEXT,phase TEXT,media_filter TEXT,cursor INTEGER DEFAULT 0,total INTEGER DEFAULT 0,resume_at REAL DEFAULT 0,scan INTEGER DEFAULT 0,retry_message INTEGER DEFAULT 0,PRIMARY KEY(bot_id,uid,source))")
        db.execute(sql,args)
        for state in ('paused','done'):
            db.execute("UPDATE descarga_group_exports SET cursor=81,total=50,state=?",(state,))
            db.execute(sql,args)
            self.assertEqual(db.execute('SELECT cursor,total,state FROM descarga_group_exports').fetchone(),(81,50,'running'))
        db.execute(sql,[*args[:-1],'video'])
        self.assertEqual(db.execute('SELECT cursor,total,media_filter FROM descarga_group_exports').fetchone(),(81,50,'video'))
        db.execute('UPDATE descarga_group_exports SET cursor=95')
        db.execute(sql,[*args[:-1],'video'])
        self.assertEqual(db.execute('SELECT cursor FROM descarga_group_exports').fetchone(),(95,))


    async def test_pending_private_admin_lists_scoped_links_without_session_or_mutation(self):
        s=self.service();s.repo.pool.fetch=AsyncMock(return_value=[{'message_id':55,'error':'ValueError'}])
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,dict(text='/pendientes 12 -1000000000123',chat={'type':'private'},**{'from':{'id':99}}))
        self.assertEqual(s.repo.pool.fetch.call_args.args[1:],(7,12,-1000000000123,0))
        self.assertIn('https://t.me/c/0000000123/55',s.tell.call_args_list[0].args[1])
        self.assertIn('ValueError',s.tell.call_args_list[0].args[1])
        s.repo.account.assert_not_awaited();s.repo.pool.execute.assert_not_awaited()
        s.repo.pool.fetchval.assert_not_awaited()

    async def test_pending_denies_non_admin_and_group_chats(self):
        for admin,chat in [(False,'private'),(True,'supergroup')]:
            s=self.service();s.repo.pool.fetch=AsyncMock()
            with patch.object(g,'authorized',return_value=admin):
                await g.command(s,dict(text='/pendientes 12 -100123',chat={'type':chat},**{'from':{'id':99}}))
            s.repo.pool.fetch.assert_not_awaited();s.tell.assert_not_awaited()

    async def test_pending_empty_and_basic_group_have_no_fabricated_link(self):
        s=self.service();s.repo.pool.fetch=AsyncMock(return_value=[])
        await g.list_pending(s,99,12,-22)
        self.assertIn('No hay pendientes',s.tell.call_args.args[1])
        s.repo.pool.fetch.return_value=[{'message_id':8,'error':'TimeoutError'}]
        await g.list_pending(s,99,12,-22)
        self.assertIn('Mensaje 8',s.tell.call_args_list[-2].args[1])
        self.assertNotIn('https://t.me/c/',s.tell.call_args_list[-2].args[1])

    async def test_pending_pages_all_entries_with_bounded_telegram_messages(self):
        s=self.service();s.repo.pool.fetch=AsyncMock(side_effect=[
            [{'message_id':i,'error':'X'*200} for i in range(1,16)],
            [{'message_id':16,'error':'TimeoutError'}]])
        await g.list_pending(s,99,12,-100123)
        self.assertEqual(s.repo.pool.fetch.call_args.args[-1],15)
        self.assertIn('Total mostrado: 16',s.tell.call_args.args[1])
        self.assertTrue(all(len(c.args[1].encode('utf-16-le'))//2<4096 for c in s.tell.call_args_list))

    def test_pending_parser_rejects_filters_and_accepts_bot_suffix(self):
        self.assertEqual(g.parse_command('/pendientes@Bot 12 -100123'),('pendientes',12,-100123,'all'))
        self.assertIsNone(g.parse_command('/pendientes 12 -100123 video'))


    async def test_admin_retry_queues_selected_account_without_resetting_cursor(self):
        s=self.service();s.repo.pool.fetchrow.return_value={**self.job(),'state':'done','cursor':90}
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,dict(text='/reintentar 12 https://t.me/c/0000000123/55',chat={'type':'private'},**{'from':{'id':99}}))
        sql,*args=s.repo.pool.execute.call_args.args
        self.assertIn("'retry'",sql)
        self.assertNotIn('cursor=',sql)
        self.assertEqual(args,[7,12,-1000000000123,99,4,55])
        self.assertIn('ALMACEN',s.tell.call_args.args[1])

    async def test_retry_rejects_uncertain_send_and_invalid_links(self):
        s=self.service();s.repo.pool.fetchrow.return_value={**self.job(),'state':'paused','sending':True}
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,dict(text='/reintentar 12 https://t.me/c/123/55',chat={'type':'private'},**{'from':{'id':99}}))
        s.repo.pool.execute.assert_not_awaited()
        self.assertIn('incierto',s.tell.call_args.args[1])
        for text in ['/reintentar 12 https://t.me/+invite','/reintentar 12 https://evil.test/c/123/55','/reintentar 12 https://t.me/c/123/0']:
            with patch.object(g,'authorized',return_value=True):
                await g.command(s,dict(text=text,chat={'type':'private'},**{'from':{'id':99}}))
            self.assertIn('Uso:',s.tell.call_args.args[1])

    async def test_retry_only_reads_requested_album_and_does_not_advance_history(self):
        s=self.service();job={**self.job(),'phase':'retry','retry_message':55}
        s.resolve.return_value=types.Channel(id=123,title='Origen',photo=types.ChatPhotoEmpty(),date=None)
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),get_me=AsyncMock(return_value=NS(first_name='A',last_name=None)),iter_messages=AsyncMock())
        s.client=lambda session:client
        items=[NS(id=55),NS(id=56)]
        s.source_items.return_value=((job['source'],888),items)
        with patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'require_entity'),patch.object(g,'archive_items',AsyncMock(return_value=2)) as archive:
            await g.step(s,job)
        s.source_items.assert_awaited_once_with(client,'https://t.me/c/0000000123/55')
        archive.assert_awaited_once_with(s,job,client,(job['source'],888),items,'A')
        client.iter_messages.assert_not_called()
        sql=s.repo.pool.execute.call_args.args[0]
        self.assertNotIn('cursor=',sql)
        self.assertIn('state=retry_return_state',sql)
        self.assertIn('terminada',s.tell.call_args.args[1])

    async def test_nonadmin_cannot_retry_using_another_account(self):
        s=self.service()
        with patch.object(g,'authorized',return_value=False):
            await g.command(s,dict(text='/reintentar 12 https://t.me/c/123/55',chat={'type':'private'},**{'from':{'id':99}}))
        s.repo.account.assert_not_awaited();s.repo.pool.execute.assert_not_awaited()


    async def test_download_private_link_uses_selected_account_and_only_link_job(self):
        s=self.service();s.repo.pool.fetchrow.return_value={**self.job(),'state':'done'}
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,dict(text='/descargar@Bot 12 https://t.me/c/0000000123/55',chat={'type':'private'},**{'from':{'id':99}}))
        sql,*args=s.repo.pool.execute.call_args.args
        self.assertIn("'retry'",sql)
        self.assertNotIn('cursor=',sql)
        self.assertEqual(args,[7,12,-1000000000123,99,4,55])
        self.assertIn('Descarga del enlace registrada',s.tell.call_args.args[1])

    async def test_download_invalid_link_does_not_start_full_history(self):
        s=self.service()
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,dict(text='/descargar 12 https://t.me/+invite',chat={'type':'private'},**{'from':{'id':99}}))
        s.repo.account.assert_not_awaited();s.repo.pool.execute.assert_not_awaited()
        self.assertIn('Uso: /descargar',s.tell.call_args.args[1])
