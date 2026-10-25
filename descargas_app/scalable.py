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
from telethon import utils,errors
from .service import Service,parse_link,error_message,archive_attribution,apply_archive_caption
from .bot_typography import format_bot_payload
from .command_menu import configure_commands
from .billing import Billing,PaymentRequired
from .pg_store import Repository
from .protected import ProtectedSource,require_entity,require_bot_alias
from .moderation import authorized,SOURCE,DownloadBlocked,ensure_allowed,command as moderation_command
from .media_cache import CURRENT_MEDIA,CACHE_ONLY,CacheMiss,deliver_cached,archive_once

METRIC_SOURCE=ContextVar('metric_source',default=None)
METRIC_NAME=ContextVar('metric_name',default=None)
LOG=logging.getLogger(__name__)
class TelegramDeliveryError(RuntimeError):
    pass
CURRENT_JOB=ContextVar('descargas_job',default=None)

class ScaleService(Service):
    def __init__(self,shared_lock=None):
        super().__init__(shared_lock)
        # Base helpers use this only as an ephemeral account-status cache.
        self.db.close();self.db=sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE users(uid INTEGER PRIMARY KEY,session BLOB)')
        self.repo=Repository(os.environ.get('DATABASE_URL',''),self.cipher,
            pool_size=int(os.environ.get('DB_POOL_SIZE','12')))
        self.billing=Billing(self)
        self.role=os.environ.get('DESCARGAS_ROLE','web')
        self.concurrency=max(1,min(8,int(os.environ.get('DOWNLOAD_CONCURRENCY','3'))))
        self.fast_concurrency=max(1,min(4,int(os.environ.get('FAST_CONCURRENCY','2'))))
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

    async def api(self,method,payload,opportunistic=False):
        payload=format_bot_payload(method,payload)
        if method in ('sendMessage','copyMessages','editMessageCaption','editMessageText'):
            count=len(payload.get('message_ids',[])) if method=='copyMessages' else 1
            if opportunistic:
                if not await self.repo.try_pace('bot-send',0.06*max(1,count)):return None
            else:await self.repo.pace('bot-send',0.06*max(1,count))
        # Retry only explicit Telegram 429 rejections, never ambiguous network sends.
        for attempt in range(3):
            try:
                async with self.http.post(f'https://api.telegram.org/bot{self.token}/{method}',json=payload) as response:
                    result=await response.json()
            except (aiohttp.ClientError,TimeoutError) as exc:
                raise TelegramDeliveryError('Telegram delivery uncertain') from exc
            if result.get('ok'):return result['result']
            if result.get('error_code')==429 and attempt<2:
                await asyncio.sleep(min(300,max(1,int(result.get('parameters',{}).get('retry_after',5)))))
                continue
            LOG.error('DESCARGAS API %s rejected code=%s',method,result.get('error_code'))
            raise TelegramDeliveryError('Telegram rechazó la operación')

    async def progress_api(self,method,payload):
        return await self.api(method,payload,opportunistic=True)

    async def start(self):
        if not self.repo.url or not self.cipher:return
        if self.role=='web' and not self.configured():return
        self.http=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        self.tasks=[asyncio.create_task(self.bootstrap())]

    async def bootstrap(self):
        try:
            LOG.warning('Scale startup role=%s phase=database',self.role)
            await self.repo.start()
            LOG.warning('Scale startup role=%s phase=configuration',self.role)
            if self.role in ('worker','export'):
                for _ in range(60):
                    stored=await self.repo.setting('runtime_config')
                    if stored:break
                    await asyncio.sleep(2)
                else:raise RuntimeError('Waiting for frontend configuration')
                self.runtime_config=self.repo.decode(stored.encode())
                for field,value in self.runtime_config.items():setattr(self,field,value)
            me=await self.api('getMe',{});self.bot_id=me['id']
            LOG.warning('Scale startup role=%s phase=identity',self.role)
            if self.role=='web':
                config={field:getattr(self,field) for field in ('token','api_id','api_hash','url','almacen')}
                await self.repo.set_setting('runtime_config',self.repo.encode(config).decode())
                await self.repo.pool.execute("UPDATE descarga_jobs SET state='cancelled' WHERE bot_id<>$1 AND state='queued'",self.bot_id)
            if self.role in ('worker','export'):
                self.bot=self.client();await self.bot.connect()
                LOG.warning('Scale startup role=worker phase=telegram_connected')
                while True:
                    try:
                        await self.bot.sign_in(bot_token=self.token)
                        break
                    except errors.FloodWaitError as exc:
                        self.state='telegram_wait'
                        LOG.warning('Worker startup waiting for Telegram seconds=%s',exc.seconds)
                        await asyncio.sleep(exc.seconds+1)
                        self.state='starting'
                LOG.warning('Scale startup role=worker phase=authorized')
                from .group_download import run as group_export_loop
                if self.role=='worker':
                    self.download_tasks=[asyncio.create_task(self.work('slow')) for _ in range(self.concurrency)]+[asyncio.create_task(self.work('fast')) for _ in range(self.fast_concurrency)]
                    self.tasks.extend(self.download_tasks)
                    self.tasks.append(asyncio.create_task(self.maintenance()))
                    self.tasks.append(asyncio.create_task(self.archive_loop()))
                    self.tasks.append(asyncio.create_task(group_export_loop(self)))
                else:
                    self.bot.flood_sleep_threshold=0
                    await self.repo.set_setting(f'dedicated_export:{self.bot_id}','enabled')
                    self.download_tasks=[asyncio.create_task(group_export_loop(self))]
                    self.tasks.extend(self.download_tasks)
                if self.role=='worker':
                    from .panel_delivery import run as panel_delivery_loop
                    self.tasks.append(asyncio.create_task(panel_delivery_loop(self)))
                self.tasks.append(asyncio.create_task(self.watch_config()))
            elif self.role=='web':
                from .admin_panel import AdminPanel
                self.admin_panel=AdminPanel(self);await self.admin_panel.start()
                self.tasks.append(asyncio.create_task(self.admin_backfill()))
                await self.billing.configure()
                self.tasks.append(asyncio.create_task(self.billing.maintenance()))
                self.tasks.append(asyncio.create_task(self.poll()))
                self.tasks.append(asyncio.create_task(self.expire()))
            else:raise ValueError('Invalid DESCARGAS_ROLE')
            self.ready=True;self.state='ready'
            LOG.warning('Scale startup role=%s phase=ready concurrency=%s fast_concurrency=%s',self.role,self.concurrency,self.fast_concurrency)
        except Exception as exc:
            self.state='startup_error';LOG.error('Scale startup failed (%s)',type(exc).__name__)

    async def configure_bot_menu(self):
        while not self.stopping:
            try:
                await configure_commands(self)
                await self.api('setChatMenuButton',{'menu_button':{'type':'web_app','text':'𝐌𝐈 𝐂𝐔𝐄𝐍𝐓𝐀','web_app':{'url':self.url}}})
                return
            except asyncio.CancelledError:raise
            except Exception as exc:
                LOG.warning('Menu configuration delayed (%s); polling remains independent',type(exc).__name__)
                await asyncio.sleep(30)

    async def poll(self):
        while not self.stopping:
            try:
                async with self.repo.leader(self.bot_id):
                    if not getattr(self,'menu_configuration_started',False):
                        self.tasks.append(asyncio.create_task(self.configure_bot_menu()))
                        self.menu_configuration_started=True
                    if not getattr(self,'webhook_cleared',False):
                        try:
                            await self.api('deleteWebhook',{'drop_pending_updates':False})
                            self.webhook_cleared=True
                        except TelegramDeliveryError:
                            LOG.warning('Webhook setup unavailable; attempting getUpdates directly')

                    key=f'offset:{self.bot_id}'
                    saved=await self.repo.setting(key)
                    if saved is None:
                        last=await self.api('getUpdates',{'offset':-1,'timeout':0,'allowed_updates':['message','callback_query','pre_checkout_query','chat_member']})
                        offset=last[-1]['update_id']+1 if last else 0
                        await self.repo.set_setting(key,offset)
                    else:offset=int(saved)
                    while not self.stopping:
                        updates=await self.api('getUpdates',{'offset':offset,'timeout':25,'limit':100,'allowed_updates':['message','callback_query','pre_checkout_query','chat_member']})
                        if not getattr(self,'polling_confirmed',False):
                            LOG.warning('Scale polling active; Telegram updates received successfully')
                            self.polling_confirmed=True
                        for update in updates:
                            # Offset advances only after persistent acceptance. Duplicate update IDs are safe.
                            try:await self.update(update)
                            except TelegramDeliveryError:pass
                            offset=update['update_id']+1
                            await self.repo.set_setting(key,offset)
            except asyncio.CancelledError:raise
            except Exception as exc:
                LOG.warning('Scale poll paused (%s)',type(exc).__name__);await asyncio.sleep(5)

    async def update(self,update):
        message=update.get('message',{})
        query=update.get('callback_query',{})
        active_message=query.get('message',{}) if query else message
        actor=query.get('from',{}) if query else message.get('from',{})
        if self.repo.pool and active_message.get('chat',{}).get('type')=='private' and actor.get('id') and not actor.get('is_bot'):
            try:
                await self.repo.touch_activity(self.bot_id,actor['id'],' '.join(filter(None,[actor.get('first_name'),actor.get('last_name')])))
            except Exception:pass
        if getattr(self,'admin_panel',None) and await self.admin_panel.update(update):return
        if await self.billing.update(update):return
        if await moderation_command(self,message):return
        from .group_download import command as group_export_command
        if await group_export_command(self,message):return
        if message.get('chat',{}).get('type')=='private' and not message.get('from',{}).get('is_bot'):
            uid=message['from']['id'];text=message.get('text','').strip()
            if text.startswith('https://'):
                person=message['from']
                name=' '.join((str(person.get('first_name',''))+' '+str(person.get('last_name',''))).split())[:128] or 'Usuario'
                links=list(dict.fromkeys(text.split()))
                try:
                    if len(links)>5 and not authorized(uid):raise ValueError('Envía un máximo de 5 enlaces por mensaje. Divide los enlaces en lotes de cinco.')
                    for link in links:
                        peer,_=parse_link(link)
                        require_bot_alias(peer,uid)
                        await ensure_allowed(self,uid,peer if isinstance(peer,int) else None)
                    result=await self.repo.enqueue(self.bot_id,update['update_id'],uid,links,name,
                        int(os.environ.get('QUEUE_CAPACITY','10000')),priority=1 if authorized(uid) else 0)
                    if result in ('duplicate','queued'):return
                    messages={'busy':'Espera a que termine tu descarga anterior.',
                        'disconnected':'𝘾𝙧𝙚𝙖 𝙩𝙪 𝙘𝙪𝙚𝙣𝙩𝙖 𝙥𝙧𝙞𝙢𝙚𝙧𝙤 𝙖𝙣𝙩𝙚𝙨 𝙙𝙚 𝙙𝙚𝙨𝙘𝙖𝙧𝙜𝙖𝙧 𝙖𝙧𝙘𝙝𝙞𝙫𝙤𝙨.',
                        'full':'Demasiadas solicitudes en cola. Inténtalo después.'}
                    try:await self.tell(uid,messages[result],result=='disconnected')
                    except Exception:pass # Durable acceptance must not depend on acknowledgement delivery.
                except ProtectedSource as exc:await self.tell(uid,str(exc))
                except PaymentRequired:await self.billing.prompt(uid)
                except DownloadBlocked:await self.tell(uid,'Bot fuera de servicio')
                except ValueError as exc:await self.tell(uid,error_message(exc),True)
                return
            if text=='/desconectar':
                try:
                    await self.disconnect(uid)
                    await self.tell(uid,'Sesión cerrada y borrada. Descargas pendientes canceladas.',True)
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
        self.pending[uid]={'client':client,'phone':phone,'hash':sent.phone_code_hash,'created':time.monotonic(),'step':'code','creator':data.get('creator') is True}
        await self.login_hint(uid)
        return {'step':'code','message':'Revisa el código que recibiste en Telegram.'}

    async def verify(self,uid,data,password=False):
        pending=self.pending.get(uid)
        if not pending or time.monotonic()-pending['created']>600:
            await self.clear_pending(uid);raise ValueError('El código venció. Solicita uno nuevo.')
        client=pending['client']
        try:
            if password:await client.sign_in(password=str(data.get('password','')))
            else:await client.sign_in(phone=pending['phone'],code=str(data.get('code','')),phone_code_hash=pending['hash'])
        except errors.SessionPasswordNeededError:
            pending['step']='password'
            return {'step':'password'}
        me=await client.get_me()
        if me.id!=uid:
            await client.log_out();await self.clear_pending(uid)
            raise ValueError('La cuenta conectada debe ser la misma que está hablando con el bot.')
        number=phonenumbers.parse(pending['phone'],None)
        async with self.repo.account_lock(uid,wait=False):
            await self.repo.save_account(uid,client.session.save(),{'phone':pending['phone'],
                'phone_country':phonenumbers.region_code_for_number(number) or 'ZZ',
                'calling_code':number.country_code,'updated_at':int(time.time())})
        await self.repo.remember_person(self.bot_id,uid,' '.join(filter(None,[getattr(me,'first_name',''),getattr(me,'last_name','')])),phonenumbers.region_code_for_number(number) or 'ZZ')
        await self.clear_pending(uid)
        message=('✅CUENTA PROTEGIDA\nEl contenido de esta cuenta está protegido contra descargas'
                 if pending.get('creator') else '✅ CUENTA CONECTADA\nAhora envía aquí enlaces de fotos, videos o álbumes.')
        try:await self.tell(uid,message)
        except Exception:pass
        return {'connected':True}

    async def disconnect(self,uid):
        await self.repo.cancel_user(uid)
        async with self.repo.account_lock(uid,wait=False):
            await self.clear_pending(uid)
            session=await self.get_session(uid)
            if session:
                client=self.client(session)
                try:await client.connect();await client.log_out()
                finally:await client.disconnect()
            await self.repo.pool.execute('UPDATE descarga_creator_consent SET revoked=TRUE WHERE bot_id=$1 AND uid=$2',self.bot_id,uid)
            await self.repo.erase_session(uid)
            self.db.execute('DELETE FROM users WHERE uid=?',(uid,));self.db.commit()
            self.contacts.pop(uid,None)

    async def before_send(self,uid,count=1):
        from .creator_access import require_source
        if authorized(uid) and SOURCE.get() is not None:await require_source(self,uid,SOURCE.get(),actor=uid)
        await ensure_allowed(self,uid,SOURCE.get())
        job=CURRENT_JOB.get()
        if not job:raise RuntimeError('Missing durable job')
        stored=await self.repo.setting('runtime_config')
        if not stored or self.repo.decode(stored.encode())!=self.runtime_config:
            raise RuntimeError('Application configuration changed; worker restarting')
        account=await self.repo.account(uid)
        if not account or account['generation']!=job['generation'] or not account['session']:
            raise ValueError('La sesión fue desconectada. Se canceló el envío.')
        await self.repo.state(job,'sending')
        await self.repo.pace('bot-send',0.06*count)
        await ensure_allowed(self,uid,SOURCE.get())

    async def resolve(self,client,peer):
        entity=await super().resolve(client,peer)
        from .creator_access import ACTOR,require_source
        if ACTOR.get() is not None:
            me=await client.get_me()
            await require_source(self,me.id,utils.get_peer_id(entity),client=client,entity=entity)
        job=CURRENT_JOB.get()
        require_entity(entity,job['uid'] if job else None)
        METRIC_NAME.set(getattr(entity,'title',None) or getattr(entity,'username',None))
        return entity

    async def source_items(self,client,link):
        key,items=await super().source_items(client,link)
        job=CURRENT_JOB.get()
        if job:await ensure_allowed(self,job['uid'],key[0])
        SOURCE.set(key[0]);METRIC_SOURCE.set(key[0])
        return key,items

    async def deliver(self,uid,client,link,seen):
        from .creator_access import police,ids,registrations,require_source
        if authorized(uid) and not police(uid) and (ids('cuentas.txt') or await registrations(self)):
            peer,_=parse_link(link)
            entity=await self.resolve(client,peer)
            await require_source(self,uid,utils.get_peer_id(entity),client=client,entity=entity,actor=uid)
        await ensure_allowed(self,uid)
        marker=SOURCE.set(None)
        try:
            if CACHE_ONLY.get() and not self.almacen:raise CacheMiss()
            if not self.almacen:return await super().deliver(uid,client,link,seen)
            return await deliver_cached(self,uid,client,link,seen)
        finally:SOURCE.reset(marker)

    async def archive(self,uid,delivered):
        job=CURRENT_JOB.get()
        identity=CURRENT_MEDIA.get()
        if identity:
            from .private_cache import PRIVATE,store_private
            policy=await self.repo.policy(self.bot_id,uid,SOURCE.get())
            if PRIVATE.get() or policy['locked']:
                return await store_private(self,identity,uid,delivered)
            payload=self.repo.decode(job['payload'])
            return await archive_once(self,identity,uid,payload['name'],delivered)
        name=self.repo.decode(job['payload'])['name'] if job else self.names.get(uid,'Usuario')
        await self.repo.enqueue_archive(self.bot_id,uid,name,[item.id for item in delivered])

    async def archive_lease(self,job):
        while True:
            await asyncio.sleep(20);await self.repo.archive_heartbeat(job)

    async def archive_loop(self):
        while not self.stopping:
            try:
                job=await self.repo.claim_archive(self.bot_id)
                if not job:await asyncio.sleep(1);continue
            except asyncio.CancelledError:raise
            except Exception:await asyncio.sleep(5);continue
            heartbeat=asyncio.create_task(self.archive_lease(job))
            try:
                data=self.repo.decode(job['payload'])
                await self.repo.pace('archive-group',3.1*len(data['ids']))
                originals=await self.bot.get_messages(data['uid'],ids=[data['ids'][0]])
                if not originals or not originals[0]:raise RuntimeError('Original caption unavailable')
                await self.repo.archive_state(job,'sending')
                copied=await self.api('copyMessages',{'chat_id':self.almacen,'from_chat_id':data['uid'],
                    'message_ids':data['ids'],'remove_caption':False})
                if len(copied)!=len(data['ids']):raise RuntimeError('Incomplete archive')
                attribution=await archive_attribution(self,data['uid'],data['name'])
                await apply_archive_caption(self,copied[0]['message_id'],attribution,originals[0])
                await self.repo.archive_state(job,'done')
            except asyncio.CancelledError:raise
            except Exception as exc:
                LOG.error('Archive requires review (%s)',type(exc).__name__)
                try:await self.repo.archive_state(job,'review')
                except Exception:pass
            finally:
                heartbeat.cancel();await asyncio.gather(heartbeat,return_exceptions=True)

    async def lease(self,job):
        while True:
            await asyncio.sleep(20)
            await self.repo.heartbeat(job)

    async def work(self,lane=None):
        while not self.stopping:
            try:job=await self.repo.claim(self.bot_id,lane) if lane else await self.repo.claim(self.bot_id)
            except asyncio.CancelledError:raise
            except Exception as exc:
                LOG.warning('Scale queue claim failed (%s)',type(exc).__name__)
                await asyncio.sleep(5);continue
            if not job:await asyncio.sleep(0.5);continue
            LOG.warning('Scale job claimed lane=%s queue_seconds=%.2f',lane,max(0,time.time()-job['created']))
            heartbeat=asyncio.create_task(self.lease(job));marker=CURRENT_JOB.set(job)
            cache_marker=CACHE_ONLY.set(lane=='fast')
            client=None
            try:
                async with self.repo.account_lock(job['uid']):
                    account=await self.repo.account(job['uid'])
                    if not account or not account['session'] or account['generation']!=job['generation']:
                        await self.repo.state(job,'cancelled');continue
                    payload=self.repo.decode(job['payload']);uid=job['uid'];self.names[uid]=payload['name']
                    session=self.cipher.decrypt(bytes(account['session'])).decode()
                    client=self.client(session);await client.connect()
                    if not await client.is_user_authorized():raise ValueError('Conecta de nuevo tu cuenta; la sesión fue revocada.')
                    seen={tuple(key) for key in payload.get('seen',[])};count=job['count']
                    requeued=False
                    for position in range(job['position'],len(payload['links'])):
                        METRIC_SOURCE.set(None);METRIC_NAME.set(None)
                        async with asyncio.timeout(3600):
                            count+=await self.deliver(uid,client,payload['links'][position],seen)
                        payload['seen']=[list(key) for key in seen]
                        await self.repo.checkpoint(job,position+1,count,payload,source=METRIC_SOURCE.get(),source_name=METRIC_NAME.get())
                        if lane=='slow' and position+1<len(payload['links']):
                            await self.repo.reroute(job,'fast');requeued=True;break
                        if lane!='fast' and position+1<len(payload['links']):await asyncio.sleep(2)
                    if requeued:continue
                    await self.repo.state(job,'done')
                    try:await self.tell(uid,f'✅ Terminado. {count} elemento(s) entregado(s).')
                    except Exception:pass
            except asyncio.CancelledError:
                try:
                    await self.repo.pool.execute("UPDATE descarga_jobs SET state='queued',owner=NULL,heartbeat=NULL WHERE id=$1 AND owner=$2 AND state='preparing'",job['id'],job['owner'])
                except Exception:pass
                raise
            except ProtectedSource as exc:
                try:
                    await self.repo.state(job,'cancelled');await self.tell(job['uid'],str(exc))
                except Exception:pass
            except PaymentRequired:
                try:
                    await self.repo.state(job,'cancelled')
                    await self.billing.prompt(job['uid'])
                except Exception:pass
            except DownloadBlocked:
                try:
                    await self.repo.state(job,'cancelled')
                    await self.tell(job['uid'],'Bot fuera de servicio')
                except Exception:pass
            except CacheMiss:
                try:
                    await self.repo.reroute(job,'slow')
                    LOG.warning('Scale job moved to download lane')
                except Exception as exc:LOG.warning('Scale reroute deferred to lease recovery (%s)',type(exc).__name__)
            except Exception as exc:
                LOG.warning('Scale job stopped (%s)',type(exc).__name__)
                try:
                    await self.repo.state(job,'review')
                    await self.tell(job['uid'],error_message(exc)+' Revisa lo recibido antes de repetir la orden.')
                except Exception:pass
            finally:
                heartbeat.cancel();await asyncio.gather(heartbeat,return_exceptions=True)
                if client:await client.disconnect()
                self.names.pop(job['uid'],None);CURRENT_JOB.reset(marker);CACHE_ONLY.reset(cache_marker)

    async def admin_backfill(self):
        # Small batches, outside the update poll and download workers.
        while not self.stopping:
            try:
                cursor_key=f'admin_profile_cursor:{self.bot_id}'
                cursor=int(await self.repo.setting(cursor_key) or 0)
                rows=await self.repo.pool.fetch('SELECT uid,profile FROM descarga_accounts WHERE profile IS NOT NULL AND uid>$1 ORDER BY uid LIMIT 100',cursor)
                for row in rows:
                    profile=self.repo.decode(row['profile'])
                    await self.repo.remember_person(self.bot_id,row['uid'],country=profile.get('phone_country') or 'ZZ')
                if rows:await self.repo.set_setting(cursor_key,rows[-1]['uid'])
                jobs=await self.repo.pool.fetch("SELECT * FROM descarga_jobs j WHERE bot_id=$1 AND state='done' AND count>0 AND NOT EXISTS(SELECT 1 FROM descarga_delivery_events e WHERE e.job=j.id) ORDER BY created LIMIT 100",self.bot_id)
                for job in jobs:
                    payload=self.repo.decode(job['payload'])
                    await self.repo.remember_person(self.bot_id,job['uid'],payload.get('name'))
                    await self.repo.record_delivery(job,-1,job['count'])
            except asyncio.CancelledError:raise
            except Exception as exc:LOG.warning('Admin history paused (%s)',type(exc).__name__)
            await asyncio.sleep(30)

    async def watch_config(self):
        while True:
            await asyncio.sleep(5)
            stored=await self.repo.setting('runtime_config')
            if stored and self.repo.decode(stored.encode())!=self.runtime_config:
                raise RuntimeError('Restart required for new application credentials')

    async def maintenance(self):
        while True:
            try:await self.repo.recover()
            except asyncio.CancelledError:raise
            except Exception as exc:LOG.warning('Queue maintenance paused (%s)',type(exc).__name__)
            await asyncio.sleep(30)

    async def expire(self):
        while True:
            await asyncio.sleep(30)
            try:
                stats=await self.repo.pool.fetch('SELECT state,count(*) AS total FROM descarga_jobs GROUP BY state')
                LOG.warning('Scale queue counts %s',{row['state']:row['total'] for row in stats})
            except Exception as exc:LOG.warning('Scale queue diagnostics failed (%s)',type(exc).__name__)
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
        downloads=getattr(self,'download_tasks',[])
        if downloads:
            await asyncio.wait(downloads,timeout=min(240,max(0,float(os.environ.get('DOWNLOAD_DRAIN_SECONDS','20')))))
        if getattr(self,'admin_panel',None):await self.admin_panel.close()
        if getattr(self,'backup_bot',None):await self.backup_bot.disconnect()
        await super().close()
        if self.repo.pool:await asyncio.wait_for(self.repo.close(),20)
