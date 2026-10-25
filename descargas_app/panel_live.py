"""Live Telegram updates, with exclusive session ownership and worker handoff.

Only invalidation hints cross SSE. All message/media reads retain the panel's
current actor, generation and source checks. No additional Telegram login.
"""
import asyncio
from contextlib import asynccontextmanager,suppress
import json
import logging
import time
from types import SimpleNamespace
from aiohttp import web
from telethon import events,types,utils
from .creator_access import ACTOR,require_account,require_source
from .moderation import authorized

LOG=logging.getLogger(__name__)
CHANNEL='descargas_panel_yield'
APPLICATION='descargas-panel-live'


def update_hint(update):
    name=type(update).__name__;peer=getattr(update,'peer',None);message=getattr(update,'message',None)
    if message is not None and not isinstance(message,int):peer=getattr(message,'peer_id',peer)
    if peer is None and getattr(update,'channel_id',None):peer=types.PeerChannel(update.channel_id)
    if peer is None and getattr(update,'chat_id',None):peer=types.PeerChat(update.chat_id)
    if peer is None and getattr(update,'user_id',None):peer=types.PeerUser(update.user_id)
    source=utils.get_peer_id(peer) if peer else None
    if name in ('UpdateShortMessage','UpdateShortChatMessage','UpdateShortSentMessage'):return source,{'kind':'message','ids':[update.id]}
    if name=='UpdateChannelTooLong':return source,{'kind':'resync'}
    if name in ('UpdateNewMessage','UpdateNewChannelMessage','UpdateNewScheduledMessage'):return source,{'kind':'message','ids':[message.id]}
    if name in ('UpdateEditMessage','UpdateEditChannelMessage'):return source,{'kind':'edit','ids':[message.id]}
    if name in ('UpdateDeleteMessages','UpdateDeleteChannelMessages'):return source,{'kind':'delete','ids':list(update.messages)}
    if name=='UpdateMessageReactions':return source,{'kind':'reaction','ids':[update.msg_id]}
    if name in ('UpdateReadHistoryOutbox','UpdateReadChannelOutbox'):return source,{'kind':'read','max':update.max_id}
    if name in ('UpdateReadHistoryInbox','UpdateReadChannelInbox','UpdateReadMessagesContents','UpdateChannelReadMessagesContents'):return source,{'kind':'refresh'}
    if name in ('UpdateUserTyping','UpdateChatUserTyping','UpdateChannelUserTyping'):return source,{'kind':'typing','active':not isinstance(update.action,types.SendMessageCancelAction)}
    if name=='UpdateUserStatus':return source,{'kind':'status','status':update.status}
    if name in ('UpdateChannel','UpdateChat','UpdateChatParticipants','UpdateFolderPeers','UpdateDialogFilter','UpdateDialogFilters'):return source,{'kind':'refresh'}
    return None,None


class LiveEntry:
    def __init__(self,hub,uid,generation):
        self.hub=hub;self.p=hub.p;self.s=hub.p.s;self.uid=uid;self.generation=generation
        self.subscribers=set();self.operation=asyncio.Lock();self.ready=False;self.client=None;self.wake=asyncio.Event();self.yield_session=asyncio.Event();self.stopping=False;self.task=None
    def publish(self,data,source=None):
        for sub in tuple(self.subscribers):
            if source is not None and sub.source is not None and source!=sub.source:continue
            if data['kind']=='delete' and source is None and isinstance(sub.entity,types.Channel):continue
            if data['kind'] in ('state','resync','revoked','reconnect'):value=data
            elif data['kind']=='status':
                if sub.source!=source:continue
                value={'kind':'status','status':self.p.features.status(SimpleNamespace(status=data['status']))}
            elif data['kind']=='typing':
                if sub.source!=source:continue
                value=data
            elif sub.source is None:value={'kind':'chats'}
            else:value=data
            if sub.queue.full():
                while not sub.queue.empty():sub.queue.get_nowait()
                value={'kind':'resync'}
            sub.queue.put_nowait(value)
    async def raw(self,update):
        source,data=update_hint(update)
        if data:self.publish(data,source)
    async def valid_actor(self):
        for sub in tuple(self.subscribers):
            try:
                await sub.validate()
                return sub.actor
            except (ValueError,web.HTTPException):
                sub.queue.put_nowait({'kind':'revoked'}) if not sub.queue.full() else None
        return None
    async def run(self):
        while not self.stopping and not self.hub.stopping:
            actor=await self.valid_actor()
            if actor is None:return
            self.yield_session.clear()
            acquired=False;marker=ACTOR.set(actor)
            try:
                acquired=await self.hub.acquire(self.uid)
                if not acquired:
                    self.publish({'kind':'state','state':'busy'})
                    await asyncio.sleep(1);continue
                row=await self.s.repo.account(self.uid)
                if not row or not row['session'] or row['generation']!=self.generation:
                    self.publish({'kind':'revoked'});return
                if self.client is None:
                    self.client=self.s.client(await self.s.get_session(self.uid))
                    self.client.add_event_handler(self.raw,events.Raw())
                await self.client.connect()
                me=await self.client.get_me()
                if not me or me.id!=self.uid:
                    self.publish({'kind':'revoked'});return
                await self.client.set_receive_updates(True)
                await self.client.catch_up()
                self.ready=not self.yield_session.is_set()
                if self.ready:self.publish({'kind':'state','state':'live'});self.publish({'kind':'resync'})
                while self.subscribers and not self.stopping and not self.yield_session.is_set() and self.client.is_connected():
                    self.wake.clear()
                    if self.yield_session.is_set() or self.stopping:break
                    try:await asyncio.wait_for(self.wake.wait(),timeout=10)
                    except TimeoutError:pass
                    if await self.valid_actor() is None:break
                self.ready=False
                if self.yield_session.is_set():self.publish({'kind':'state','state':'busy'})
            except asyncio.CancelledError:raise
            except Exception as exc:
                LOG.warning('Panel live connection deferred (%s)',type(exc).__name__)
                self.ready=False;self.publish({'kind':'state','state':'reconnecting'})
                await asyncio.sleep(2)
            finally:
                self.ready=False
                if acquired:
                    # Every web operation finishes before another process may
                    # use this Telegram authorization. Never overlap auth keys.
                    async with self.operation:
                        if self.client:
                            with suppress(Exception):await self.client.disconnect()
                        await self.hub.release(self.uid)
                ACTOR.reset(marker)
            if not self.subscribers:return
            if self.yield_session.is_set():await asyncio.sleep(1)
    @asynccontextmanager
    async def borrow(self,actor):
        async with self.operation:
            if self.ready and not self.yield_session.is_set():
                await require_account(self.s,self.uid,actor)
                row=await self.s.repo.account(self.uid)
                if row and row['session'] and row['generation']==self.generation:
                    yield self.client,self.generation
                    return
            yield None


class Subscription:
    def __init__(self,hub,request,session,uid,generation,source,entity):
        self.hub=hub;self.request=request;self.session=session;self.actor=session['actor'];self.uid=uid;self.generation=generation;self.source=source;self.entity=entity;self.queue=asyncio.Queue(maxsize=64)
    async def validate(self):
        if not authorized(self.actor):raise web.HTTPForbidden()
        await self.hub.p.session(self.request)
        await require_account(self.hub.p.s,self.uid,self.actor)
        row=await self.hub.p.s.repo.account(self.uid)
        if not row or not row['session'] or row['generation']!=self.generation:raise web.HTTPForbidden()
        if self.source is not None:
            await require_source(self.hub.p.s,self.uid,self.source,entity=self.entity,client=self.hub.entries.get(self.uid).client if self.uid in self.hub.entries else None,actor=self.actor)


class LiveHub:
    def __init__(self,panel):
        self.p=panel;self.entries={};self.connection=None;self.db_lock=asyncio.Lock();self.stopping=False
    async def connect_database(self):
        if self.connection is None or self.connection.is_closed():
            if self.connection:
                with suppress(Exception):await self.p.s.repo.pool.release(self.connection)
            self.connection=await self.p.s.repo.pool.acquire()
            await self.connection.execute("SELECT set_config('application_name',$1,false)",APPLICATION)
            await self.connection.add_listener(CHANNEL,self.notification)
            self.connection.add_termination_listener(self.lost_database)
    def lost_database(self,connection):
        if self.stopping:return
        for entry in tuple(self.entries.values()):
            entry.ready=False;entry.stopping=True;entry.wake.set()
            if entry.client:asyncio.create_task(entry.client.disconnect())
    def notification(self,connection,pid,channel,payload):
        try:entry=self.entries.get(int(payload))
        except ValueError:return
        if entry:entry.ready=False;entry.yield_session.set();entry.wake.set()
    async def acquire(self,uid):
        async with self.db_lock:
            await self.connect_database()
            return bool(await self.connection.fetchval('SELECT pg_try_advisory_lock($1)',uid))
    async def release(self,uid):
        async with self.db_lock:
            if self.connection and not self.connection.is_closed():await self.connection.execute('SELECT pg_advisory_unlock($1)',uid)
    @asynccontextmanager
    async def borrow(self,actor,uid):
        entry=self.entries.get(uid)
        if entry:
            async with entry.borrow(actor) as data:yield data
        else:yield None
    @asynccontextmanager
    async def subscribe(self,request,session,uid,handle):
        source=None;entity=None
        async with self.p.account(session['actor'],uid) as (client,generation):
            if handle:
                entity=await self.p.target(session['actor'],uid,generation,client,handle);source=utils.get_peer_id(entity)
        entry=self.entries.get(uid)
        if entry and entry.generation!=generation:
            entry.stopping=True;entry.wake.set();await entry.task;entry=None
        if entry is None or entry.task.done():
            entry=LiveEntry(self,uid,generation);self.entries[uid]=entry
        sub=Subscription(self,request,session,uid,generation,source,entity);entry.subscribers.add(sub);entry.wake.set()
        if entry.task is None or entry.task.done():
            entry.task=asyncio.create_task(entry.run())
            entry.task.add_done_callback(lambda task:self.completed(entry,task))
        elif entry.ready:sub.queue.put_nowait({'kind':'state','state':'live'});sub.queue.put_nowait({'kind':'resync'})
        try:yield sub
        finally:
            entry.subscribers.discard(sub);entry.wake.set()
    def completed(self,entry,task):
        if not task.cancelled():
            error=task.exception()
            if error:LOG.warning('Panel live listener restarted (%s)',type(error).__name__)
        if not entry.subscribers:
            if self.entries.get(entry.uid) is entry:self.entries.pop(entry.uid,None)
            entry.client=None
        elif not self.stopping:entry.publish({'kind':'reconnect'})
    async def stream(self,request,session,uid,handle):
        from .web_panel import HEADERS
        async with self.subscribe(request,session,uid,handle) as sub:
            response=web.StreamResponse(headers={**HEADERS,'Content-Type':'text/event-stream','X-Accel-Buffering':'no','Connection':'keep-alive'})
            await response.prepare(request)
            await response.write(b'retry: 1500\n\n')
            try:
                while not self.stopping:
                    try:data=await asyncio.wait_for(sub.queue.get(),timeout=15)
                    except TimeoutError:data=None
                    await sub.validate()
                    if data:
                        if data['kind']=='revoked':
                            await response.write(b'data: {"kind":"revoked"}\n\n');break
                        await response.write(('data: '+json.dumps(data,separators=(',',':'))+'\n\n').encode())
                        if data['kind']=='reconnect':break
                    else:await response.write(b': keepalive\n\n')
            except (ConnectionResetError,asyncio.CancelledError):raise
            except (ValueError,web.HTTPException):
                with suppress(ConnectionResetError):await response.write(b'data: {"kind":"revoked"}\n\n')
            return response
    async def close(self):
        self.stopping=True
        entries=list(self.entries.values())
        for entry in entries:
            entry.stopping=True;entry.wake.set()
            entry.publish({'kind':'revoked'})
        tasks=[e.task for e in entries if e.task and not e.task.done()]
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        async with self.db_lock:
            if self.connection:
                with suppress(Exception):await self.connection.remove_listener(CHANNEL,self.notification)
                await self.p.s.repo.pool.release(self.connection);self.connection=None
