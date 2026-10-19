"""Opt-in shared PostgreSQL deployment. Legacy service remains available unchanged."""
import asyncio
from contextvars import ContextVar
import json
import logging
import os
import phonenumbers
import sqlite3
import time
import aiohttp
from telethon import errors
from .service import Service,parse_link,error_message
from .pg_store import Repository

LOG=logging.getLogger(__name__)
CURRENT_JOB=ContextVar('descargas_job',default=None)

class ScaleService(Service):
    def __init__(self,shared_lock=None):
        super().__init__(shared_lock)
        # Base helpers use this only as an ephemeral account-status cache.
        self.db.close();self.db=sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE users(uid INTEGER PRIMARY KEY,session BLOB)')
        self.repo=Repository(os.environ.get('DATABASE_URL',''),self.cipher,
            pool_size=int(os.environ.get('DB_POOL_SIZE','12')))
        self.role=os.environ.get('DESCARGAS_ROLE','web')
        self.concurrency=max(1,min(8,int(os.environ.get('DOWNLOAD_CONCURRENCY','3'))))
        self.max_pending=max(20,min(500,int(os.environ.get('LOGIN_CAPACITY','200'))))
        self.bot_id=None;self.stopping=False

    async def get_session(self,uid):
        row=await self.repo.account(uid)
        return self.cipher.decrypt(bytes(row['session'])).decode() if row and row['session'] else ''

    async def cache_session(self,uid):
        session=await self.get_session(uid)
        if session:self.db.execute('INSERT OR REPLACE INTO users VALUES(?,?)',(uid,self.cipher.encrypt(session.encode())))
        else:self.db.execute('DELETE FROM users WHERE uid=?',(uid,))
        self.db.commit()
        return session

    async def api(self,method,payload):
        if method in ('sendMessage','copyMessages','editMessageCaption'):
            count=len(payload.get('message_ids',[])) if method=='copyMessages' else 1
            await self.repo.pace('bot-send',0.06*max(1,count))
        # Retry only explicit Telegram 429 rejections, never ambiguous network sends.
        for attempt in range(3):
            async with self.http.post(f'https://api.telegram.org/bot{self.token}/{method}',json=payload) as response:
                result=await response.json()
            if result.get('ok'):return result['result']
            if result.get('error_code')==429 and attempt<2:
                await asyncio.sleep(min(300,max(1,int(result.get('parameters',{}).get('retry_after',5)))))
                continue
            LOG.error('DESCARGAS API %s rejected code=%s',method,result.get('error_code'))
            raise RuntimeError('Telegram rechazó la operación')

    async def start(self):
        if not self.configured() or not self.repo.url:return
        self.http=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        self.tasks=[asyncio.create_task(self.bootstrap())]

    async def bootstrap(self):
        try:
            await self.repo.start()
            me=await self.api('getMe',{});self.bot_id=me['id']
            if self.role=='worker':
                self.bot=self.client();await self.bot.connect()
                await self.bot.sign_in(bot_token=self.token)
                self.tasks.extend(asyncio.create_task(self.work()) for _ in range(self.concurrency))
                self.tasks.append(asyncio.create_task(self.maintenance()))
            elif self.role=='web':
                self.tasks.append(asyncio.create_task(self.poll()))
                self.tasks.append(asyncio.create_task(self.expire()))
            else:raise ValueError('Invalid DESCARGAS_ROLE')
            self.ready=True;self.state='ready'
        except Exception as exc:
            self.state='startup_error';LOG.error('Scale startup failed (%s)',type(exc).__name__)

    async def poll(self):
        while not self.stopping:
            try:
                async with self.repo.leader(self.bot_id):
                    await self.api('deleteWebhook',{'drop_pending_updates':False})
                    await self.api('setMyCommands',{'commands':[{'command':'start','description':'Conectar cuenta y descargar'},{'command':'estado','description':'Ver conexión de mi cuenta'},{'command':'desconectar','description':'Cerrar mi sesión'}]})
                    await self.api('setChatMenuButton',{'menu_button':{'type':'web_app','text':'Mi cuenta','web_app':{'url':self.url}}})
                    key=f'offset:{self.bot_id}'
                    saved=await self.repo.setting(key)
                    if saved is None:
                        last=await self.api('getUpdates',{'offset':-1,'timeout':0,'allowed_updates':['message','callback_query']})
                        offset=last[-1]['update_id']+1 if last else 0
                        await self.repo.set_setting(key,offset)
                    else:offset=int(saved)
                    while not self.stopping:
                        updates=await self.api('getUpdates',{'offset':offset,'timeout':25,'limit':100,'allowed_updates':['message','callback_query']})
                        for update in updates:
                            # Offset advances only after persistent acceptance. Duplicate update IDs are safe.
                            await self.update(update)
                            offset=update['update_id']+1
                            await self.repo.set_setting(key,offset)
            except asyncio.CancelledError:raise
            except Exception as exc:
                LOG.warning('Scale poll paused (%s)',type(exc).__name__);await asyncio.sleep(5)

    async def update(self,update):
        message=update.get('message',{})
        if message.get('chat',{}).get('type')=='private' and not message.get('from',{}).get('is_bot'):
            uid=message['from']['id'];text=message.get('text','').strip()
            if text.startswith('https://'):
                person=message['from']
                name=' '.join((str(person.get('first_name',''))+' '+str(person.get('last_name',''))).split())[:128] or 'Usuario'
                links=list(dict.fromkeys(text.split()))
                try:
                    if len(links)>20:raise ValueError('Envía un máximo de 20 enlaces por mensaje.')
                    for link in links:parse_link(link)
                    result=await self.repo.enqueue(self.bot_id,update['update_id'],uid,links,name,
                        int(os.environ.get('QUEUE_CAPACITY','10000')))
                    if result=='duplicate':return
                    messages={'busy':'Espera a que termine tu descarga anterior.',
                        'disconnected':'Conecta primero tu cuenta desde el botón.',
                        'full':'Demasiadas solicitudes en cola. Inténtalo después.',
                        'queued':f'Recibido: {len(links)} enlace(s). Tu solicitud está en cola.'}
                    try:await self.tell(uid,messages[result],result=='disconnected')
                    except Exception:pass # Durable acceptance must not depend on acknowledgement delivery.
                except ValueError as exc:await self.tell(uid,error_message(exc),True)
                return
            if text=='/estado':
                await self.tell(uid,'✅ Cuenta conectada. Envía los enlaces.' if await self.get_session(uid) else 'Conecta primero tu cuenta.',True)
                return
        await super().update(update)

    async def send_code(self,uid,data):
        if data.get('consent') is not True:raise ValueError('Acepta el acceso de la sesión antes de continuar.')
        if await self.get_session(uid):return {'connected':True}
        if time.monotonic()-self.last_code.get(uid,-1000)<60:raise ValueError('Espera un minuto antes de solicitar otro código.')
        if len(self.pending)>=self.max_pending and uid not in self.pending:raise ValueError('Demasiadas conexiones en curso. Inténtalo después.')
        try:
            phone=phonenumbers.parse(str(data.get('phone','')),None)
            if not phonenumbers.is_valid_number(phone):raise ValueError()
            phone=phonenumbers.format_number(phone,phonenumbers.PhoneNumberFormat.E164)
        except Exception:raise errors.PhoneNumberInvalidError(request=None)
        self.last_code[uid]=time.monotonic();await self.clear_pending(uid)
        client=self.client();await client.connect()
        try:sent=await client.send_code_request(phone)
        except BaseException:await client.disconnect();raise
        self.pending[uid]={'client':client,'phone':phone,'hash':sent.phone_code_hash,'created':time.monotonic()}
        return {'step':'code','message':'Revisa el código que recibiste en Telegram.'}

    async def verify(self,uid,data,password=False):
        pending=self.pending.get(uid)
        if not pending or time.monotonic()-pending['created']>600:
            await self.clear_pending(uid);raise ValueError('El código venció. Solicita uno nuevo.')
        client=pending['client']
        try:
            if password:await client.sign_in(password=str(data.get('password','')))
            else:await client.sign_in(phone=pending['phone'],code=str(data.get('code','')),phone_code_hash=pending['hash'])
        except errors.SessionPasswordNeededError:return {'step':'password'}
        me=await client.get_me()
        if me.id!=uid:
            await client.log_out();await self.clear_pending(uid)
            raise ValueError('La cuenta conectada debe ser la misma que está hablando con el bot.')
        number=phonenumbers.parse(pending['phone'],None)
        async with self.repo.account_lock(uid):
            await self.repo.save_account(uid,client.session.save(),{'phone':pending['phone'],
                'phone_country':phonenumbers.region_code_for_number(number) or 'ZZ',
                'calling_code':number.country_code,'updated_at':int(time.time())})
        await self.clear_pending(uid)
        try:await self.tell(uid,'✅ CUENTA CONECTADA\nAhora envía aquí enlaces de fotos, videos o álbumes.')
        except Exception:pass
        return {'connected':True}

    async def disconnect(self,uid):
        await self.repo.cancel_user(uid)
        async with self.repo.account_lock(uid):
            await self.clear_pending(uid)
            session=await self.get_session(uid)
            if session:
                client=self.client(session)
                try:await client.connect();await client.log_out()
                finally:await client.disconnect()
            await self.repo.erase_session(uid)
            self.db.execute('DELETE FROM users WHERE uid=?',(uid,));self.db.commit()
            self.contacts.pop(uid,None)

    async def before_send(self,uid,count=1):
        job=CURRENT_JOB.get()
        if not job:raise RuntimeError('Missing durable job')
        account=await self.repo.account(uid)
        if not account or account['generation']!=job['generation'] or not account['session']:
            raise ValueError('La sesión fue desconectada. Se canceló el envío.')
        await self.repo.state(job['id'],'sending')
        await self.repo.pace('bot-send',0.06*count)

    async def archive(self,uid,delivered):
        await self.repo.pace('archive-group',3.1*len(delivered))
        return await super().archive(uid,delivered)

    async def lease(self,job):
        while True:
            await asyncio.sleep(20)
            await self.repo.heartbeat(job['id'])

    async def work(self):
        while not self.stopping:
            try:job=await self.repo.claim(self.bot_id)
            except asyncio.CancelledError:raise
            except Exception:
                await asyncio.sleep(5);continue
            if not job:await asyncio.sleep(0.5);continue
            heartbeat=asyncio.create_task(self.lease(job));marker=CURRENT_JOB.set(job)
            client=None
            try:
                async with self.repo.account_lock(job['uid']):
                    account=await self.repo.account(job['uid'])
                    if not account or not account['session'] or account['generation']!=job['generation']:
                        await self.repo.state(job['id'],'cancelled');continue
                    payload=self.repo.decode(job['payload']);uid=job['uid'];self.names[uid]=payload['name']
                    session=self.cipher.decrypt(bytes(account['session'])).decode()
                    client=self.client(session);await client.connect()
                    if not await client.is_user_authorized():raise ValueError('Conecta de nuevo tu cuenta; la sesión fue revocada.')
                    seen={tuple(key) for key in payload.get('seen',[])};count=job['count']
                    for position in range(job['position'],len(payload['links'])):
                        async with asyncio.timeout(1800):
                            count+=await self.deliver(uid,client,payload['links'][position],seen)
                        payload['seen']=[list(key) for key in seen]
                        await self.repo.checkpoint(job['id'],position+1,count,payload)
                        await asyncio.sleep(2)
                    await self.repo.state(job['id'],'done')
                    try:await self.tell(uid,f'✅ Terminado. {count} elemento(s) entregado(s).')
                    except Exception:pass
            except asyncio.CancelledError:
                # Leave lease + durable phase for the recovery process; never replay uncertain sends.
                raise
            except Exception as exc:
                LOG.warning('Scale job stopped (%s)',type(exc).__name__)
                try:
                    await self.repo.state(job['id'],'review')
                    await self.tell(job['uid'],error_message(exc)+' Revisa lo recibido antes de repetir la orden.')
                except Exception:pass
            finally:
                heartbeat.cancel();await asyncio.gather(heartbeat,return_exceptions=True)
                if client:await client.disconnect()
                self.names.pop(job['uid'],None);CURRENT_JOB.reset(marker)

    async def maintenance(self):
        while True:
            try:await self.repo.recover()
            except asyncio.CancelledError:raise
            except Exception as exc:LOG.warning('Queue maintenance paused (%s)',type(exc).__name__)
            await asyncio.sleep(30)

    async def expire(self):
        while True:
            await asyncio.sleep(30)
            now=time.monotonic()
            for uid,p in list(self.pending.items()):
                if now-p['created']>600:
                    async with self.locks[uid]:await self.clear_pending(uid)
            for uid,(_,created) in list(self.contacts.items()):
                if now-created>600:self.contacts.pop(uid,None)
            for uid,attempts in list(self.attempts.items()):
                recent=[stamp for stamp in attempts if now-stamp<60]
                if recent:self.attempts[uid]=recent
                else:self.attempts.pop(uid,None)
            for uid,stamp in list(self.last_code.items()):
                if now-stamp>600 and uid not in self.pending:self.last_code.pop(uid,None)
            for uid,lock in list(self.locks.items()):
                if uid not in self.pending and not lock.locked():self.locks.pop(uid,None)
            self.names.clear()

    async def close(self):
        self.stopping=True
        await super().close()
        if self.repo.pool:await asyncio.wait_for(self.repo.close(),20)
