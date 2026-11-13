"""Regresiones del reporte individual de Telegram en la cuenta vinculada."""
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch
from aiohttp import web
from telethon import types, functions
from tests.test_web_panel import PanelTests, message
from descargas_app.web_panel import peer_data


class FakeClient(N):
    async def __call__(self, request):
        return await self.rpc(request)


class ReportTests(PanelTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client=FakeClient(**vars(self.client))
        self.client.rpc=AsyncMock()
        self.entity=types.Channel(
            id=77,access_hash=123,title="Canal de prueba",
            photo=types.ChatPhotoEmpty(),date=None,broadcast=True,
        )
        self.client.get_entity.return_value=self.entity
        m=message(9,text="Mensaje concreto")
        m.peer_id=types.PeerChannel(77)
        self.client.get_messages.return_value=m
        self.peer=self.panel.pack(
            "peer",actor=111,uid=22,generation=3,**peer_data(self.entity,22))
        granted=patch("descargas_app.panel_reports.police",side_effect=lambda actor:actor in self.allowed)
        granted.start();self.addCleanup(granted.stop)
        self.receipts=set()
        async def claim(sql,key,value):
            if key in self.receipts:return None
            self.receipts.add(key)
            return key
        self.pool.fetchval=AsyncMock(side_effect=claim)

    def data(self, **extra):
        return {"account":22,"peer":self.peer,"message":9,**extra}

    async def test_telegram_supplies_motives_and_comment_before_sending(self):
        self.client.rpc.side_effect=[
            types.ReportResultChooseOption(
                title="Motivo",options=[
                    types.MessageReportOption(text="Spam",option=b"spam-token"),
                    types.MessageReportOption(text="Otros",option=b"other-token"),
                ]),
            types.ReportResultAddComment(option=b"comment-token",optional=False),
            types.ReportResultReported(),
        ]
        first=await self.panel.reports.begin(111,self.data())
        self.assertEqual(first["kind"],"choose")
        self.assertEqual([x["text"] for x in first["choices"]],["Spam","Otros"])
        second=await self.panel.reports.next(111,{"account":22,"peer":self.peer,"token":first["choices"][0]["token"]})
        self.assertEqual(second["kind"],"comment")
        self.assertFalse(second["optional"])
        third=await self.panel.reports.next(111,{"account":22,"peer":self.peer,"token":second["token"],"comment":"Contenido que viola las normas"})
        self.assertEqual(third["kind"],"reported")
        req=[x.args[0] for x in self.client.rpc.await_args_list]
        self.assertEqual(len(req),3)
        self.assertTrue(all(isinstance(x,functions.messages.ReportRequest) for x in req))
        self.assertEqual([x.id for x in req],[[9],[9],[9]])
        self.assertEqual([x.option for x in req],[b"",b"spam-token",b"comment-token"])
        self.assertEqual(req[-1].message,"Contenido que viola las normas")

    async def test_step_token_cannot_be_replayed(self):
        self.client.rpc.side_effect=[
            types.ReportResultChooseOption(
                title="Elige",options=[types.MessageReportOption(text="Otro",option=b"op")]),
            types.ReportResultReported(),
        ]
        result=await self.panel.reports.begin(111,self.data())
        choice=result["choices"][0]["token"]
        sent=await self.panel.reports.next(111,{"account":22,"peer":self.peer,"token":choice})
        self.assertEqual(sent["kind"],"reported")
        with self.assertRaisesRegex(ValueError,"ya se envió"):
            await self.panel.reports.next(111,{"account":22,"peer":self.peer,"token":choice})
        self.assertEqual(self.client.rpc.await_count,2)

    async def test_admin_role_scoped_to_real_account_and_chat(self):
        self.allowed.clear()
        with self.assertRaises(web.HTTPForbidden):
            await self.panel.reports.begin(111,self.data())
        self.client.rpc.assert_not_awaited()

    async def test_tampered_peer_and_message_are_rejected(self):
        m=self.client.get_messages.return_value
        m.peer_id=types.PeerChannel(99)
        with self.assertRaisesRegex(ValueError,"pertenece"):
            await self.panel.reports.begin(111,self.data())
        self.client.rpc.assert_not_awaited()

    async def test_private_conversation_cannot_report_via_group_control(self):
        self.entity=types.User(id=55,first_name="Persona",access_hash=6)
        self.client.get_entity.return_value=self.entity
        self.peer=self.panel.pack("peer",actor=111,uid=22,generation=3,
                                   **peer_data(self.entity,22))
        with self.assertRaisesRegex(ValueError,"grupo o canal"):
            await self.panel.reports.begin(111,self.data())
        self.client.rpc.assert_not_awaited()

    async def test_comment_required_and_token_must_match_account(self):
        self.client.rpc.side_effect=[types.ReportResultAddComment(option=b"comment",optional=False)]
        start=await self.panel.reports.begin(111,self.data())
        with self.assertRaisesRegex(ValueError,"comentario"):
            await self.panel.reports.next(111,{"account":22,"peer":self.peer,"token":start["token"]})
        with self.assertRaises(web.HTTPForbidden):
            await self.panel.reports.next(111,{"account":23,"peer":self.peer,"token":start["token"],"comment":"prueba"})
        self.assertEqual(self.client.rpc.await_count,1)

    async def test_optional_comment_can_be_skipped(self):
        self.client.rpc.side_effect=[
            types.ReportResultAddComment(option=b"comment",optional=True),
            types.ReportResultReported(),
        ]
        start=await self.panel.reports.begin(111,self.data())
        response=await self.panel.reports.next(111,{"account":22,"peer":self.peer,
                                       "token":start["token"],"comment":"","skip":True})
        self.assertEqual(response["kind"],"reported")
        self.assertEqual(self.client.rpc.await_args.args[0].message,"")

    def test_user_ui_requires_explicit_report_choice(self):
        source=(Path(__file__).resolve().parents[1]/"descargas_app"/"panel_web"/"app.js").read_text()
        self.assertIn("Reportar a Telegram",source)
        self.assertIn("report-begin",source)
        self.assertIn("report-step",source)
        self.assertIn("messageOptions(m)",source)
        self.assertIn("oncontextmenu",source)
        self.assertIn("confirm(",source)


if __name__=="__main__":
    unittest.main()
