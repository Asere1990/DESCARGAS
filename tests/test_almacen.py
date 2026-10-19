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
            service.bot=SimpleNamespace(send_file=AsyncMock(return_value=[object(),object()]),disconnect=AsyncMock())
            delivered=[object(),object()]
            try:
                await service.archive(10,delivered)
                args=service.bot.send_file.call_args
                self.assertEqual(args.args,(-100123456,delivered))
                self.assertEqual(args.kwargs['caption'],['Usuario: Test User\nTelegram ID: 10',''])
                self.assertEqual(len(args.kwargs['formatting_entities'][0]),2)
                self.assertIsNone(args.kwargs['parse_mode'])
            finally:await service.close()
    async def test_single_caption(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'ALMACEN':'-100123456','DESCARGAS_DATA_DIR':directory}):
            service=Service()
            from types import SimpleNamespace
            service.bot=SimpleNamespace(send_file=AsyncMock(return_value=object()),disconnect=AsyncMock())
            try:
                item=object();await service.archive(20,[item])
                self.assertIs(service.bot.send_file.call_args.args[1],item)
                self.assertIsInstance(service.bot.send_file.call_args.kwargs['caption'],str)
            finally:await service.close()
