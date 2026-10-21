import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from telethon import types
from descargas_app.service import apply_archive_caption

class ArchiveCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_format_and_clickable_identity_keep_utf16_offsets(self):
        s=NS(almacen=-100123,api=AsyncMock())
        original=NS(message='📥 Texto original',entities=[types.MessageEntityBold(offset=3,length=5)])
        attribution={'caption':'Usuario: Juan','caption_entities':[{'type':'text_link','offset':9,'length':4,'url':'tg://user?id=1'}]}
        await apply_archive_caption(s,20,attribution,original)
        method,payload=s.api.call_args.args
        self.assertEqual(method,'editMessageCaption')
        self.assertEqual(payload['caption'],'📥 Texto original\n\nUsuario: Juan')
        self.assertEqual(payload['caption_entities'][0],{'type':'bold','offset':3,'length':5})
        self.assertEqual(payload['caption_entities'][1]['offset'],9+len('📥 Texto original\n\n'.encode('utf-16-le'))//2)
    async def test_long_caption_is_left_intact_and_identity_sent_as_reply(self):
        s=NS(almacen=-100123,api=AsyncMock())
        await apply_archive_caption(s,20,{'caption':'Usuario: Juan','caption_entities':[]},NS(message='x'*1020,entities=[]))
        method,payload=s.api.call_args.args
        self.assertEqual(method,'sendMessage');self.assertEqual(payload['reply_parameters'],{'message_id':20})
        self.assertEqual(payload['text'],'Usuario: Juan')
