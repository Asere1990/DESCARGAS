import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
import unittest
from telethon import types
from descargas_app.panel_live import LiveHub,LiveEntry,update_hint
from descargas_app.pg_store import Repository
from tests import test_web_panel as fixtures

class Acquire:
    def __init__(self,conn):self.conn=conn
    async def get(self):return self.conn
    def __await__(self):return self.get().__await__()
    async def __aenter__(self):return self.conn
    async def __aexit__(self,*args):pass

class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_locks_other_transfers_keep_original_fail_fast_behavior(self):
        conn=N(fetchval=AsyncMock(side_effect=[False,False]),execute=AsyncMock())
        repo=Repository('',None);repo.pool=N(acquire=lambda:Acquire(conn))
        with self.assertRaises(ValueError):
            async with repo.account_lock(22,wait=False):pass
        conn.execute.assert_not_awaited()
    async def test_worker_notifies_live_owner_before_waiting_for_exclusive_ownership(self):
        conn=N(fetchval=AsyncMock(side_effect=[False,True]),execute=AsyncMock())
        repo=Repository('',None);repo.pool=N(acquire=lambda:Acquire(conn))
        async with repo.account_lock(22):pass
        queries=[a.args[0] for a in conn.execute.await_args_list]
        self.assertIn('pg_notify',queries[0]);self.assertIn('pg_advisory_lock',queries[1]);self.assertIn('pg_advisory_unlock',queries[2])
    async def test_frontend_waits_for_live_owner_to_yield_without_new_login(self):
        conn=N(fetchval=AsyncMock(side_effect=[False,True,True]),execute=AsyncMock())
        repo=Repository('',None);repo.pool=N(acquire=lambda:Acquire(conn))
        with patch('descargas_app.pg_store.asyncio.sleep',AsyncMock()):
            async with repo.account_lock(22,wait=False):pass
        self.assertIn('pg_notify',conn.execute.await_args_list[0].args[0]);self.assertIn('pg_advisory_unlock',conn.execute.await_args_list[-1].args[0])
    async def test_listener_uses_one_database_connection_for_multiple_accounts(self):
        conn=N(is_closed=lambda:False,execute=AsyncMock(),fetchval=AsyncMock(return_value=True),add_listener=AsyncMock(),add_termination_listener=lambda callback:None,remove_listener=AsyncMock())
        pool=N(acquire=AsyncMock(return_value=conn),release=AsyncMock());hub=LiveHub(N(s=N(repo=N(pool=pool))))
        self.assertTrue(await hub.acquire(22));self.assertTrue(await hub.acquire(33));self.assertEqual(pool.acquire.await_count,1)
        await hub.release(22);await hub.release(33);await hub.close();pool.release.assert_awaited_once_with(conn)
    async def test_delete_events_do_not_cross_channel_message_id_namespaces(self):
        hub=LiveHub(N(s=N()));entry=LiveEntry(hub,22,3)
        class Sub:
            def __init__(self,source,entity):self.source=source;self.entity=entity;self.queue=asyncio.Queue(maxsize=64)
        private=Sub(55,types.User(55));channel=Sub(-1000000000077,types.Channel(77,'Grupo',types.ChatPhotoEmpty(),None));listing=Sub(None,None)
        entry.subscribers.update([private,channel,listing])
        entry.publish({'kind':'delete','ids':[9]},None)
        self.assertEqual((await private.queue.get())['ids'],[9]);self.assertTrue(channel.queue.empty());self.assertEqual((await listing.queue.get())['kind'],'chats')
        entry.publish({'kind':'state','state':'live'});self.assertEqual((await listing.queue.get())['state'],'live')
    async def test_updates_map_to_new_edit_delete_reaction_and_typing_hints(self):
        m=fixtures.message(9);m.peer_id=types.PeerUser(55)
        source,hint=update_hint(types.UpdateNewMessage(m,1,1));self.assertEqual(source,55);self.assertEqual(hint,{'kind':'message','ids':[9]})
        source,hint=update_hint(types.UpdateEditMessage(m,2,1));self.assertEqual(hint['kind'],'edit')
        source,hint=update_hint(types.UpdateDeleteChannelMessages(77,[9],3,1));self.assertEqual(source,-1000000000077);self.assertEqual(hint['kind'],'delete')
        source,hint=update_hint(types.UpdateUserTyping(55,types.SendMessageTypingAction()));self.assertTrue(hint['active'])
    async def test_handoff_drains_web_operation_then_disconnects_before_unlock(self):
        released=asyncio.Event();disconnected=asyncio.Event();state={'connected':False};order=[]
        async def connect():state['connected']=True
        async def disconnect():state['connected']=False;order.append('disconnect');disconnected.set()
        client=N(connect=connect,disconnect=disconnect,is_connected=lambda:state['connected'],get_me=AsyncMock(return_value=N(id=22)),set_receive_updates=AsyncMock(),catch_up=AsyncMock(),add_event_handler=lambda *args:None)
        s=N(repo=N(account=AsyncMock(return_value={'session':b'yes','generation':3})),get_session=AsyncMock(return_value='secret'),client=lambda session:client)
        hub=LiveHub(N(s=s));hub.acquire=AsyncMock(return_value=True)
        async def release(uid):self.assertFalse(state['connected']);order.append('unlock');released.set()
        hub.release=release;entry=LiveEntry(hub,22,3);hub.entries[22]=entry;entry.valid_actor=AsyncMock(return_value=111)
        class Sub:
            source=55;entity=types.User(55);queue=asyncio.Queue(maxsize=64)
        sub=Sub();entry.subscribers.add(sub);task=asyncio.create_task(entry.run())
        while (await asyncio.wait_for(sub.queue.get(),1)).get('state')!='live':pass
        await entry.operation.acquire();hub.notification(None,1,'descargas_panel_yield','22')
        self.assertFalse(entry.ready);self.assertFalse(disconnected.is_set());self.assertFalse(released.is_set())
        entry.stopping=True;entry.operation.release();await asyncio.wait_for(task,2)
        self.assertEqual(order,['disconnect','unlock'])

class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):await fixtures.PanelTests.asyncSetUp(self)
    setup_http=fixtures.PanelTests.setup_http
    async def test_sse_emits_events_without_polling_and_validates_each_delivery(self):
        await self.setup_http();queue=asyncio.Queue()
        for item in [{'kind':'state','state':'live'},{'kind':'edit','ids':[9]},{'kind':'revoked'}]:queue.put_nowait(item)
        sub=N(queue=queue,validate=AsyncMock())
        @asynccontextmanager
        async def subscribe(*args):yield sub
        self.panel.live.subscribe=subscribe
        response=await self.http.get('/panel/api/events',params={'account':22,'peer':self.peer})
        text=await response.text();self.assertEqual(response.content_type,'text/event-stream');self.assertIn('"state":"live"',text);self.assertIn('"kind":"edit"',text);self.assertIn('"kind":"revoked"',text);self.assertEqual(sub.validate.await_count,3)
    async def test_events_require_current_admin_session(self):
        await self.setup_http();self.allowed.clear()
        response=await self.http.get('/panel/api/events',params={'account':22,'peer':self.peer});self.assertEqual(response.status,403)
    async def test_stream_subscription_can_wait_for_busy_account_without_opening_second_session(self):
        import descargas_app.panel_live as live
        async def wait_for_close(entry):await asyncio.Event().wait()
        with patch.object(live,'require_account',self.account_guard),patch.object(live,'require_source',self.guard),patch.object(live.LiveEntry,'run',wait_for_close):
            async with self.panel.live.subscribe(N(),{'actor':111},22,self.peer) as sub:
                entry=self.panel.live.entries[22];task=entry.task
                self.assertEqual(sub.source,55);self.assertIsNone(entry.client)
                self.client.get_me.assert_not_awaited()
                self.guard.assert_awaited_once_with(self.s,22,55,actor=111)
            task.cancel();await asyncio.gather(task,return_exceptions=True)
