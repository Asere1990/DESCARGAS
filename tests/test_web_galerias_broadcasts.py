"""Isolated Web Galerías general notices and Telegram-delivery security tests."""
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from aiohttp import web

from web_galerias import broadcasts


class NoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unprivileged_cannot_publish_or_upload_notice(self):
        fake=N(db=lambda:None)
        with patch.object(broadcasts,"is_admin",return_value=False):
            for op in ("admin-notices-publish","admin-notices-list",
                       "admin-notices-audience"):
                with self.subTest(op=op),self.assertRaises(web.HTTPForbidden):
                    await broadcasts.admin_operation(fake,445,op,{})
        request=N(query={"kind":"photo"},content_type="image/jpeg")
        fake2=N(auth=AsyncMock(return_value=445))
        with patch.object(broadcasts,"is_admin",return_value=False):
            with self.assertRaises(web.HTTPForbidden):
                await broadcasts.upload(fake2,request)

    def test_clickable_phrases_allow_only_https_and_escape_markup(self):
        answer=broadcasts.format_html(
            "Hola <script> [Toca aquí](https://example.org/promos?x=1&y=2) "
            "y https://example.org/oferta")
        self.assertIn("Hola &lt;script&gt;",answer)
        self.assertIn('href="https://example.org/promos?x=1&amp;y=2"',answer)
        self.assertIn('>Toca aquí</a>',answer)
        self.assertIn('>https://example.org/oferta</a>',answer)
        self.assertNotIn("<script>",answer)
        self.assertIsNone(broadcasts.safe_url("javascript:alert(1)"))
        self.assertIsNone(broadcasts.safe_url("http://example.com"))
        self.assertIsNone(broadcasts.safe_url("https://bob:pass@example.com"))
        self.assertIsNotNone(broadcasts.safe_url("https://t.me/CustomerRobot"))

    async def test_publish_requires_confirm_and_valid_payload(self):
        g=N(db=lambda:N())
        with patch.object(broadcasts,"is_admin",return_value=True):
            for data in ({},{"message":"Hola"},
                         {"message":"", "confirm":"ENVIAR_A_TODOS"},
                         {"message":"x"*3001,"confirm":"ENVIAR_A_TODOS"}):
                with self.subTest(data=data),self.assertRaises(web.HTTPBadRequest):
                    await broadcasts.admin_operation(g,1,"admin-notices-publish",data)

    async def test_single_publish_is_atomic_and_sends_to_existing_users(self):
        class Conn:
            def __init__(self):
                self.fetchrow=AsyncMock(return_value={"id":77})
                self.fetchval=AsyncMock(return_value=3)
                self.execute=AsyncMock(return_value="INSERT 0 3")
            @asynccontextmanager
            async def transaction(self):
                yield
        conn=Conn()
        @asynccontextmanager
        async def acquire():
            yield conn
        g=N(db=lambda:N(acquire=acquire),log=AsyncMock())
        with patch.object(broadcasts,"is_admin",return_value=True):
            out=await broadcasts.admin_operation(g,42,"admin-notices-publish",{
                "message":"Hola [ver](https://example.org)",
                "confirm":"ENVIAR_A_TODOS"})
        self.assertEqual(out,{"ok":True,"id":77,"audience":3})
        self.assertIn("wg_notice_deliveries",conn.execute.await_args.args[0])
        self.assertIn("INSERT INTO wg_notices",conn.fetchrow.await_args.args[0])
        g.log.assert_awaited_once_with(42,"admin-notice-published")

    async def test_registered_user_cannot_read_other_users_receipts(self):
        db=N(fetchval=AsyncMock(side_effect=[1,4,1,1]),fetch=AsyncMock(
            return_value=[{"id":7,"message":"Aviso","media_kind":None,
                           "mime":None,"filename":None,"created":200,
                           "read":False}]),execute=AsyncMock())
        g=N(db=lambda:db)
        count=await broadcasts.user_operation(g,123,"notices-count",{})
        self.assertEqual(count["unread"],4)
        listing=await broadcasts.user_operation(g,123,"notices-list",{})
        self.assertEqual([x["id"] for x in listing["notices"]],[7])
        self.assertEqual(db.fetch.await_args.args[1],123)
        result=await broadcasts.user_operation(g,123,"notices-mark-read",{"ids":[7]})
        self.assertTrue(result["ok"])
        self.assertEqual(db.execute.await_args.args[1],123)
        self.assertIn("JOIN wg_users u ON u.uid=$1",db.fetch.await_args.args[0])

    async def test_no_registered_user_has_no_notices(self):
        g=N(db=lambda:N(fetchval=AsyncMock(return_value=None)))
        with self.assertRaises(web.HTTPForbidden):
            await broadcasts.user_operation(g,999,"notices-list",{})

    async def test_claim_and_finish_keep_retry_durable(self):
        db=N(fetchrow=AsyncMock(return_value={"notice":1,"uid":55,"attempts":2}),
             execute=AsyncMock())
        res=await broadcasts._claim(db)
        self.assertEqual(res["uid"],55)
        self.assertIn("SKIP LOCKED",db.fetchrow.await_args.args[0])
        await broadcasts._finish(db,1,55,"retry","429",10)
        self.assertEqual(db.execute.await_args.args[2],"retry")
        self.assertGreater(db.execute.await_args.args[3],0)

    def test_durable_separate_tables_and_private_r2_bucket(self):
        for table in ("wg_notices","wg_notice_uploads","wg_notice_reads",
                      "wg_notice_deliveries"):
            self.assertIn("CREATE TABLE IF NOT EXISTS "+table,broadcasts.SCHEMA)
        self.assertIn("ENABLE ROW LEVEL SECURITY",broadcasts.SCHEMA)
        self.assertNotIn("DELETE FROM wg_media",broadcasts.SCHEMA)
        self.assertNotIn("descarga_accounts",broadcasts.SCHEMA)
        self.assertIn("notices/",__import__("inspect").getsource(broadcasts.upload))


if __name__=="__main__":
    unittest.main()
