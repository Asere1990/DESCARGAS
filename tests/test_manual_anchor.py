import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from telethon import types
from test_manual_board import Pool
from descargas_app import manual_anchor as a,manual_board as b


def msg(mid,kind='photo',group=None):
    return NS(id=mid,media=None,grouped_id=group,sticker=None,gif=True if kind=='gif' else None,
        photo=NS(id=mid) if kind=='photo' else None,
        document=NS(id=mid,mime_type='image/jpeg' if kind=='image' else 'application/pdf') if kind in ('image','pdf') else None)


class Client:
    def __init__(self,history):
        self.history=history;self.calls=[]
        self.connect=AsyncMock();self.disconnect=AsyncMock();self.is_user_authorized=AsyncMock(return_value=True)
    async def iter_messages(self,entity,**kwargs):
        self.calls.append(kwargs)
        for m in self.history(kwargs):yield m


class AnchorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool=Pool();self.addCleanup(self.pool.db.close);self.settings={}
        async def setting(k):return self.settings.get(k)
        async def put(k,v):self.settings[k]=v
        @asynccontextmanager
        async def lock(*args,**kw):yield
        self.s=NS(bot_id=7,almacen=-100999,repo=NS(pool=self.pool,encode=lambda d:json.dumps(d).encode(),decode=lambda d:json.loads(d),setting=setting,set_setting=put,account=AsyncMock(return_value={'session':b'x','generation':4}),account_lock=lock,pace=AsyncMock()),api=AsyncMock(return_value={'message_id':50}),tell=AsyncMock(),get_session=AsyncMock(return_value='session'),resolve=AsyncMock(return_value=types.Channel(id=123,title='Grupo',photo=types.ChatPhotoEmpty(),date=None)))
        self.base={'generation':4,'title':'Grupo','entries':[b.entry(msg(i),[msg(i)]) for i in (10,20,30)],'complete':True}
        await self.pool.execute('INSERT INTO descarga_manual_boards(bot_id,uid,source,actor,token,payload,message_id,position) VALUES($1,$2,$3,$4,$5,$6,$7,$8)',7,12,-1000000000123,99,'board',self.s.repo.encode(self.base),50,1)
        self.patches=[patch.object(a,'authorized',return_value=True),patch.object(b,'authorized',return_value=True),patch('descargas_app.manual_gallery.authorized',return_value=True),patch.object(a,'ensure_allowed',AsyncMock()),patch('descargas_app.group_download.users_busy',AsyncMock(return_value=False))]
        for p in self.patches:p.start();self.addCleanup(p.stop)
    async def row(self):return await self.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=7')
    async def job(self):return await self.pool.fetchrow('SELECT * FROM descarga_group_exports WHERE bot_id=7')
    async def query(self,action):
        row=await self.row()
        return {'data':f'manual:{action}:{row["token"]}:{row["position"]}','message':{'chat':{'id':99},'message_id':row['message_id']}}
    async def open(self):
        await a.callback(self.s,99,await self.query('as'))
        self.assertTrue(await a.input_link(self.s,99,'https://t.me/c/0000000123/100'))
    def data(self,direction=1):
        return {'generation':4,'title':'Grupo','mode':'anchor','query':'anchor:100','anchor_ready':True,'anchor_cursor':100,'anchor_start':100,'anchor_direction':direction,'anchor_inclusive':True,'entries':[]}

    async def test_link_opens_four_buttons_without_reading_whole_history_and_back_restores_cursor(self):
        self.s.client=AsyncMock()
        await self.open();self.s.client.assert_not_called()
        row=await self.row();data=self.s.repo.decode(row['payload'])
        text,buttons=b.presentation(row,data)
        self.assertIn('comenzando desde aquí',text)
        self.assertEqual([len(row) for row in buttons],[2,1,1])
        await a.callback(self.s,99,await self.query('az'))
        self.assertEqual((await self.row())['position'],1)
        self.assertEqual(self.s.repo.decode((await self.row())['payload'])['entries'],self.base['entries'])

    async def test_rejects_link_from_another_source_and_expired_or_stale_prompt(self):
        await a.callback(self.s,99,await self.query('as'))
        with self.assertRaisesRegex(ValueError,'pertenecer'):await a.input_link(self.s,99,'https://t.me/c/999/100')
        self.assertEqual((await self.row())['token'],'board')
        pending=json.loads(self.settings[a.pending_key(self.s,99)]);pending['expires']=0
        self.settings[a.pending_key(self.s,99)]=json.dumps(pending)
        self.assertFalse(await a.input_link(self.s,99,'https://t.me/c/0000000123/100'))

    async def test_forward_and_backward_include_initial_point_and_use_media_filters(self):
        for direction in (1,-1):
            c=Client(lambda kw:[msg(100),msg(102 if direction==1 else 98)] if isinstance(kw['filter'],types.InputMessagesFilterPhotoVideo) else [])
            self.s.client=lambda session:c
            data=await a.collect(self.s,99,12,-1000000000123,self.data(direction))
            self.assertEqual([e['mid'] for e in data['entries']],[100,102 if direction==1 else 98])
            self.assertTrue(data['complete'])
            for kw in c.calls:
                self.assertIn('filter',kw)
                self.assertEqual(kw['min_id'] if direction==1 else kw['max_id'],99 if direction==1 else 101)

    async def test_document_gaps_do_not_jump_over_nearer_image_and_only_limiting_stream_advances(self):
        def history(kw):
            if isinstance(kw['filter'],types.InputMessagesFilterPhotoVideo):return [msg(i) for i in range(500,550)]
            if isinstance(kw['filter'],types.InputMessagesFilterDocument):
                return [msg(i,'pdf') for i in range(100,150)] if kw['min_id']==99 else [msg(150,'image')]
            return []
        c=Client(history);self.s.client=lambda session:c
        data=await a.collect(self.s,99,12,-1000000000123,self.data())
        self.assertEqual(data['entries'],[])
        self.assertEqual(len(data['anchor_buffer']),50)
        data=await a.collect(self.s,99,12,-1000000000123,data)
        self.assertEqual(data['entries'][0]['mid'],150)
        self.assertEqual(sum(isinstance(k['filter'],types.InputMessagesFilterPhotoVideo) for k in c.calls),1)

    async def test_batch_five_updates_actual_cursor_and_next_direction_excludes_last_album(self):
        await self.open()
        await a.callback(self.s,99,await self.query('ax'))
        self.assertEqual((await self.job())['phase'],'manual_load')
        with self.assertRaises(ValueError):await a.callback(self.s,99,await self.query('ax'))
        async def page(s,actor,uid,source,data):
            data['entries']=[b.entry(msg(i),[msg(i)]) for i in range(100,105)];data['complete']=False
            return data
        with patch.object(b,'collect',page):await b.load_batch(self.s,await self.job())
        with patch('descargas_app.export_inventory.stored',AsyncMock(return_value=True)):
            for _ in range(5):await b.complete(self.s,await self.job(),[],1)
        row=await self.row();data=self.s.repo.decode(row['payload'])
        self.assertEqual(data['anchor_cursor'],104);self.assertTrue(data['anchor_started']);self.assertFalse(row['busy'])
        self.assertEqual((await self.job())['total'],5)
        await a.callback(self.s,99,await self.query('ay'))
        data=self.s.repo.decode((await self.row())['payload'])
        self.assertEqual(data['anchor_start'],104);self.assertFalse(data['anchor_inclusive']);self.assertEqual(data['anchor_direction'],-1)

    async def test_album_is_one_link_and_following_batch_skips_its_members(self):
        c=Client(lambda kw:[msg(100,'photo',77),msg(101,'photo',77)] if isinstance(kw['filter'],types.InputMessagesFilterPhotoVideo) else [])
        self.s.client=lambda session:c
        data=await a.collect(self.s,99,12,-1000000000123,self.data())
        self.assertEqual(len(data['entries']),1);self.assertEqual(data['entries'][0]['elements'],2)
        await self.open();await a.callback(self.s,99,await self.query('ax'))
        async def page(s,actor,uid,source,d):d['entries']=data['entries'];d['complete']=True;return d
        with patch.object(b,'collect',page):await b.load_batch(self.s,await self.job())
        items=[msg(i,'photo',77) for i in (99,100,101)]
        with patch('descargas_app.export_inventory.stored',AsyncMock(return_value=True)):
            await b.complete(self.s,await self.job(),items,3)
        saved=self.s.repo.decode((await self.row())['payload'])
        self.assertEqual(saved['anchor_cursor'],100)
        self.assertEqual(set(saved['anchor_skip_ids']),{99,100,101})
        saved.update(entries=[],streams={},anchor_buffer=[],complete=False,anchor_start=100,anchor_inclusive=False)
        c=Client(lambda kw:[msg(101,'photo',77),msg(102)] if isinstance(kw['filter'],types.InputMessagesFilterPhotoVideo) else [])
        self.s.client=lambda session:c
        saved=await a.collect(self.s,99,12,-1000000000123,saved)
        self.assertEqual([e['mid'] for e in saved['entries']],[102])

    async def test_dm_priority_prevents_metadata_session(self):
        self.s.client=AsyncMock()
        with patch('descargas_app.group_download.users_busy',AsyncMock(return_value=True)):
            with self.assertRaisesRegex(ValueError,'DM'):await a.collect(self.s,99,12,-1000000000123,self.data())
        self.s.client.assert_not_called()
