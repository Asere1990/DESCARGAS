import unittest
import unicodedata
from descargas_app.bot_typography import styled_text, format_bot_payload


class BotTypographyTests(unittest.TestCase):
    def test_wording_and_accents(self):
        original = '¡Conexión! País, contraseña: 42.\n¿Listo?'
        formatted = styled_text(original)
        self.assertEqual(unicodedata.normalize('NFKD', formatted), unicodedata.normalize('NFKD', original))
        self.assertEqual(formatted, original)
        self.assertEqual(styled_text('𝘿𝙚𝙨𝙘𝙖𝙧𝙜𝙖𝙨'), 'Descargas')

    def test_serif_and_idempotence(self):
        original = '𝐒𝐔𝐒𝐂𝐑𝐈𝐏𝐂𝐈Ó𝐍 𝐌𝐄𝐍𝐒𝐔𝐀𝐋 · Mi cuenta'
        formatted = styled_text(original)
        self.assertTrue(formatted.startswith('𝐒𝐔𝐒𝐂𝐑𝐈𝐏𝐂𝐈Ó𝐍 𝐌𝐄𝐍𝐒𝐔𝐀𝐋'))
        self.assertEqual(styled_text(formatted), formatted)

    def test_links_commands_and_email(self):
        literal = '/start /datos @Descargas_Robot https://t.me/bot?start=Hola user@example.com'
        self.assertEqual(styled_text(literal), literal)

    def test_html_links_and_literal_code(self):
        original = '<a href="tg://user?id=123">Juan</a> &amp; <code>abcXYZ</code>'
        self.assertEqual(styled_text(original, html=True), '<a href="tg://user?id=123">Juan</a> &amp; <code>abcXYZ</code>')

    def test_entities_follow_utf16_offsets(self):
        payload = {'text': '𝙃𝙤𝙡𝙖 𝙅𝙪𝙖𝙣', 'entities': [{'type': 'text_link', 'offset': 9, 'length': 8, 'url': 'tg://user?id=123'}]}
        result = format_bot_payload('sendMessage', payload)
        self.assertEqual(result['entities'][0]['offset'], 5)
        self.assertEqual(result['entities'][0]['length'], 4)
        self.assertEqual(payload['text'], '𝙃𝙤𝙡𝙖 𝙅𝙪𝙖𝙣')

    def test_buttons_preserve_actions(self):
        button = {'text': 'Continuar →', 'callback_data': 'next', 'style': 'success', 'web_app': {'url': 'https://example.com'}}
        result = format_bot_payload('sendMessage', {'text': 'Hola', 'reply_markup': {'inline_keyboard': [[button]]}})
        formatted = result['reply_markup']['inline_keyboard'][0][0]
        self.assertEqual(formatted['text'], 'Continuar →')
        for key in ('callback_data', 'style', 'web_app'):
            self.assertEqual(formatted[key], button[key])

    def test_media_captions_unchanged(self):
        payload = {'caption': 'Texto original de Juan', 'caption_entities': []}
        self.assertEqual(format_bot_payload('sendPhoto', payload), payload)
