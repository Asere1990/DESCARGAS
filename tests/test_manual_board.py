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

    async def test_batch_ten_chains_durably_and_replay_cannot_duplicate(self):
        self.data['entries']=[dict(self.data['entries'][1],mid=i,ids=[i]) for i in range(1,13)]
        await self.pool.execute('UPDATE descarga_manual_boards SET payload=$1',self.s.repo.encode(self.data))
        await b.enqueue(self.s,99,'token',0,50,batch=10)
        first=await self.job()
        with patch('descargas_app.export_inventory.stored',AsyncMock(return_value=True)):
            await b.complete(self.s,first,[],1)
            await b.complete(self.s,first,[],1)
            self.assertEqual((await self.row())['position'],1)
            for _ in range(9):await b.complete(self.s,await self.job(),[],1)
        row=await self.row();job=await self.job()
        self.assertEqual(row['position'],10);self.assertFalse(row['busy'])
        self.assertEqual(job['total'],10);self.assertEqual(job['state'],'paused')
        self.assertIsNone(job['manual_token'])

    async def test_back_from_search_preserves_original_group_progress(self):
        from descargas_app.manual_gallery import switch_view
        await self.pool.execute('UPDATE descarga_manual_boards SET position=1')
        original=await self.row()
        search=await switch_view(self.s,original,12,-100123,'Santiago')
        self.assertEqual(self.s.repo.decode(search['payload'])['query'],'Santiago')
        restored=await switch_view(self.s,search,12,-100123,None)
        self.assertEqual(restored['position'],1)
        self.assertEqual(self.s.repo.decode(restored['payload'])['entries'],self.data['entries'])

    async def test_batch_reaches_next_page_and_stops_after_ten_not_at_page_boundary(self):
        self.data.update(lazy=True,complete=False)
        await self.pool.execute('UPDATE descarga_manual_boards SET payload=$1',self.s.repo.encode(self.data))
        await b.enqueue(self.s,99,'token',0,50,batch=10)
        with patch('descargas_app.export_inventory.stored',AsyncMock(return_value=True)):
            await b.complete(self.s,await self.job(),[],1)
            await b.complete(self.s,await self.job(),[],1)
        self.assertEqual((await self.job())['phase'],'manual_load')
        self.assertTrue((await self.row())['busy'])
        async def page(s,actor,uid,source,data):
            data['entries'] += [dict(self.data['entries'][1],mid=i,ids=[i]) for i in range(100,110)]
            data['complete']=True
            return data
        with patch.object(b,'collect',page):await b.load_batch(self.s,await self.job())
        self.assertEqual((await self.job())['retry_message'],100)
        with patch('descargas_app.export_inventory.stored',AsyncMock(return_value=True)):
            for _ in range(8):await b.complete(self.s,await self.job(),[],1)
        self.assertEqual((await self.row())['position'],10)
        self.assertFalse((await self.row())['busy'])
        self.assertEqual((await self.job())['total'],10)

    async def test_numbered_selection_downloads_only_target_and_preserves_cursor(self):
        self.data['mode']='photo';self.data['complete']=True
        self.data['entries']=[dict(self.data['entries'][1],mid=i,ids=[i]) for i in range(1,61)]
        await self.pool.execute('UPDATE descarga_manual_boards SET payload=$1,position=3',self.s.repo.encode(self.data))
        await b.enqueue(self.s,99,'token',3,50,number=53)
        self.assertEqual((await self.job())['retry_message'],53)
        self.assertEqual((await self.row())['position'],52)
        with patch('descargas_app.export_inventory.stored',AsyncMock(return_value=True)):
            await b.complete(self.s,await self.job(),[],1)
        row=await self.row();data=self.s.repo.decode(row['payload'])
        self.assertEqual(row['position'],3)
        self.assertEqual(data['entries'][52]['status'],'saved')
        self.assertEqual(data['entries'][51]['status'],'pending')
        self.assertEqual((await self.job())['state'],'paused')

    async def test_numbered_selection_loads_unseen_pages_without_downloading_preceding_entries(self):
        self.data.update(mode='photo',lazy=True,complete=False)
        await self.pool.execute('UPDATE descarga_manual_boards SET payload=$1',self.s.repo.encode(self.data))
        await b.enqueue(self.s,99,'token',0,50,number=53)
        self.assertEqual((await self.job())['phase'],'manual_load')
        self.assertEqual((await self.job())['retry_message'],0)
        async def page(s,actor,uid,source,data):
            data['entries']=[dict(self.data['entries'][1],mid=i,ids=[i]) for i in range(1,61)]
            data['complete']=True
            return data
        with patch.object(b,'collect',page):await b.load_batch(self.s,await self.job())
        self.assertEqual((await self.job())['retry_message'],53)
        self.assertEqual((await self.job())['total'],0)
        with patch('descargas_app.export_inventory.stored',AsyncMock(return_value=True)):
            await b.complete(self.s,await self.job(),[],1)
        self.assertEqual((await self.row())['position'],0)

    async def test_missing_number_is_reported_without_downloading_a_different_element(self):
        self.data.update(mode='photo',lazy=True,complete=False)
        await self.pool.execute('UPDATE descarga_manual_boards SET payload=$1',self.s.repo.encode(self.data))
        await b.enqueue(self.s,99,'token',0,50,number=53)
        async def page(s,actor,uid,source,data):data['complete']=True;return data
        with patch.object(b,'collect',page):await b.load_batch(self.s,await self.job())
        self.assertEqual((await self.job())['state'],'paused')
        self.assertEqual((await self.job())['total'],0)
        row=await self.row();self.assertFalse(row['busy']);self.assertEqual(row['position'],0)
        self.assertIn('no existe',self.s.repo.decode(row['payload'])['notice'])

    async def test_category_switch_restores_separate_progress_for_each_type(self):
        from descargas_app.manual_gallery import switch_view
        row=await self.row()
        photo=await switch_view(self.s,row,12,-100123,None,'photo')
        data=self.s.repo.decode(photo['payload']);data['entries']=self.data['entries'];data['mode']='photo'
        photo=dict(photo,payload=self.s.repo.encode(data),position=1)
        video=await switch_view(self.s,photo,12,-100123,None,'video')
        restored=await switch_view(self.s,video,12,-100123,None,'photo')
        self.assertEqual(restored['position'],1)
        self.assertEqual(self.s.repo.decode(restored['payload'])['mode'],'photo')

    async def test_failed_numbered_selection_returns_to_previous_cursor(self):
        self.data.update(mode='photo',complete=True)
        self.data['entries']=[dict(self.data['entries'][1],mid=i,ids=[i]) for i in range(1,61)]
        await self.pool.execute('UPDATE descarga_manual_boards SET payload=$1,position=3',self.s.repo.encode(self.data))
        await b.enqueue(self.s,99,'token',3,50,number=53)
        await self.pool.execute("UPDATE descarga_group_exports SET state='paused'")
        await b.failed(self.s,await self.job(),'Telegram exige esperar')
        row=await self.row();data=self.s.repo.decode(row['payload'])
        self.assertEqual(row['position'],3);self.assertFalse(row['busy'])
        self.assertNotIn('number_request',data);self.assertNotIn('return_position',data)
        self.assertEqual(data['entries'][52]['status'],'pending')
