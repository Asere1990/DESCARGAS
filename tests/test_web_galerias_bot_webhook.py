"""New TOKEN_WEB Bot API webhook, safe takeover and durable inbox tests."""
import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from web_galerias import bot_feed as feed


class FakeConn:
    def __init__(self, route=True, result="INSERT 0 1"):
        self.route=route
        self.result=result
        self.calls=[]
    @asynccontextmanager
    async def transaction(self):
        yield self
    async def fetchrow(self,sql,*args):
        self.calls.append(("fetchrow",sql,args))
        if not self.route:
            return None
        return {"folder":55,"group_id":-1004406743505,"revision":"route1",
                "linked_at":1700000000}
    async def execute(self,sql,*args):
        self.calls.append(("execute",sql,args))
        return self.result

class FakePool:
    def __init__(self,conn):
        self.conn=conn
    @asynccontextmanager
    async def acquire(self):
        yield self.conn

def photo_update():
    return {"update_id":241,
            "message":{"message_id":141,"date":1800000000,
                "chat":{"id":-1004406743505,"type":"supergroup"},
                "from":{"is_bot":True,"id":998800},
                "caption":"Caption de otra cuenta",
                "media_group_id":"GRP123",
                "photo":[{"file_id":"image100","file_size":85422,
                          "width":700,"height":600}]}}

class FakeRequest:
    def __init__(self,secret,update=None):
        self.headers={"X-Telegram-Bot-Api-Secret-Token":secret}
        self.content_length=1024
        self.json=AsyncMock(return_value=update or photo_update())

class IngestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_from_other_bot_saves_media_group_caption_without_touching_readers(self):
        conn=FakeConn()
        g=SimpleNamespace(db=lambda:FakePool(conn),_bot_feed_schema_ready=True)
        with patch.object(feed,"token",return_value="123:SECRET"):
            response=await feed.handle_webhook(
                g,FakeRequest(feed.webhook_secret("123:SECRET")))
        self.assertTrue(json.loads(response.text)["queued"])
        self.assertEqual(g._bot_feed_receiver_state if hasattr(g,"_bot_feed_receiver_state") else None,None)
        insert=[z for z in conn.calls if z[0]=="execute"]
        self.assertEqual(len(insert),1)
        sql,args=insert[0][1:]
        self.assertIn("wg_bot_feed_items",sql)
        self.assertIn("ON CONFLICT(group_id,message_id) DO NOTHING",sql)
        self.assertEqual(args[0],-1004406743505)
        self.assertEqual(args[1],141)
        self.assertEqual(args[2],55)
        self.assertEqual(args[8],"Caption de otra cuenta")
        self.assertEqual(args[9],"GRP123")
        self.assertGreater(g._bot_feed_last_media_queued_at,0)
        self.assertNotIn("descarga_accounts",sql)

    async def test_repeat_webhook_deduplicates_without_claiming_new_media(self):
        conn=FakeConn(result="INSERT 0 0")
        g=SimpleNamespace(db=lambda:FakePool(conn),_bot_feed_schema_ready=True)
        with patch.object(feed,"token",return_value="123:SECRET"):
            result=await feed.handle_webhook(
                g,FakeRequest(feed.webhook_secret("123:SECRET")))
        self.assertFalse(json.loads(result.text)["queued"])
        self.assertFalse(hasattr(g,"_bot_feed_last_media_queued_at"))

    async def test_updates_outside_assigned_group_are_acknowledged_but_not_queued(self):
        conn=FakeConn(route=False)
        g=SimpleNamespace(db=lambda:FakePool(conn),_bot_feed_schema_ready=True)
        with patch.object(feed,"token",return_value="123:SECRET"):
            result=await feed.handle_webhook(
                g,FakeRequest(feed.webhook_secret("123:SECRET")))
        self.assertFalse(json.loads(result.text)["queued"])
        self.assertFalse([x for x in conn.calls if x[0]=="execute"])

    async def test_invalid_signature_cannot_write_any_media(self):
        conn=FakeConn()
        g=SimpleNamespace(db=lambda:FakePool(conn),_bot_feed_schema_ready=True)
        with patch.object(feed,"token",return_value="123:SECRET"):
            with self.assertRaises(web.HTTPForbidden):
                await feed.handle_webhook(g,FakeRequest("bad-secret"))
        self.assertEqual(conn.calls,[])

    async def test_ignored_text_is_acknowledged_without_db_call(self):
        g=SimpleNamespace(db=lambda:None,_bot_feed_schema_ready=True)
        update={"update_id":900,"message":{"message_id":123,
                "chat":{"id":-1004406743505,"type":"supergroup"},
                "date":1800000000,"text":"texto"}}
        with patch.object(feed,"token",return_value="123:SECRET"):
            response=await feed.handle_webhook(
                g,FakeRequest(feed.webhook_secret("123:SECRET"),update=update))
        self.assertFalse(json.loads(response.text)["queued"])

class ClientSessionMock:
    def __init__(self):
        self.client=MagicMock()
        self.client.__aenter__=AsyncMock(return_value=object())
        self.client.__aexit__=AsyncMock(return_value=False)

class SafeWebhookRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_exclusive_bot_registers_signed_webhook_without_deleting_pending(self):
        g=SimpleNamespace(closed=False,db=lambda:None)
        client=ClientSessionMock()
        called=[]
        async def tg(session, token, method, payload=None):
            called.append((method,payload))
            if method=="getMe":
                return {"id":11111111,"can_read_all_group_messages":True}
            if method=="getWebhookInfo":
                return {"url":""}
            if method=="setWebhook":
                self.assertEqual(payload["url"],feed.BOT_WEBHOOK_URL)
                self.assertEqual(payload["secret_token"],feed.webhook_secret(token))
                self.assertFalse(payload["drop_pending_updates"])
                self.assertEqual(payload["allowed_updates"],["message","channel_post"])
                return True
            raise AssertionError("Unexpected method")
        async def consume(g,s,t):
            g.closed=True
        class Lock:
            def __init__(self):
                self.locked=False
            async def fetchval(self,sql,*args):
                if "pg_try_advisory_lock" in sql:return True
                if "pg_advisory_unlock" in sql:return True
                raise AssertionError(sql)
        lock=Lock()
        @asynccontextmanager
        async def acquire():
            yield lock
        g.db=lambda:SimpleNamespace(acquire=acquire)
        with patch.object(feed,"token",return_value="11111111:SECRET"),patch.object(
            feed,"schema",new_callable=AsyncMock
        ),patch.object(feed.aiohttp,"ClientSession",return_value=client.client),patch.object(
            feed,"telegram",new_callable=AsyncMock,side_effect=tg),patch.object(
            feed,"consumer",new_callable=AsyncMock,side_effect=consume):
            await feed.run(g)
        self.assertEqual([m for m,_ in called],["getMe","getWebhookInfo","setWebhook"])
        self.assertEqual(g._bot_feed_receiver_state,"webhook")
        self.assertEqual(g._bot_feed_bot_id,11111111)

    async def test_foreign_zeus_webhook_cannot_be_changed_by_automatic_startup(self):
        g=SimpleNamespace(closed=False,db=lambda:None)
        client=ClientSessionMock()
        async def tg(session, token, method, payload=None):
            if method=="getMe": return {"id":444}
            if method=="getWebhookInfo": return {"url":"https://conserjee.onrender.com/SECRETPATH"}
            raise AssertionError("No automatic takeover allowed")
        with patch.object(feed,"token",return_value="444:SECRET"),patch.object(
            feed,"schema",new_callable=AsyncMock
        ),patch.object(feed.aiohttp,"ClientSession",return_value=client.client),patch.object(
            feed,"telegram",new_callable=AsyncMock,side_effect=tg) as bot:
            await feed.run(g)
        self.assertEqual(g._bot_feed_receiver_state,"webhook_conflict")
        self.assertEqual(g._bot_feed_webhook_host,"conserjee.onrender.com")
        self.assertEqual(bot.await_count,2)

    async def test_admin_confirm_still_cannot_delete_zeus_webhook(self):
        g=SimpleNamespace(bot_feed_task=None)
        client=ClientSessionMock()
        with patch.object(feed,"token",return_value="444:SECRET"),patch.object(
            feed.aiohttp,"ClientSession",return_value=client.client),patch.object(
            feed,"telegram",new_callable=AsyncMock,
            return_value={"url":"https://conserjee.onrender.com/HIDDEN"}) as bot:
            with self.assertRaises(web.HTTPConflict):
                await feed.activate_receiver(g,{"confirm":True})
        bot.assert_awaited_once()
        self.assertEqual(bot.await_args.args[2],"getWebhookInfo")

    def test_webhook_secret_rotates_with_bot_token(self):
        a=feed.webhook_secret("BOT_TOKEN_1")
        b=feed.webhook_secret("BOT_TOKEN_2")
        self.assertNotEqual(a,b)
        self.assertRegex(a,r"^[a-f0-9]{64}$")

if __name__=="__main__":
    unittest.main()
