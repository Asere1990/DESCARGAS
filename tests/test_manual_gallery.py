import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from telethon import types,errors
from descargas_app import manual_gallery as g


def message(mid,kind='photo',group=None,parent=None):
    return NS(id=mid,media=None,grouped_id=group,gif=True if kind=='gif' else None,sticker=None,
        photo=NS(id=mid) if kind=='photo' else None,
        document=NS(id=mid,mime_type='image/jpeg' if kind=='image' else 'video/mp4' if kind in ('video','gif') else 'application/pdf') if kind in ('image','video','gif','pdf') else None,
        reply_to=NS(reply_to_msg_id=parent) if parent else None)


class Client:
    def __init__(self,history):
        self.connect=AsyncMock();self.disconnect=AsyncMock();self.is_user_authorized=AsyncMock(return_value=True)
        self.get_me=AsyncMock(return_value=types.User(id=12,is_self=True,first_name='Test'))
        self.get_messages=AsyncMock(return_value=[])
        self.history=history;self.calls=[];self.counter_calls=0
    async def __call__(self,request):
        self.counter_calls+=1
        return [NS(count=n) for n in (1000,2000,3,50)]
    async def iter_messages(self,entity,**kwargs):
        self.calls.append(kwargs)
        for m in self.history(kwargs):yield m


class GalleryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings={}
        async def setting(k):return self.settings.get(k)
        async def put(k,v):self.settings[k]=v
        @asynccontextmanager
        async def lock(*args,**kw):yield
        self.s=NS(bot_id=7,almacen=-100999,repo=NS(account=AsyncMock(return_value={'session':b'x','generation':4}),
            setting=setting,set_setting=put,encode=lambda d:json.dumps(d).encode(),decode=lambda d:json.loads(d),
            pool=NS(fetch=AsyncMock(return_value=[]),fetchval=AsyncMock(return_value=0)),account_lock=lock,pace=AsyncMock()),
            get_session=AsyncMock(return_value='session'),resolve=AsyncMock(return_value=types.Channel(id=123,title='Grupo',photo=types.ChatPhotoEmpty(),date=None)))
        self.patches=[patch.object(g,'authorized',return_value=True),patch.object(g,'ensure_allowed',AsyncMock()),patch('descargas_app.group_download.users_busy',AsyncMock(return_value=False))]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    async def test_filtered_pages_are_bounded_merge_album_and_resume_offsets(self):
        def history(kw):
            if not isinstance(kw['filter'],types.InputMessagesFilterPhotoVideo):return []
            return [message(i,group=88 if i<=52 else None) for i in range(100,50,-1)] if not kw['offset_id'] else [message(50,group=88)]
        c=Client(history);self.s.client=lambda session:c
        data=await g.collect_page(self.s,99,12,-1000000000123)
        self.assertFalse(data['complete']);self.assertEqual(len(c.calls),3)
        self.assertTrue(all('filter' in kw and kw['limit']==50 for kw in c.calls))
        self.assertEqual(c.counter_calls,1);self.assertEqual(data['totals']['photos'],1000)
        await self.s.repo.set_setting(g.view_key(self.s,12,-1000000000123,None)+':page','')
        data=await g.collect_page(self.s,99,12,-1000000000123,data)
        self.assertTrue(data['complete']);self.assertEqual(c.calls[-1]['offset_id'],51)
        self.assertEqual(next(e for e in data['entries'] if e['group']==88)['elements'],3)
        self.assertEqual(c.counter_calls,1);self.s.resolve.assert_awaited_once()

    async def test_search_caption_and_reply_parent_only_includes_media_deduplicated(self):
        c=Client(lambda kw:[message(10,'photo',77),message(20,None,parent=10),message(21,None),message(22,'pdf')])
        c.get_messages.return_value=[message(10,'photo',77)]
        self.s.client=lambda session:c
        data=await g.collect_page(self.s,99,12,-1000000000123,{'title':'Grupo','generation':4,'entries':[],'query':'Santiago'})
        self.assertEqual(len(data['entries']),1);self.assertEqual(data['entries'][0]['elements'],1)
        self.assertEqual(c.calls[0]['search'],'Santiago');self.assertIsInstance(c.calls[0]['filter'],types.InputMessagesFilterEmpty)
        self.assertEqual(c.counter_calls,0)

    async def test_saved_messages_use_own_identity_and_include_image_documents(self):
        c=Client(lambda kw:[message(10,'image')] if isinstance(kw['filter'],types.InputMessagesFilterDocument) else [])
        self.s.client=lambda session:c
        data=await g.collect_page(self.s,99,12,12)
        self.assertEqual(data['title'],'Mensajes guardados');self.assertEqual(data['entries'][0]['photos'],1)
        self.s.resolve.assert_not_awaited()
        c.get_me.return_value=types.User(id=13,first_name='Wrong')
        with self.assertRaises(ValueError):await g.entity_for(self.s,c,12,12,4)

    async def test_dm_priority_does_not_open_session(self):
        self.s.client=AsyncMock()
        with patch('descargas_app.group_download.users_busy',AsyncMock(return_value=True)):
            with self.assertRaisesRegex(ValueError,'DM'):await g.collect_page(self.s,99,12,-1000000000123)
        self.s.client.assert_not_called()

    async def test_flood_resumes_successful_filter_checkpoint_without_starting_over(self):
        fail=True
        def history(kw):
            if isinstance(kw['filter'],types.InputMessagesFilterGif) and fail:raise errors.FloodWaitError(request=None,capture=61)
            return [message(10)] if isinstance(kw['filter'],types.InputMessagesFilterPhotoVideo) else []
        c=Client(history);self.s.client=lambda session:c
        with self.assertRaises(errors.FloodWaitError):await g.collect_page(self.s,99,12,-1000000000123)
        fail=False
        data=await g.collect_page(self.s,99,12,-1000000000123)
        self.assertEqual(len(data['entries']),1);self.assertEqual(c.counter_calls,1)
        self.assertEqual(sum(isinstance(k['filter'],types.InputMessagesFilterPhotoVideo) for k in c.calls),1)

    async def test_photo_mode_lists_individual_album_photos_and_image_documents_only(self):
        def history(kw):
            if isinstance(kw['filter'],types.InputMessagesFilterPhotos):return [message(10,'photo',77),message(9,'photo',77)]
            return [message(8,'image'),message(7,'video'),message(6,'gif'),message(5,'pdf')]
        c=Client(history);self.s.client=lambda session:c
        data=await g.collect_page(self.s,99,12,-1000000000123,{'title':'Grupo','generation':4,'entries':[],'mode':'photo'})
        self.assertEqual([e['mid'] for e in data['entries']],[10,9,8])
        self.assertTrue(all(e['elements']==1 and e['group'] is None for e in data['entries']))
        self.assertEqual([type(k['filter']) for k in c.calls],[types.InputMessagesFilterPhotos,types.InputMessagesFilterDocument])

    async def test_video_mode_deduplicates_document_stream_and_excludes_gifs(self):
        c=Client(lambda kw:[message(10,'video',77)] if isinstance(kw['filter'],types.InputMessagesFilterVideo) else [message(10,'video',77),message(9,'gif'),message(8,'image')])
        self.s.client=lambda session:c
        data=await g.collect_page(self.s,99,12,-1000000000123,{'title':'Grupo','generation':4,'entries':[],'mode':'video'})
        self.assertEqual(len(data['entries']),1);self.assertEqual(data['entries'][0]['elements'],1)
        self.assertIsNone(data['entries'][0]['group'])

    async def test_album_mode_requires_two_items_and_merges_category_boundaries(self):
        c=Client(lambda kw:[message(10,'photo',77),message(8,'photo'),message(7,'photo',88)] if isinstance(kw['filter'],types.InputMessagesFilterPhotoVideo) else [message(9,'image',77)] if isinstance(kw['filter'],types.InputMessagesFilterDocument) else [])
        self.s.client=lambda session:c
        data=await g.collect_page(self.s,99,12,-1000000000123,{'title':'Grupo','generation':4,'entries':[],'mode':'album'})
        self.assertEqual(len(data['entries']),1)
        self.assertEqual(data['entries'][0]['group'],77)
        self.assertEqual(data['entries'][0]['elements'],2)
        self.assertIn('88',data['album_parts'])
