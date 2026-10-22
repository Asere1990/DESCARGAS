import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from test_manual_board import Pool
from descargas_app import manual_types as t
from descargas_app.manual_progress import ManualProgress


def photo(mid,caption=''):
    return NS(id=mid,photo=NS(id=mid),document=None,sticker=None,gif=None,message=caption,entities=[],file=NS(size=100))


class TypedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool=Pool();self.addCleanup(self.pool.db.close)
        self.settings={}
        async def setting(k):return self.settings.get(k)
        async def put(k,v):self.settings[k]=v
        self.s=NS(bot_id=7,repo=NS(pool=self.pool,encode=lambda d:json.dumps(d).encode(),decode=lambda d:json.loads(d),setting=setting,set_setting=put),api=AsyncMock(),tell=AsyncMock())
        self.data={'generation':4,'title':'Grupo','mode':'photo','entries':[{'mid':20,'ids':[20],'photos':1,'videos':0,'gifs':0,'elements':1,'files':[['photo',20]],'status':'pending'}]}
        await self.pool.execute('INSERT INTO descarga_manual_boards(bot_id,uid,source,actor,token,payload,message_id,busy) VALUES($1,$2,$3,$4,$5,$6,$7,TRUE)',7,12,-100123,99,'token',self.s.repo.encode(self.data),50)
        await self.pool.execute("INSERT INTO descarga_group_exports(bot_id,uid,source,actor,generation,state,phase,retry_message,manual_token) VALUES(7,12,-100123,99,4,'running','retry',20,'token')")
        self.job={'uid':12,'source':-100123,'retry_message':20,'manual_token':'token','scan':0}

    async def test_single_photo_keeps_album_caption_without_sending_other_members(self):
        original=photo(20);first=photo(10,'Caption del álbum')
        key,items=await t.select_items(self.s,self.job,(-100123,888),[first,original])
        self.assertEqual(key,(-100123,20));self.assertEqual([m.id for m in items],[20])
        self.assertEqual(items[0].message,'Caption del álbum')
        self.assertEqual(original.message,'')

    async def test_video_selector_rejects_photo_and_album_selector_rejects_single_item(self):
        for mode in ('video','album'):
            self.data['mode']=mode
            await self.pool.execute('UPDATE descarga_manual_boards SET payload=$1',self.s.repo.encode(self.data))
            with self.assertRaises(ValueError):await t.select_items(self.s,self.job,(-100123,888),[photo(20)])

    async def test_stale_job_cannot_select_another_file(self):
        with self.assertRaises(ValueError):await t.select_items(self.s,dict(self.job,retry_message=21),(-100123,888),[photo(20)])

    async def test_number_input_routes_to_own_type_and_validates_owner(self):
        await self.pool.execute('UPDATE descarga_manual_boards SET busy=FALSE')
        self.settings['manual-number:7:99']=json.dumps({'token':'token','message_id':50,'generation':4,'mode':'video','expires':9999999999})
        with patch.object(t,'authorized',return_value=True),patch('descargas_app.manual_board.open_board',AsyncMock()) as opened,patch('descargas_app.manual_board.enqueue',AsyncMock()) as queued:
            self.assertTrue(await t.number_input(self.s,99,'53'))
            opened.assert_awaited_once_with(self.s,99,12,-100123,query=None,mode='video')
            queued.assert_awaited_once_with(self.s,99,'token',0,50,number=53)
        self.assertEqual(self.settings['manual-number:7:99'],'')

    async def test_percentage_uses_real_combined_bytes_and_never_exceeds_100(self):
        progress=ManualProgress(self.s,self.job,[photo(1),NS(file=NS(size=300))])
        progress.update(0,100,100);progress.update(1,200,300)
        progress.update(0,40,100,upload=True)
        self.assertIn('75%',progress.text());self.assertIn('10%',progress.text())
        progress.update(1,999,300);progress.update(1,999,300,upload=True)
        self.assertIn('100%',progress.text())
        self.assertNotIn('250%',progress.text())

    async def test_progress_edits_same_panel_without_rewriting_snapshot_and_stale_publish_is_fenced(self):
        progress=ManualProgress(self.s,self.job,[photo(20)])
        progress.update(0,50,100)
        before=(await self.pool.fetchrow('SELECT payload FROM descarga_manual_boards'))['payload']
        await progress.publish()
        self.assertEqual(self.s.api.call_args.args[0],'editMessageText')
        self.assertEqual(self.s.api.call_args.args[1]['message_id'],50)
        self.assertIn('50%',self.s.api.call_args.args[1]['text'])
        after=(await self.pool.fetchrow('SELECT payload FROM descarga_manual_boards'))['payload']
        self.assertEqual(before,after)
        self.s.api.reset_mock();progress.update(0,80,100)
        await self.pool.execute('UPDATE descarga_group_exports SET scan=1')
        await progress.publish();self.s.api.assert_not_awaited()
