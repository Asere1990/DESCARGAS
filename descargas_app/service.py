import asyncio
from collections import defaultdict
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import urlparse
import aiohttp
from cryptography.fernet import Fernet
import phonenumbers
from telethon import TelegramClient, errors, types, utils
from telethon.sessions import StringSession
from .media_upload import prepare_media
from .paid_media import purchased_items
from .transfer import prepare_album

LOG=logging.getLogger(__name__)
MAX_FILE=500*1024*1024
MAX_ALBUM=1024*1024*1024


def parse_link(link):
    url=urlparse(link.strip())
    if url.scheme!='https' or url.hostname not in ('t.me','www.t.me'):
        raise ValueError('Envía enlaces HTTPS de mensajes de Telegram.')
    parts=url.path.strip('/').split('/')
    if len(parts)==3 and parts[0]=='c' and all(p.isdigit() for p in parts[1:]):
        return int('-100'+parts[1]),int(parts[2])
    if len(parts)==2 and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,}',parts[0]) and parts[1].isdigit():
        return parts[0],int(parts[1])
    raise ValueError('Copia el enlace de una foto, video o álbum; no una invitación.')


async def album(client,entity,message):
    gid=getattr(message,'grouped_id',None)
    if not gid:return [message]
    found={message.id:message}
    async for item in client.iter_messages(entity,min_id=max(0,message.id-101),max_id=message.id+101,limit=200):
        if getattr(item,'grouped_id',None)==gid:found[item.id]=item
    return [found[key] for key in sorted(found)]


def error_message(exc):
    if isinstance(exc,errors.FloodWaitError):return f'Telegram pide esperar {exc.seconds} segundos. No se reintentará automáticamente.'
    if isinstance(exc,errors.PhoneCodeInvalidError):return 'El código no es correcto. Revísalo y vuelve a intentar.'
    if isinstance(exc,errors.PhoneCodeExpiredError):return 'El código venció. Solicita uno nuevo.'
    if isinstance(exc,errors.PasswordHashInvalidError):return 'La contraseña de dos pasos no es correcta.'
    if isinstance(exc,errors.PhoneNumberInvalidError):return 'El teléfono no es válido.'
    if isinstance(exc,(errors.ChannelPrivateError,errors.ChatAdminRequiredError)):return 'Tu cuenta no tiene acceso a ese contenido.'
    if isinstance(exc,ValueError):
        allowed=('Envía ','Copia ','Acepta ','La cuenta ','Conecta ','El archivo ','El contenido ','El código ','No hay ','Espera ','Demasiadas ','La sesión ','El álbum ')
        if str(exc).startswith(allowed):return str(exc)
    return f'No se pudo completar la operación ({type(exc).__name__}).'


class Service:
    def __init__(self,shared_lock=None):
        self.token=os.environ.get('TOKEN','')
        self.api_id=os.environ.get('ID','').strip()
        self.api_hash=os.environ.get('HASH','').strip()
        self.url=os.environ.get('DESCARGAS_PUBLIC_URL','https://tatica.onrender.com/descargas/').rstrip('/')+'/'
        self.key=os.environ.get('DESCARGAS_SESSION_KEY','')
        raw_store=os.environ.get('ALMACEN','').strip()
        self.almacen=int(raw_store) if re.fullmatch(r'-[1-9][0-9]*',raw_store) else None
        self.names={}
        self.root=Path(os.environ.get('DESCARGAS_DATA_DIR','/var/data/descargas' if Path('/var/data').is_dir() else '/tmp/descargas'))
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.db=sqlite3.connect(str(self.root/'sessions.sqlite3'))
        self.db.execute('CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, session BLOB NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, uid INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS profiles(uid INTEGER PRIMARY KEY, phone BLOB NOT NULL, phone_country TEXT NOT NULL, calling_code INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, consent_version INTEGER NOT NULL)')
        self.db.commit()
        try:self.cipher=Fernet(self.key.encode()) if self.key else None
        except ValueError:self.cipher=None
        self.shared=shared_lock or asyncio.Lock()
        self.locks=defaultdict(asyncio.Lock)
        self.pending={};self.contacts={};self.attempts=defaultdict(list);self.last_code={}
        self.epochs=defaultdict(int);self.active=set();self.queue=asyncio.Queue(maxsize=20)
        self.bot=None;self.http=None;self.tasks=[];self.ready=False;self.state='missing_configuration'

    def configured(self):
        return bool(self.token and self.api_id.isdigit() and re.fullmatch('[a-fA-F0-9]{32}',self.api_hash) and self.cipher)

    def session(self,uid):
        row=self.db.execute('SELECT session FROM users WHERE uid=?',(uid,)).fetchone()
        return self.cipher.decrypt(row[0]).decode() if row and self.cipher else ''

    async def get_session(self,uid):
        return self.session(uid)

    async def before_send(self,uid,count=1):
        pass

    def save_profile(self,uid,phone):
        parsed=phonenumbers.parse(phone,None)
        normalized=phonenumbers.format_number(parsed,phonenumbers.PhoneNumberFormat.E164)
        country=phonenumbers.region_code_for_number(parsed) or 'ZZ'
        now=int(time.time())
        self.db.execute("INSERT INTO profiles VALUES (?,?,?,?,?,?,1) ON CONFLICT(uid) DO UPDATE SET phone=excluded.phone,phone_country=excluded.phone_country,calling_code=excluded.calling_code,updated_at=excluded.updated_at,consent_version=excluded.consent_version",
            (uid,self.cipher.encrypt(normalized.encode()),country,parsed.country_code,now,now))

    def client(self,session=''):
        return TelegramClient(StringSession(session),int(self.api_id),self.api_hash,
            device_model='DESCARGAS · sesión autorizada',app_version='1.0',receive_updates=False,
            flood_sleep_threshold=0,request_retries=3,connection_retries=1,
            raise_last_call_error=True)

    async def api(self,method,payload):
        async with self.http.post(f'https://api.telegram.org/bot{self.token}/{method}',json=payload) as response:
            result=await response.json()
        if not result.get('ok'):
            description=' '.join(str(result.get('description','Unknown error')).split())[:240]
            for secret in (self.token,self.api_hash,self.key):
                if secret:description=description.replace(secret,'[redacted]')
            LOG.error('DESCARGAS BotAPI %s failed code=%s reason=%s',method,result.get('error_code'),description)
            raise RuntimeError('BotAPI request failed')
        return result['result']

    async def tell(self,uid,text,menu=False):
        payload={'chat_id':uid,'text':text}
        if menu:
            connected=bool(await self.get_session(uid))
            payload['reply_markup']={'inline_keyboard':[
                [{'text':'👤 MI CUENTA' if connected else '🔗 CONECTAR MI CUENTA','web_app':{'url':self.url},'style':'primary'}],
                [{'text':'🔒 DESCONECTAR MI CUENTA','callback_data':'disconnect','style':'danger'}]]}
        await self.api('sendMessage',payload)

    async def start(self):
        if not self.configured():return
        self.http=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        self.tasks=[asyncio.create_task(self.bootstrap())]

    def bind_bot(self,bot_id):
        identity=str(bot_id)
        previous=self.db.execute("SELECT value FROM settings WHERE key='bot_id'").fetchone()
        if previous and previous[0]==identity:return
        # Update IDs belong to each bot. User sessions belong to users and remain intact.
        with self.db:
            self.db.execute("DELETE FROM settings WHERE key='offset'")
            self.db.execute('DELETE FROM orders')
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('bot_id',?)",(identity,))

    async def bootstrap(self):
        stage='bot_identity'
        try:
            me=await self.api('getMe',{})
            self.bind_bot(me['id'])
            # Bot session is in memory only; user sessions are encrypted on disk.
            stage='mtproto_connect'
            self.bot=self.client()
            await self.bot.connect()
            stage='mtproto_auth'
            await self.bot.sign_in(bot_token=self.token)
            stage='bot_setup'
            await self.api('deleteWebhook',{'drop_pending_updates':False})
            await self.api('setMyCommands',{'commands':[{'command':'start','description':'Conectar cuenta y descargar'},{'command':'estado','description':'Ver conexión de mi cuenta'},{'command':'desconectar','description':'Cerrar y borrar mi sesión'}]})
            await self.api('setChatMenuButton',{'menu_button':{'type':'web_app','text':'Mi cuenta','web_app':{'url':self.url}}})
            if self.almacen:
                try:
                    await self.api('getChat',{'chat_id':self.almacen})
                    member=await self.api('getChatMember',{'chat_id':self.almacen,'user_id':me['id']})
                    LOG.warning('DESCARGAS ALMACEN access status=%s can_send_messages=%s can_post_messages=%s',
                        member.get('status'),member.get('can_send_messages'),member.get('can_post_messages'))
                except Exception:
                    LOG.error('DESCARGAS ALMACEN access validation failed')
            self.ready=True;self.state='ready'
            self.tasks.extend([asyncio.create_task(self.poll()),asyncio.create_task(self.work()),asyncio.create_task(self.expire())])
        except Exception as exc:
            self.state='startup_error'
            LOG.error('DESCARGAS startup failed at %s (%s)',stage,type(exc).__name__)

    async def poll(self):
        row=self.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
        offset=int(row[0]) if row else None
        while True:
            try:
                updates=await self.api('getUpdates',{'offset':offset if offset is not None else -1,'timeout':25,'allowed_updates':['message','callback_query']})
                for update in updates:
                    if offset is not None:
                        try:await self.update(update)
                        except Exception as exc:LOG.warning('DESCARGAS update failed (%s)',type(exc).__name__)
                    offset=update['update_id']+1
                    self.db.execute("INSERT OR REPLACE INTO settings VALUES('offset',?)",(str(offset),));self.db.commit()
                if offset is None:offset=0
            except asyncio.CancelledError:raise
            except Exception:
                await asyncio.sleep(5)

    async def update(self,update):
        callback=update.get('callback_query')
        if callback:
            await self.api('answerCallbackQuery',{'callback_query_id':callback['id']})
            chat=callback.get('message',{}).get('chat',{})
            uid=callback['from']['id']
            if chat.get('type')!='private' or chat.get('id')!=uid:return
            if callback.get('data')=='disconnect':
                await self.api('sendMessage',{'chat_id':uid,'text':'¿Cerrar y borrar tu sesión? Se cancelarán las descargas pendientes.',
                    'reply_markup':{'inline_keyboard':[[{'text':'Sí, desconectar','callback_data':'confirm_disconnect','style':'danger'}],[{'text':'Cancelar','callback_data':'cancel_disconnect'}]]}})
            elif callback.get('data')=='confirm_disconnect':
                try:
                    await self.disconnect(uid)
                    await self.tell(uid,'Sesión cerrada y borrada.',True)
                except Exception as exc:await self.tell(uid,error_message(exc),True)
            elif callback.get('data')=='cancel_disconnect':
                await self.tell(uid,'Conservas tu conexión.',True)
            return
        message=update.get('message',{})
        if message.get('chat',{}).get('type')!='private' or message.get('from',{}).get('is_bot'):return
        uid=message['from']['id']
        person=message['from']
        self.names[uid]=' '.join((str(person.get('first_name',''))+' '+str(person.get('last_name',''))).split())[:128] or str(person.get('username','')) or f'Usuario {uid}'
        contact=message.get('contact')
        if contact and contact.get('user_id')==uid and not message.get('forward_origin'):
            self.contacts[uid]=(contact['phone_number'],time.monotonic());return
        text=message.get('text','').strip()
        if text.startswith('/start'):
            if await self.get_session(uid):
                await self.tell(uid,'✅ CUENTA CONECTADA\n\nEnvía aquí hasta 5 enlaces de fotos, videos, álbumes o archivos por mensaje.',True)
                return
            await self.tell(uid,"𝘿𝙀𝙎𝘾𝘼𝙍𝙂𝘼𝙎📥\n\n𝘾𝙧𝙚𝙖 𝙩𝙪 𝙘𝙪𝙚𝙣𝙩𝙖 𝙥𝙖𝙧𝙖 𝙙𝙚𝙨𝙘𝙖𝙧𝙜𝙖𝙧 𝙛𝙤𝙩𝙤𝙨, 𝙫𝙞𝙙𝙚𝙤𝙨, 𝙖𝙡𝙗𝙪𝙢𝙨, 𝙖𝙧𝙘𝙝𝙞𝙫𝙤𝙨 𝙤 𝙜𝙞𝙛𝙨 𝙙𝙚 𝙜𝙧𝙪𝙥𝙤𝙨, 𝙘𝙖𝙣𝙖𝙡𝙚𝙨 𝙤 𝙗𝙤𝙩𝙨 𝙖𝙪𝙣𝙦𝙪𝙚 𝙩𝙚𝙣𝙜𝙖𝙣 𝙧𝙚𝙨𝙩𝙧𝙞𝙘𝙘𝙞𝙤́𝙣 𝙙𝙚 𝙙𝙚𝙨𝙘𝙖𝙧𝙜𝙖𝙨. 𝘾𝙪𝙖𝙣𝙙𝙤 𝙩𝙚𝙧𝙢𝙞𝙣𝙚𝙨 𝙙𝙚 𝙘𝙧𝙚𝙖𝙧 𝙡𝙖 𝙘𝙪𝙚𝙣𝙩𝙖, 𝙨𝙤𝙡𝙤 𝙚𝙣𝙫𝙞𝙖 𝙚𝙡 𝙚𝙣𝙡𝙖𝙘𝙚 𝙖 𝙙𝙚𝙨𝙘𝙖𝙧𝙜𝙖𝙧 𝙖𝙦𝙪𝙞 𝙥𝙖𝙧𝙖 𝙙𝙚𝙫𝙤𝙡𝙫𝙚𝙧𝙩𝙚 𝙚𝙡 𝙖𝙧𝙘𝙝𝙞𝙫𝙤 𝙙𝙚𝙨𝙘𝙖𝙧𝙜𝙖𝙙𝙤 𝙚𝙣 𝙨𝙚𝙜𝙪𝙣𝙙𝙤𝙨.",True)
        elif text=='/estado':
            await self.tell(uid,'✅ Cuenta conectada. Envía los enlaces.' if self.session(uid) else 'Conecta primero tu cuenta.',True)
        elif text=='/desconectar':
            await self.disconnect(uid);await self.tell(uid,'Sesión cerrada y borrada. Descargas pendientes canceladas.',True)
        elif text.startswith('https://'):
            links=list(dict.fromkeys(text.split()))
            try:
                if len(links)>5:raise ValueError('Envía un máximo de 5 enlaces por mensaje. Divide los enlaces en lotes de cinco.')
                for link in links:parse_link(link)
                if not self.session(uid):raise ValueError('Conecta primero tu cuenta desde el botón.')
                if uid in self.active:raise ValueError('Espera a que termine tu descarga anterior.')
                if self.queue.full():raise ValueError('Demasiadas descargas en cola. Inténtalo después.')
                if self.db.execute('SELECT 1 FROM orders WHERE id=?',(update['update_id'],)).fetchone():return
                self.db.execute('INSERT INTO orders VALUES (?,?)',(update['update_id'],uid));self.db.commit()
                self.active.add(uid);self.queue.put_nowait((uid,links,self.epochs[uid]))
                await self.tell(uid,f'Recibido: {len(links)} enlace(s). Te entregaré el contenido aquí.')
            except Exception as exc:await self.tell(uid,error_message(exc),True)
        elif text:
            await self.tell(uid,'Envía enlaces de mensajes de Telegram. Para conectar tu cuenta usa el botón; no envíes aquí códigos ni contraseñas.',True)

    def throttle(self,uid):
        now=time.monotonic();self.attempts[uid]=[t for t in self.attempts[uid] if now-t<60]
        if len(self.attempts[uid])>=10:raise ValueError('Demasiadas solicitudes. Espera un minuto.')
        self.attempts[uid].append(now)

    async def login_status(self,uid):
        # Only disclose the stage belonging to the authenticated Mini App user.
        if await self.get_session(uid):return {'connected':True}
        pending=self.pending.get(uid)
        step='phone'
        if pending and time.monotonic()-pending['created']<=600:
            step='password' if pending.get('step')=='password' else 'code'
        return {'connected':False,'step':step}

    async def send_code(self,uid,data):
        if data.get('consent') is not True:raise ValueError('Acepta el acceso de la sesión antes de continuar.')
        if self.session(uid):return {'connected':True}
        if time.monotonic()-self.last_code.get(uid,-1000)<60:raise ValueError('Espera un minuto antes de solicitar otro código.')
        if len(self.pending)>=20 and uid not in self.pending:raise ValueError('Demasiadas conexiones en curso. Inténtalo después.')
        try:
            phone=phonenumbers.parse(str(data.get('phone','')),None)
            if not phonenumbers.is_valid_number(phone):raise ValueError()
            phone=phonenumbers.format_number(phone,phonenumbers.PhoneNumberFormat.E164)
        except Exception:raise errors.PhoneNumberInvalidError(request=None)
        self.last_code[uid]=time.monotonic()
        await self.clear_pending(uid)
        client=self.client();await client.connect()
        try:sent=await client.send_code_request(phone)
        except BaseException:
            await client.disconnect();raise
        self.pending[uid]={'client':client,'phone':phone,'hash':sent.phone_code_hash,'created':time.monotonic(),'step':'code'}
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
        token=self.cipher.encrypt(client.session.save().encode())
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO users VALUES (?,?)',(uid,token))
            self.save_profile(uid,pending['phone'])
        await self.clear_pending(uid)
        try:await self.tell(uid,'✅ CUENTA CONECTADA\nAhora envía aquí enlaces de fotos, videos o álbumes. Para cerrar y borrar la sesión: /desconectar.')
        except Exception:pass
        return {'connected':True}

    async def login_hint(self,uid):
        pending=self.pending.get(uid)
        if not pending:return
        name=self.names.get(uid,'').strip()
        try:
            person=await self.api('getChat',{'chat_id':uid})
            name=str(person.get('first_name') or name).strip()
        except Exception:pass
        greeting=(name or 'Hola')+' revisa'
        try:
            message=await self.api('sendMessage',{'chat_id':uid,'text':
                '🔐 𝐂𝐎𝐃𝐈𝐆𝐎 𝐃𝐄 𝐓𝐄𝐋𝐄𝐆𝐑𝐀𝐌\n\n'
                f'{greeting} en tus chats el mensaje con el código de 5 cifras enviado por Telegram, luego ve a «𝐌𝐢 𝐂𝐮𝐞𝐧𝐭𝐚», abajo a la izquierda, e introduce el código en la ventana que se abre de forma segura.\n\n'
                '𝐍𝐎𝐓𝐀: No envíes el código como mensaje en este chat.'})
            pending['hint_message_id']=message['message_id']
        except Exception:
            # A failed instructional message must not interrupt authentication.
            pass

    async def clear_pending(self,uid):
        pending=self.pending.pop(uid,None)
        if not pending:return
        try:
            if pending.get('hint_message_id'):
                try:await self.api('deleteMessage',{'chat_id':uid,'message_id':pending['hint_message_id']})
                except Exception:pass
        finally:await pending['client'].disconnect()

    async def disconnect(self,uid):
        self.epochs[uid]+=1
        async with self.locks[uid]:
            await self.clear_pending(uid)
            session=self.session(uid)
            if session:
                client=self.client(session)
                try:
                    await client.connect();await client.log_out()
                except Exception:
                    raise ValueError('La sesión no pudo revocarse en Telegram. Inténtalo otra vez o ciérrala en Ajustes → Dispositivos.')
                finally:await client.disconnect()
            with self.db:
                self.db.execute('DELETE FROM users WHERE uid=?',(uid,))
            self.contacts.pop(uid,None)

    async def expire(self):
        while True:
            await asyncio.sleep(30)
            for uid,p in list(self.pending.items()):
                if time.monotonic()-p['created']>600:
                    async with self.locks[uid]:await self.clear_pending(uid)
            for uid,(_,created) in list(self.contacts.items()):
                if time.monotonic()-created>600:self.contacts.pop(uid,None)

    async def resolve(self,client,peer):
        if isinstance(peer,int):
            async for dialog in client.iter_dialogs():
                if dialog.id==peer:return dialog.entity
            raise ValueError('La cuenta no tiene acceso al grupo del enlace.')
        return await client.get_entity(peer)

    async def work(self):
        while True:
            uid,links,epoch=await self.queue.get()
            try:
                async with self.shared,self.locks[uid]:
                    if epoch!=self.epochs[uid]:continue
                    session=self.session(uid)
                    if not session:continue
                    client=self.client(session)
                    try:
                        await client.connect()
                        if not await client.is_user_authorized():raise ValueError('Conecta de nuevo tu cuenta; la sesión fue revocada.')
                        seen=set();count=0
                        for link in links:
                            if epoch!=self.epochs[uid]:break
                            async with asyncio.timeout(3600):
                                count+=await self.deliver(uid,client,link,seen)
                            await asyncio.sleep(2)
                        await self.tell(uid,f'✅ Terminado. {count} elemento(s) entregado(s).')
                    finally:await client.disconnect()
            except asyncio.CancelledError:raise
            except Exception as exc:
                try:await self.tell(uid,error_message(exc)+' Revisa lo recibido antes de repetir la orden.')
                except Exception:pass
            finally:
                self.active.discard(uid);self.queue.task_done()

    async def source_items(self,client,link):
        peer,mid=parse_link(link);entity=await self.resolve(client,peer)
        message=await client.get_messages(entity,ids=mid)
        paid=purchased_items(message)
        if not message or (paid is None and not (message.photo or message.document)):
            raise ValueError('El contenido no contiene una foto, video o archivo descargable.')
        key=(utils.get_peer_id(entity),getattr(message,'grouped_id',None) or mid)
        items=paid if paid is not None else await album(client,entity,message)
        if not items or any(not (m.photo or m.document) for m in items):
            raise ValueError('El álbum no contiene multimedia descargable completa.')
        return key,items

    async def deliver(self,uid,client,link,seen):
        key,items=await self.source_items(client,link)
        return await self.deliver_items(uid,client,key,items,seen)

    async def deliver_items(self,uid,client,key,items,seen):
        from .progress import Progress,ACTIVE
        if key in seen:return 0
        progress=Progress(self,uid,[m.file.size or 0 for m in items]).start()
        marker=ACTIVE.set(progress)
        try:return await self._deliver_items(uid,client,key,items,seen)
        finally:
            ACTIVE.reset(marker)
            await progress.close()

    async def _deliver_items(self,uid,client,key,items,seen):
        if key in seen:return 0
        seen.add(key)
        size=sum((m.file.size or 0) for m in items)
        if size>MAX_ALBUM or any((m.file.size or 0)>MAX_FILE for m in items):raise ValueError('El archivo supera 500 MB o el álbum supera 1 GB.')
        with tempfile.TemporaryDirectory(prefix='descargas-') as directory:
            started=time.monotonic()
            async def prepare(path,item):
                return await prepare_media_for_bot(self.bot,client,path,item)
            media=await prepare_album(client,items,directory,prepare,MAX_FILE,MAX_ALBUM)
            LOG.warning('Transfer prepared items=%s bytes=%s seconds=%.2f',len(items),size,time.monotonic()-started)
            from .progress import ACTIVE
            if ACTIVE.get():ACTIVE.get().phase='sending'
            await self.before_send(uid,len(media))
            result=await self.bot.send_file(uid,media if len(media)>1 else media[0],
                caption=[m.message or '' for m in items] if len(items)>1 else items[0].message or '',
                formatting_entities=[m.entities or [] for m in items] if len(items)>1 else items[0].entities or [],parse_mode=None)
            LOG.warning('Transfer delivered items=%s total_seconds=%.2f',len(items),time.monotonic()-started)
            delivered=result if isinstance(result,list) else [result]
            if len(delivered)!=len(items):raise ValueError('El álbum se entregó incompleto. Revisa lo recibido antes de repetir.')
            if self.almacen:
                try:
                    await self.archive(uid,delivered)
                except Exception as exc:
                    LOG.error('DESCARGAS archive failed (%s)',type(exc).__name__)
                    await self.tell(uid,'Tu descarga fue entregada, pero no se pudo completar la copia al almacén. No necesitas repetirla.')
            return len(items)

    async def archive(self,uid,delivered):
        # Bot API resolves destination IDs without a Telethon entity/access-hash cache.
        archived=await self.api('copyMessages',{'chat_id':self.almacen,'from_chat_id':uid,
            'message_ids':[item.id for item in delivered],'remove_caption':True})
        if len(archived)!=len(delivered):raise ValueError('El álbum no se guardó completo en el almacén.')
        attribution=await archive_attribution(self,uid,self.names.get(uid,f'Usuario {uid}'))
        await self.api('editMessageCaption',{'chat_id':self.almacen,
            'message_id':archived[0]['message_id'],**attribution})

    async def close(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        for uid in list(self.pending):await self.clear_pending(uid)
        if self.bot:await self.bot.disconnect()
        if self.http:await self.http.close()
        self.db.close()


async def archive_attribution(service,uid,name):
    username=None
    try:
        chat=await service.api('getChat',{'chat_id':uid})
        if isinstance(chat,dict):username=chat.get('username')
    except Exception as exc:
        LOG.warning('Archive public profile unavailable (%s)',type(exc).__name__)
    caption,entities=archive_caption(name,uid,username)
    return {'caption':caption,'caption_entities':[{'type':'text_link','offset':e.offset,
        'length':e.length,'url':e.url} for e in entities]}


def archive_caption(name,uid,username=None):
    name=' '.join(name.split())[:128] or f'Usuario {uid}'
    prefix='Usuario: '
    middle='\nTelegram ID: '
    text=prefix+name+middle+str(uid)
    units=lambda value:len(value.encode('utf-16-le'))//2
    url=f'https://t.me/{username}' if username and re.fullmatch(r'[A-Za-z0-9_]{1,32}',username) else f'tg://user?id={uid}'
    return text,[types.MessageEntityTextUrl(offset=units(prefix),length=units(name),url=url),
                 types.MessageEntityTextUrl(offset=units(prefix+name+middle),length=len(str(uid)),url=url)]


async def prepare_media_for_bot(bot,source,path,original):
    class UploadBridge:
        upload_file=bot.upload_file
        download_media=source.download_media
    return await prepare_media(UploadBridge(),path,original)
