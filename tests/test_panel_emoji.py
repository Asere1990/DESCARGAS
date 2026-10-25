import gzip
import io
import json
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from datetime import datetime,timezone
from PIL import Image
from telethon import types
from descargas_app.panel_emoji import PremiumEmoji,tgs_animation,video_webp
from tests import test_web_actions as fixtures

class EmojiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ActionsTests.asyncSetUp(self)
        m=self.client.get_messages.return_value;m.entities=[types.MessageEntityCustomEmoji(0,2,88)]
        self.doc=types.Document(88,12,b'ref',datetime.now(timezone.utc),'application/x-tgsticker',64,1,[types.DocumentAttributeCustomEmoji('❤️',types.InputStickerSetEmpty())])
        self.client.rpc.return_value=[self.doc];self.panel.features.check=AsyncMock()
        self.animation={'v':'5.7.4','fr':30,'ip':0,'op':90,'w':100,'h':100,'layers':[]}
        self.data=gzip.compress(json.dumps(self.animation).encode());self.client.download_media.return_value=self.data
    async def test_native_custom_emoji_document_is_resolved_and_tgs_played_as_lottie(self):
        mime,data=await self.panel.emoji.asset(111,22,self.peer,9,88)
        self.assertEqual(mime,'application/json');self.assertEqual(json.loads(data)['animation'],self.animation);self.assertEqual(json.loads(data)['format'],'lottie')
        self.panel.features.check.assert_awaited_once();self.client.download_media.assert_awaited_once()
        await self.panel.emoji.asset(111,22,self.peer,9,88);self.assertEqual(self.client.download_media.await_count,1)
    async def test_cache_never_bypasses_current_message_scope(self):
        await self.panel.emoji.asset(111,22,self.peer,9,88)
        self.client.get_messages.return_value.entities=[]
        with self.assertRaisesRegex(ValueError,'pertenece'):await self.panel.emoji.asset(111,22,self.peer,9,88)
        self.client.get_messages.return_value.entities=[types.MessageEntityCustomEmoji(0,2,88)];self.client.get_messages.return_value.peer_id=types.PeerUser(99)
        with self.assertRaisesRegex(ValueError,'pertenece'):await self.panel.emoji.asset(111,22,self.peer,9,88)
    async def test_custom_reaction_can_render_its_premium_emoji(self):
        m=self.client.get_messages.return_value;m.entities=[];m.reactions=N(results=[N(reaction=types.ReactionCustomEmoji(88))])
        mime,data=await self.panel.emoji.asset(111,22,self.peer,9,88);self.assertEqual(mime,'application/json')
    async def test_static_emoji_uses_original_webp_with_transparency(self):
        image=Image.new('RGBA',(100,100),(255,0,0,80));out=io.BytesIO();image.save(out,format='WEBP');data=out.getvalue()
        self.doc.mime_type='image/webp';self.doc.size=len(data);self.client.download_media.return_value=data
        mime,body=await self.panel.emoji.asset(111,22,self.peer,9,88);self.assertEqual(mime,'image/webp');self.assertEqual(body,data)
    async def test_video_emoji_is_converted_to_iphone_compatible_animation(self):
        self.doc.mime_type='video/webm';self.client.download_media.return_value=b'native-video'
        with patch('descargas_app.panel_emoji.video_webp',AsyncMock(return_value=b'animated-webp')) as convert:
            mime,data=await self.panel.emoji.asset(111,22,self.peer,9,88)
        self.assertEqual(mime,'image/webp');convert.assert_awaited_once_with(b'native-video')
    async def test_tgs_rejects_external_assets_and_decompression_bombs(self):
        value={**self.animation,'assets':[{'p':'https://untrusted.invalid/tracker.png'}]}
        with self.assertRaisesRegex(ValueError,'externas'):tgs_animation(gzip.compress(json.dumps(value).encode()))
        with self.assertRaises(ValueError):tgs_animation(gzip.compress(b' '*5*1024*1024))
    async def test_temporary_messages_and_non_emoji_documents_are_never_served(self):
        self.client.get_messages.return_value.ttl_period=30
        with self.assertRaisesRegex(ValueError,'temporal'):await self.panel.emoji.asset(111,22,self.peer,9,88)
        self.client.get_messages.return_value.ttl_period=None;self.doc.attributes=[]
        with self.assertRaisesRegex(ValueError,'disponible'):await self.panel.emoji.asset(111,22,self.peer,9,88)
