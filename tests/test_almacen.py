import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
from descargas_app.service import Service,archive_caption

class Caption(unittest.TestCase):
    def test_unicode_name_and_clickable_id(self):
        text,entities=archive_caption('Rafelito 🇨🇺 😀',1609522430)
        encoded=text.encode('utf-16-le')
        self.assertEqual(len(entities),2)
        for entity,want in zip(entities,['Rafelito 🇨🇺 😀','1609522430']):
            self.assertEqual(encoded[entity.offset*2:(entity.offset+entity.length)*2].decode('utf-16-le'),want)
            self.assertEqual(entity.url,'tg://user?id=1609522430')
    def test_name_cannot_inject_formatting(self):
        text,entities=archive_caption('<b>name</b>\nnew line',10)
        self.assertIn('<b>name</b> new line',text)

class Archive(unittest.IsolatedAsyncioTestCase):
    async def test_album_caption_and_no_reupload(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'ALMACEN':'-100123456','DESCARGAS_DATA_DIR':directory}):
            service=Service();service.names[10]='Test User'
            from types import SimpleNamespace
            service.api=AsyncMock(side_effect=[[{'message_id':501},{'message_id':502}],{'username':'Test_User'},{}])
            try:
                await service.archive(10,[SimpleNamespace(id=101),SimpleNamespace(id=102)])
                copy,profile,caption=service.api.call_args_list
                self.assertEqual(copy.args,('copyMessages',{'chat_id':-100123456,'from_chat_id':10,'message_ids':[101,102],'remove_caption':False}))
                self.assertEqual(caption.args[1]['message_id'],501)
                self.assertEqual(caption.args[1]['caption'],'Usuario: Test User\nTelegram ID: 10')
                self.assertEqual(len(caption.args[1]['caption_entities']),2)
                for entity in caption.args[1]['caption_entities']:
                    self.assertEqual(entity['type'],'text_link')
                    self.assertEqual(entity['url'],'https://t.me/Test_User')
                self.assertIsNone(service.bot)
            finally:await service.close()
    async def test_partial_album_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'ALMACEN':'-100123456','DESCARGAS_DATA_DIR':directory}):
            service=Service()
            from types import SimpleNamespace
            service.api=AsyncMock(return_value=[])
            try:
                with self.assertRaises(ValueError):await service.archive(20,[SimpleNamespace(id=101)])
                self.assertEqual(service.api.await_count,1)
            finally:await service.close()
