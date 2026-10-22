import json
import sqlite3
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from descargas_app import manual_board as b

class Pool:
    def __init__(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        CREATE TABLE descarga_manual_boards(bot_id INTEGER,uid INTEGER,source INTEGER,actor INTEGER,token TEXT UNIQUE,payload BLOB,position INTEGER DEFAULT 0,message_id INTEGER,busy INTEGER DEFAULT 0,closed INTEGER DEFAULT 0,PRIMARY KEY(bot_id,uid,source));
        CREATE TABLE descarga_group_exports(bot_id INTEGER,uid INTEGER,source INTEGER,actor INTEGER,generation INTEGER,state TEXT DEFAULT 'paused',phase TEXT DEFAULT 'download',retry_message INTEGER DEFAULT 0,retry_return_state TEXT DEFAULT 'paused',cursor INTEGER DEFAULT 0,total INTEGER DEFAULT 0,sending INTEGER DEFAULT 0,scan INTEGER DEFAULT 0,resume_at REAL DEFAULT 0,manual_token TEXT,PRIMARY KEY(bot_id,uid,source));
        ''')
    def query(self,sql,args):
        sql=sql.replace(' FOR UPDATE','')
        return self.db.execute(sql,{str(i):value for i,value in enumerate(args,1)})
    async def execute(self,sql,*args):
        if 'pg_advisory' in sql:return
        self.query(sql,args)
    async def fetchrow(self,sql,*args):
        row=self.query(sql,args).fetchone();return dict(row) if row else None
    async def fetchval(self,sql,*args):
        if 'pg_try_advisory' in sql:return True
        row=self.query(sql,args).fetchone();return row[0] if row else None
    @asynccontextmanager
    async def acquire(self):yield self
    @asynccontextmanager
    async def transaction(self):
        self.db.commit();self.db.execute('BEGIN')
        try:yield
        except BaseException:self.db.rollback();raise
        else:self.db.commit()

class BoardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool=Pool();self.addCleanup(self.pool.db.close)
        self.settings={}
        async def setting(key):return self.settings.get(key)
        async def put(key,value):self.settings[key]=str(value)
        repo=NS(pool=self.pool,encode=lambda d:json.dumps(d).encode(),decode=lambda d:json.loads(d),
            account=AsyncMock(return_value={'session':b'x','generation':4}),setting=setting,set_setting=put)
        self.s=NS(bot_id=7,almacen=-100999,repo=repo,tell=AsyncMock(),api=AsyncMock(return_value={'message_id':50}))
        self.data={'title':'Grupo prueba','generation':4,'entries':[
            {'mid':10,'ids':[10,11],'photos':1,'videos':1,'gifs':0,'elements':2,'files':[['photo',100],['document',101]],'status':'pending'},
            {'mid':20,'ids':[20],'photos':0,'videos':1,'gifs':0,'elements':1,'files':[['document',200]],'status':'pending'}]}
        await self.pool.execute('INSERT INTO descarga_manual_boards(bot_id,uid,source,actor,token,payload,message_id) VALUES($1,$2,$3,$4,$5,$6,$7)',7,12,-100123,99,'token',repo.encode(self.data),50)
    async def row(self):return await self.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE token=$1','token')
    async def job(self):return await self.pool.fetchrow('SELECT * FROM descarga_group_exports WHERE bot_id=$1',7)

    async def test_click_enqueues_once_and_completion_updates_same_panel_and_counters(self):
        await b.enqueue(self.s,99,'token',0,50)
        job=await self.job();self.assertEqual((job['uid'],job['source'],job['retry_message']),(12,-100123,10))
        self.assertTrue((await self.row())['busy'])
        with self.assertRaises(ValueError):await b.enqueue(self.s,99,'token',0,50)
        with patch('descargas_app.export_inventory.stored',AsyncMock(return_value=True)):
            await b.complete(self.s,job,[],2)
            await b.complete(self.s,job,[],2) # replay must not count twice
        row=await self.row();data=self.s.repo.decode(row['payload'])
        self.assertEqual(row['position'],1);self.assertFalse(row['busy'])
        text,buttons=b.presentation(row,data)
        self.assertIn('Fotos descargadas: 1',text);self.assertIn('Videos descargados: 1',text)
        self.assertIn('Enlaces descargados: 1',text);self.assertIn('2: https://t.me/c/123/20',text)
        self.assertEqual((await self.job())['total'],2)
        self.assertTrue(any(c.args[0]=='editMessageText' for c in self.s.api.call_args_list))
        with self.assertRaises(ValueError):await b.enqueue(self.s,99,'token',0,50)

    async def test_error_never_advances_or_counts_as_downloaded(self):
        await b.enqueue(self.s,99,'token',0,50);job=await self.job()
        await self.pool.execute("UPDATE descarga_group_exports SET state='paused'")
        await b.failed(self.s,job,'Telegram exige esperar')
        row=await self.row();self.assertEqual(row['position'],0);self.assertFalse(row['busy'])
        self.assertIn('No se avanzó',self.s.repo.decode(row['payload'])['notice'])
        self.assertIn('Enlaces descargados: 0',b.presentation(row,self.s.repo.decode(row['payload']))[0])

    async def test_excluded_link_moves_forward_without_false_download_counts(self):
        await b.enqueue(self.s,99,'token',0,50)
        with patch('descargas_app.export_inventory.stored',AsyncMock(return_value=False)):
            await b.complete(self.s,await self.job(),[],0)
        row=await self.row();self.assertEqual(row['position'],1)
        self.assertIn('Enlaces descargados: 0',b.presentation(row,self.s.repo.decode(row['payload']))[0])

    async def test_owner_message_and_cooldown_checks_prevent_unauthorized_queue(self):
        for actor,mid in ((98,50),(99,49)):
            with self.assertRaises(ValueError):await b.enqueue(self.s,actor,'token',0,mid)
        await self.s.repo.set_setting('link-list-wait:7:12',9999999999)
        with self.assertRaisesRegex(ValueError,'esperar'):await b.enqueue(self.s,99,'token',0,50)
        self.assertIsNone(await self.job())

    async def test_close_saves_progress_and_reopen_uses_same_position(self):
        await self.pool.execute('UPDATE descarga_manual_boards SET position=1')
        query={'data':'manual:c:token:1','message':{'chat':{'id':99},'message_id':50}}
        with patch.object(b,'authorized',return_value=True):
            await b.callback(self.s,99,query)
            self.assertTrue((await self.row())['closed'])
            with patch.object(b,'collect',AsyncMock()) as collect:
                await b.open_board(self.s,99,12,-100123)
                collect.assert_not_awaited()
        self.assertFalse((await self.row())['closed']);self.assertEqual((await self.row())['position'],1)

    async def test_filtered_destination_selection_opens_correct_source(self):
        from descargas_app.account_chat import selection_key
        entries=[{'id':-100123,'name':'Grupo','kind':'Grupo'},{'id':-100124,'name':'Canal','kind':'Canal'}]
        import time
        await self.s.repo.set_setting(selection_key(self.s,99,12),json.dumps({'expires':time.time()+100,'generation':4,'snapshot':self.s.repo.encode({'entries':entries}).hex()}))
        await b.destinations(self.s,99,12,False)
        with patch.object(b,'open_board',AsyncMock()) as opened:
            self.assertTrue(await b.select_number(self.s,99,'1'))
            opened.assert_awaited_once_with(self.s,99,12,-100123)
        listing=self.s.api.call_args.args[1]['text'];self.assertIn('Grupo',listing);self.assertNotIn('Canal',listing)

    async def test_back_button_returns_to_menu_and_last_download_reopens_board(self):
        await self.s.repo.set_setting(b.last_key(self.s,99,12),-100123)
        with patch.object(b,'authorized',return_value=True),patch.object(b,'menu',AsyncMock()) as menu:
            await b.callback(self.s,99,{'data':'manual:b:12','message':{'chat':{'id':99}}})
            menu.assert_awaited_once_with(self.s,99,12)
        with patch.object(b,'authorized',return_value=True),patch.object(b,'open_board',AsyncMock()) as opened:
            await b.callback(self.s,99,{'data':'manual:l:12','message':{'chat':{'id':99}}})
            opened.assert_awaited_once_with(self.s,99,12,-100123)

    async def test_saved_prefix_resumes_after_known_files_and_keeps_counts(self):
        self.data['entries'][0]['status']='saved'
        self.assertEqual(b.advance(self.data,0),1)
        text,_=b.presentation(dict(await self.row(),position=1),self.data)
        self.assertIn('Enlaces descargados: 1',text)
        self.assertIn('2: https://t.me/c/123/20',text)

    async def test_stale_worker_completion_cannot_advance_new_job(self):
        await b.enqueue(self.s,99,'token',0,50);job=await self.job()
        await self.pool.execute('UPDATE descarga_group_exports SET scan=scan+1')
        with patch('descargas_app.export_inventory.stored',AsyncMock(return_value=True)):
            await b.complete(self.s,job,[],2)
        self.assertEqual((await self.row())['position'],0)

    async def test_archive_attribution_adds_guide_without_removing_original_metadata(self):
        from descargas_app.media_cache import append_archive_source
        from telethon import types
        source=types.Channel(id=123,title='Nombre',photo=types.ChatPhotoEmpty(),date=None,username='example')
        msg=NS(id=10,get_chat=AsyncMock(return_value=source))
        data={'caption':'Usuario: Persona\nTelegram ID: 12','caption_entities':[]}
        await append_archive_source(data,msg,-100123)
        self.assertIn('Usuario: Persona',data['caption'])
        self.assertTrue(data['caption'].endswith('Enlace de origen: https://t.me/c/123/10'))
        self.assertEqual(data['caption_entities'][0]['url'],'https://t.me/example')

    async def test_collect_merges_album_across_pages_and_counts_inventory(self):
        from telethon import types
        import test_group_download as helpers
        s=helpers.GroupExportTests().service()
        s.repo.encode=lambda d:json.dumps(d).encode()
        s.repo.decode=lambda d:json.loads(d)
        s.repo.pool.fetchval.side_effect=None;s.repo.pool.fetchval.return_value=0
        s.repo.pool.fetch=AsyncMock(return_value=[{'kind':'document','file_id':102}])
        s.resolve.return_value=types.Channel(id=123,title='Grupo',photo=types.ChatPhotoEmpty(),date=None)
        def item(mid,kind=None,gid=None):
            return NS(id=mid,media=None,grouped_id=gid,gif=None,sticker=None,
                photo=NS(id=mid) if kind=='photo' else None,
                document=NS(id=mid,mime_type=kind) if kind and '/' in kind else None)
        pages=iter([[item(i) for i in range(1,99)]+[item(99,'photo',88),item(100,'video/mp4',88)],
                    [item(101,'photo',88),item(102,'image/jpeg')]])
        async def history(*args,**kwargs):
            for m in next(pages):yield m
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),iter_messages=history)
        s.client=lambda session:client
        with patch.object(b,'authorized',return_value=True),patch.object(b,'ensure_allowed',AsyncMock()),patch.object(b,'require_entity'):
            data=await b.collect(s,99,12,-1000000000123)
        self.assertEqual(len(data['entries']),2)
        album=data['entries'][0]
        self.assertEqual((album['elements'],album['photos'],album['videos']),(3,2,1))
        self.assertEqual(data['entries'][1]['status'],'saved')
        self.assertEqual(client.disconnect.await_count,2)

    async def test_long_flood_resumes_checkpoint_and_preserves_split_album(self):
        from telethon import types,errors
        self.s.stopping=False
        self.pool.fetch=AsyncMock(return_value=[])
        self.s.repo.pace=AsyncMock()
        @asynccontextmanager
        async def lock(*args,**kwargs):yield
        self.s.repo.account_lock=lock
        self.s.get_session=AsyncMock(return_value='session')
        self.s.resolve=AsyncMock(return_value=types.Channel(id=123,title='Grupo',photo=types.ChatPhotoEmpty(),date=None))
        def msg(mid,photo=False,gid=None):
            return NS(id=mid,grouped_id=gid,media=None,gif=None,sticker=None,document=None,photo=NS(id=mid) if photo else None)
        minimums=[];page_number=0
        async def history(*args,**kwargs):
            nonlocal page_number
            page_number+=1;minimums.append(kwargs['min_id'])
            if page_number==1:
                for i in range(1,101):yield msg(i,i>=99,88 if i>=99 else None)
            elif page_number==2:raise errors.FloodWaitError(request=None,capture=61)
            else:
                yield msg(101,True,88);yield msg(102,True)
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),iter_messages=history)
        self.s.client=lambda session:client
        with patch.object(b,'authorized',return_value=True),patch.object(b,'ensure_allowed',AsyncMock()),patch.object(b,'require_entity'):
            with self.assertRaises(errors.FloodWaitError):await b.collect(self.s,99,12,-1000000000123)
            data=await b.collect(self.s,99,12,-1000000000123)
        self.assertEqual(minimums,[0,100,100])
        self.assertEqual(len(data['entries']),2)
        self.assertEqual(data['entries'][0]['elements'],3)
        self.assertEqual(data['entries'][0]['ids'],[99,100,101])
        self.s.resolve.assert_awaited_once()
        self.assertEqual(client.flood_sleep_threshold,30)
        self.assertTrue(data['complete'])
