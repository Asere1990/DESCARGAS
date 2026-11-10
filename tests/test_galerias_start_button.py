"""Regresión de menú /start para CUENTA CREADA y SUSCRIPCIÓN ACTIVADA.

Verifica la inserción central incluso cuando Billing.update intercepta /start.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from descargas_app.scalable import (
    ScaleService, START_MENU_UID, ensure_gallery_start_button
)


class GalleryStartButton(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patcher=patch.dict("os.environ",{
            "GALERIAS_PUBLIC":"0",
            "GALERIAS_URL":"https://cloudflare-t2gf.onrender.com/web-galerias"})
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()

    def test_start_with_admin_inserts_button_without_replacing_existing(self):
        token=START_MENU_UID.set(7539025033)
        try:
            original={"chat_id":7539025033,"text":"👤 CUENTA CREADA",
                "reply_markup":{"inline_keyboard":[
                    [{"text":"👤 MI CUENTA","web_app":{"url":"https://example.org"}}],
                    [{"text":"PROTECCIÓN A CREADORES","url":"https://example.org/p"}]]}}
            new=ensure_gallery_start_button(original)
            self.assertEqual(len(new["reply_markup"]["inline_keyboard"]),3)
            self.assertEqual(len(original["reply_markup"]["inline_keyboard"]),2)
            self.assertEqual(new["reply_markup"]["inline_keyboard"][-1][0]["text"],"🖼 WEB GALERÍAS")
        finally:
            START_MENU_UID.reset(token)

    def test_all_three_pilot_users_get_button(self):
        for uid in (1609522430,7536216348,5662710947):
            token=START_MENU_UID.set(uid)
            try:
                original={"chat_id":uid,"text":"CUENTA CREADA"}
                modified=ensure_gallery_start_button(original)
                self.assertEqual(modified["reply_markup"]["inline_keyboard"][0][0]["text"],
                                 "🖼 WEB GALERÍAS")
            finally:
                START_MENU_UID.reset(token)

    def test_non_pilot_user_not_modified(self):
        uid=987654321
        token=START_MENU_UID.set(uid)
        try:
            original={"chat_id":uid,"text":"CUENTA CREADA"}
            self.assertIs(ensure_gallery_start_button(original),original)
        finally:
            START_MENU_UID.reset(token)

    def test_paid_menu_preserves_cancel_button(self):
        token=START_MENU_UID.set(1609522430)
        try:
            original={"chat_id":1609522430,"text":"SUSCRIPCIÓN ACTIVADA",
                "reply_markup":{"inline_keyboard":[[{"text":"CANCELAR MEMBRESÍA",
                    "callback_data":"membership:cancel","style":"danger"}]]}}
            updated=ensure_gallery_start_button(original)
            self.assertEqual(updated["reply_markup"]["inline_keyboard"][0],
                             original["reply_markup"]["inline_keyboard"][0])
            self.assertEqual(updated["reply_markup"]["inline_keyboard"][1][0]["text"],
                             "🖼 WEB GALERÍAS")
        finally:
            START_MENU_UID.reset(token)

    def test_does_not_duplicate_preexisting_gallery_button(self):
        token=START_MENU_UID.set(7539025033)
        try:
            original={"chat_id":7539025033,"reply_markup":{"inline_keyboard":[[
                {"text":"🖼 WEB GALERÍAS","web_app":{"url":"https://example.org"}}]]}}
            self.assertIs(ensure_gallery_start_button(original),original)
        finally:
            START_MENU_UID.reset(token)

    async def test_billing_interception_still_has_start_context_and_resets(self):
        service=object.__new__(ScaleService)
        events=[]
        async def handled(self,update):
            events.append(ensure_gallery_start_button(
                {"chat_id":1609522430,"text":"SUSCRIPCIÓN ACTIVADA"}))
        update={"message":{"from":{"id":1609522430},"chat":{
            "type":"private","id":1609522430},"text":"/start"}}
        with patch.object(ScaleService,"_update_without_gallery_context",new=handled):
            await service.update(update)
        self.assertEqual(events[0]["reply_markup"]["inline_keyboard"][0][0]["text"],
                         "🖼 WEB GALERÍAS")
        self.assertIsNone(START_MENU_UID.get())

    async def test_no_text_or_callback_does_not_crash(self):
        service=object.__new__(ScaleService)
        events=[]
        async def handled(self,update):
            events.append(START_MENU_UID.get())
        with patch.object(ScaleService,"_update_without_gallery_context",new=handled):
            await service.update({"callback_query":{"from":{"id":1609522430}}})
            await service.update({"message":{"from":{"id":1609522430},
                                                "chat":{"type":"private"}}})
        self.assertEqual(events,[None,None])

    async def test_real_bot_api_keeps_authorized_button(self):
        class FakeHttp:
            request=None
            def post(self,url,json):
                self.request=json
                class Response:
                    async def __aenter__(self):
                        return SimpleNamespace(json=AsyncMock(return_value={
                            "ok":True,"result":{"message_id":1}}))
                    async def __aexit__(self,*args):
                        return False
                return Response()
        http=FakeHttp()
        fake=SimpleNamespace(
            http=http,token="FAKE",
            repo=SimpleNamespace(pace=AsyncMock(),try_pace=AsyncMock(return_value=True)))
        token=START_MENU_UID.set(5662710947)
        try:
            await ScaleService.api(fake,"sendMessage",{"chat_id":5662710947,
                          "text":"👤 CUENTA CREADA","reply_markup":{
                              "inline_keyboard":[[{"text":"👤 MI CUENTA","url":"https://example.com"}]]}})
        finally:
            START_MENU_UID.reset(token)
        self.assertEqual([r[0]["text"] for r in
            http.request["reply_markup"]["inline_keyboard"]],
            ["👤 MI CUENTA","🖼 WEB GALERÍAS"])


if __name__=="__main__":
    unittest.main()
