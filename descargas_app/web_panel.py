"""Independent administrative Telegram-style viewer; never changes Mini App routes."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timezone
import hashlib
import html
import io
import json
import math
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit
from aiohttp import web
from PIL import Image
from telethon import types,utils,errors
from .moderation import authorized
from .creator_access import ACTOR,require_account,require_source
from .account_chat import connected
from .protected import require_entity

ROOT=Path(__file__).with_name('panel_web')
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
 'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}

def integer(value,minimum=1):
    try:n=int(value)
    except (TypeError,ValueError):raise ValueError('Identificador no válido.')
    if not minimum<=n<2**63:raise ValueError('Identificador no válido.')
    return n

def name(entity):
    return (' '.join(filter(None,[getattr(entity,'first_name',None),getattr(entity,'last_name',None)])) or getattr(entity,'title',None) or 'Sin nombre')[:160]

def account_label(profile,directory=None):
    profile=profile or {};directory=directory or {}
    candidates=[profile.get('name'),' '.join(filter(None,[profile.get('first_name'),profile.get('last_name')])),directory.get('name')]
    for value in candidates:
        if not isinstance(value,str):continue
        value=' '.join(value.split())
        if value and not value.replace('+','').isdigit() and value.casefold() not in ('sin nombre','nombre no disponible','no disponible'):
            return value[:160]
    return '@'+str(profile['username'])[:100] if profile.get('username') else 'Cuenta de Telegram'

def peer_data(entity,uid):
    if isinstance(entity,types.User):return {'kind':'self' if entity.id==uid else 'user','id':entity.id,'hash':entity.access_hash or 0}
    if isinstance(entity,types.Channel):return {'kind':'channel','id':entity.id,'hash':entity.access_hash or 0}
    if isinstance(entity,types.Chat):return {'kind':'chat','id':entity.id,'hash':0}
    raise ValueError('Conversación no disponible.')

def input_peer(data):
    return {'self':lambda:types.InputPeerSelf(),'user':lambda:types.InputPeerUser(data['id'],data['hash']),
      'channel':lambda:types.InputPeerChannel(data['id'],data['hash']),'chat':lambda:types.InputPeerChat(data['id'])}[data['kind']]()

def source_id(data):return data['id'] if data['kind'] in ('user','self') else -(1000000000000+data['id']) if data['kind']=='channel' else -data['id']

def media_info(message):
    media=getattr(message,'media',None)
    if getattr(media,'ttl_seconds',None) or getattr(message,'ttl_period',None):return None
    if getattr(message,'photo',None):
        sizes=getattr(message.photo,'sizes',[]) or []
        size=max([getattr(x,'size',0) or max(getattr(x,'sizes',[]) or [0]) for x in sizes]+[0])
        return {'type':'photo','mime':'image/jpeg','size':size,'name':'foto.jpg','duration':0}
    doc=getattr(message,'document',None)
    if not doc:return None
    attrs=doc.attributes or [];filename=next((a.file_name for a in attrs if isinstance(a,types.DocumentAttributeFilename)),'archivo')
    video=next((a for a in attrs if isinstance(a,types.DocumentAttributeVideo)),None)
    audio=next((a for a in attrs if isinstance(a,types.DocumentAttributeAudio)),None)
    sticker=any(isinstance(a,types.DocumentAttributeSticker) for a in attrs)
    kind='video' if video else 'voice' if audio and audio.voice else 'audio' if audio else 'sticker' if sticker else 'document'
    return {'type':kind,'mime':doc.mime_type or 'application/octet-stream','size':doc.size,'name':filename[:150],
      'duration':getattr(video or audio,'duration',0) or 0}

def message_data(m,protected=False):
    if getattr(m,'ttl_period',None) or getattr(getattr(m,'media',None),'ttl_seconds',None):return None
    info=media_info(m);sender=getattr(m,'sender',None)
    return {'id':m.id,'date':m.date.timestamp() if m.date else 0,'out':bool(m.out),'text':str(getattr(m,'message','') or '')[:16000],
      'sender':name(sender) if sender else '', 'sender_id':getattr(m,'sender_id',None),
      'reply':getattr(getattr(m,'reply_to',None),'reply_to_msg_id',None),'grouped':str(m.grouped_id) if getattr(m,'grouped_id',None) else None,
      'media':info,'protected':bool(protected or getattr(m,'noforwards',False))}

async def access_link(s,actor):
    if not authorized(actor):raise ValueError('Acceso no autorizado.')
    version=await s.repo.setting(f'web-panel-version:{s.bot_id}:{actor}') or '0'
    code=s.cipher.encrypt(json.dumps({'purpose':'access','actor':actor,'bot':s.bot_id,'version':version}).encode()).decode()
    return s.url.rstrip('/')+'/panel#'+code

class TelegramPanel:
    def __init__(self,service):
        self.s=service;self.rates={};self.slots=asyncio.Semaphore(3);self.transfers=asyncio.Semaphore(2);self.profiles={};self.profile_slots=asyncio.Semaphore(2)
    def pack(self,purpose,**values):return self.s.cipher.encrypt(json.dumps({'purpose':purpose,'bot':self.s.bot_id,**values},separators=(',',':')).encode()).decode()
    def unpack(self,value,kind):
        if not isinstance(value,str) or len(value)>4000:raise ValueError('Acceso no válido.')
        try:data=json.loads(self.s.cipher.decrypt(value.encode()))
        except Exception:raise ValueError('Acceso no válido.') from None
        if data.get('purpose')!=kind or data.get('bot')!=self.s.bot_id:raise ValueError('Acceso no válido.')
        return data
    def rate(self,key,max_count):
        now=time.monotonic();at,count=self.rates.get(key,(now,0))
        if now-at>60:at,count=now,0
        self.rates[key]=(at,count+1)
        if len(self.rates)>5000:self.rates={k:v for k,v in self.rates.items() if now-v[0]<60}
        if count>=max_count:raise web.HTTPTooManyRequests(text='Espera un momento antes de repetir.')
    async def session(self,request):
        data=self.unpack(request.cookies.get('descargas_panel',''),'session');actor=integer(data.get('actor'))
        if not authorized(actor):raise web.HTTPForbidden()
        if data.get('version')!=(await self.s.repo.setting(f'web-panel-version:{self.s.bot_id}:{actor}') or '0'):raise web.HTTPUnauthorized()
        return data
    @asynccontextmanager
    async def account(self,actor,uid,timeout=45):
        if not authorized(actor):raise web.HTTPForbidden()
        try:await require_account(self.s,uid,actor)
        except ValueError:raise web.HTTPForbidden() from None
        token=ACTOR.set(actor)
        try:
            if self.slots.locked():raise ValueError('Hay varias consultas en curso. Reintenta en unos segundos.')
            async with self.slots,asyncio.timeout(timeout),connected(self.s,uid) as client:
                client.flood_sleep_threshold=0
                row=await self.s.repo.account(uid)
                yield client,row['generation']
                if not authorized(actor):raise web.HTTPForbidden()
                await require_account(self.s,uid,actor)
        finally:ACTOR.reset(token)
    async def target(self,actor,uid,generation,client,handle):
        data=self.unpack(handle,'peer')
        if data.get('uid')!=uid or data.get('generation')!=generation or data.get('actor')!=actor:raise ValueError('El chat no corresponde a esta cuenta. Abre su lista otra vez.')
        entity=await client.get_entity(input_peer(data));source=utils.get_peer_id(entity)
        if source!=source_id(data):raise ValueError('Conversación no válida.')
        try:
            await require_source(self.s,uid,source,client=client,entity=entity,actor=actor);require_entity(entity,uid)
        except ValueError:raise web.HTTPForbidden() from None
        return entity
    async def accounts(self,actor,after=0):
        rows=await self.s.repo.pool.fetch('SELECT a.uid,a.profile,d.name FROM descarga_accounts a LEFT JOIN descarga_directory d ON d.uid=a.uid AND d.bot_id=$2 WHERE a.session IS NOT NULL AND a.uid>$1 ORDER BY a.uid LIMIT 61',after,self.s.bot_id)
        items=[]
        for row in rows[:60]:
            try:await require_account(self.s,row['uid'],actor)
            except ValueError:continue
            profile=self.s.repo.decode(row['profile']) if row['profile'] else {}
            directory=self.s.repo.decode(row['name']) if row.get('name') else {}
            items.append({'id':str(row['uid']),'name':account_label(profile,directory),'username':profile.get('username') or ''})
        return {'items':items,'next':str(rows[59]['uid']) if len(rows)>60 else None}
    async def account_profile(self,actor,uid):
        if not authorized(actor):raise web.HTTPForbidden()
        try:await require_account(self.s,uid,actor)
        except ValueError:raise web.HTTPForbidden() from None
        row=await self.s.repo.pool.fetchrow('SELECT a.profile,d.name FROM descarga_accounts a LEFT JOIN descarga_directory d ON d.uid=a.uid AND d.bot_id=$2 WHERE a.uid=$1 AND a.session IS NOT NULL',uid,self.s.bot_id)
        if not row:raise web.HTTPNotFound()
        key=(actor,uid);cached=self.profiles.get(key)
        if cached and cached['expires']>time.monotonic():return cached
        profile=self.s.repo.decode(row['profile']) if row['profile'] else {}
        directory=self.s.repo.decode(row['name']) if row.get('name') else {}
        result={'name':account_label(profile,directory),'username':profile.get('username') or '', 'file':None,'expires':time.monotonic()+30}
        async with self.profile_slots:
            # The bot retrieves a public Telegram profile; no user session is opened.
            try:
                async with asyncio.timeout(8):info=await self.s.api('getChat',{'chat_id':uid})
                if info.get('id')!=uid:raise ValueError('Profile does not match account')
                result.update(name=account_label(info,{'name':result['name']}),username=info.get('username') or '',file=(info.get('photo') or {}).get('small_file_id'),expires=time.monotonic()+600)
            except asyncio.CancelledError:raise
            except Exception:pass
        if not authorized(actor):raise web.HTTPForbidden()
        try:await require_account(self.s,uid,actor)
        except ValueError:raise web.HTTPForbidden() from None
        if len(self.profiles)>=512:self.profiles.pop(next(iter(self.profiles)))
        self.profiles[key]=result
        return result
    async def account_avatar(self,actor,uid):
        profile=await self.account_profile(actor,uid)
        if not profile['file']:return web.Response(status=204,headers=HEADERS)
        data=profile.get('image')
        if data is None:
            async with self.profile_slots,asyncio.timeout(15):
                metadata=await self.s.api('getFile',{'file_id':profile['file']})
                filename=metadata.get('file_path','')
                if not re.fullmatch(r'[a-zA-Z0-9_/.-]{1,300}',filename) or '..' in filename:raise ValueError('Foto no disponible.')
                # Telegram's bot token stays server-side and is never returned in an image URL.
                async with self.s.http.get('https://api.telegram.org/file/bot'+self.s.token+'/'+filename,allow_redirects=False) as response:
                    if response.status!=200:return web.Response(status=204,headers=HEADERS)
                    chunks=[];size=0
                    async for chunk in response.content.iter_chunked(65536):
                        size+=len(chunk)
                        if size>2*1024*1024:raise ValueError('Foto demasiado grande.')
                        chunks.append(chunk)
                def thumbnail():
                    with Image.open(io.BytesIO(b''.join(chunks))) as image:
                        if image.width*image.height>4000000:raise ValueError('Foto demasiado grande.')
                        image.thumbnail((96,96));out=io.BytesIO();image.convert('RGB').save(out,format='JPEG',quality=80);return out.getvalue()
                data=await asyncio.to_thread(thumbnail)
                if sum(len(v.get('image',b'')) for v in self.profiles.values())+len(data)<4*1024*1024:profile['image']=data
        if not authorized(actor):raise web.HTTPForbidden()
        try:await require_account(self.s,uid,actor)
        except ValueError:raise web.HTTPForbidden() from None
        return web.Response(body=data,content_type='image/jpeg',headers=HEADERS)
    async def chats(self,actor,uid,cursor=''):
        async with self.account(actor,uid) as (client,generation):
            kwargs={'limit':61}
            if cursor:
                c=self.unpack(cursor,'dialogs')
                if c.get('actor')!=actor or c.get('uid')!=uid or c.get('generation')!=generation:raise ValueError('Recarga la lista de chats.')
                kwargs.update(offset_id=c['offset'],offset_date=datetime.fromtimestamp(c['date'],timezone.utc),offset_peer=input_peer(c['peer']),ignore_pinned=True)
            rows=[d async for d in client.iter_dialogs(**kwargs)];items=[]
            for d in rows[:60]:
                entity=d.entity
                try:
                    data=peer_data(entity,uid)
                    await require_source(self.s,uid,d.id,client=client,entity=entity,actor=actor);require_entity(entity,uid)
                except ValueError:continue
                handle=self.pack('peer',actor=actor,uid=uid,generation=generation,**data)
                last=message_data(d.message,bool(getattr(entity,'noforwards',False))) if d.message else None
                items.append({'id':str(d.id),'handle':handle,'name':'Mensajes guardados' if d.id==uid else name(entity),
                 'type':'private' if isinstance(entity,types.User) else 'channel' if getattr(entity,'broadcast',False) else 'group',
                 'date':d.date.timestamp() if d.date else 0,'unread':d.unread_count,'pinned':bool(d.pinned),
                 'preview':last['text'] or (last['media']['type'] if last['media'] else '') if last else '',
                 'avatar':bool(getattr(entity,'photo',None))})
            next_cursor=None
            if len(rows)>60:
                d=rows[59];next_cursor=self.pack('dialogs',actor=actor,uid=uid,generation=generation,offset=d.message.id if d.message else 0,date=d.date.timestamp() if d.date else 0,peer=peer_data(d.entity,uid))
            me=await client.get_me()
            return {'items':items,'next':next_cursor,'account':name(me),'generation':generation}
    async def messages(self,actor,uid,handle,before=0,gallery=False,query=''):
        async with self.account(actor,uid) as (client,generation):
            entity=await self.target(actor,uid,generation,client,handle)
            kwargs={'limit':61,'offset_id':before}
            if gallery:kwargs['filter']=types.InputMessagesFilterPhotoVideo()
            if query:kwargs['search']=query[:100]
            rows=[m async for m in client.iter_messages(entity,**kwargs)]
            items=[v for m in rows[:60] if (v:=message_data(m,bool(getattr(entity,'noforwards',False))))]
            if gallery:items=[v for v in items if v['media'] and v['media']['type'] in ('photo','video')]
            try:await require_source(self.s,uid,utils.get_peer_id(entity),client=client,entity=entity,actor=actor)
            except ValueError:raise web.HTTPForbidden() from None
            return {'items':list(reversed(items)),'next':rows[59].id if len(rows)>60 else None,'title':name(entity)}
    async def media(self,request,actor,uid,handle,mid,thumb=False,avatar=False):
        # Transfers are streamed and never saved to the download queue, ALMACEN or Dropbox.
        if self.transfers.locked():raise ValueError('Hay dos archivos abiertos. Espera un momento.')
        async with self.transfers,self.account(actor,uid,timeout=300) as (client,generation):
            entity=await self.target(actor,uid,generation,client,handle)
            if avatar:
                data=await client.download_profile_photo(entity,file=bytes,download_big=False)
                if not data:return web.Response(status=204,headers=HEADERS)
                await require_source(self.s,uid,utils.get_peer_id(entity),client=client,entity=entity,actor=actor)
                if not authorized(actor):raise web.HTTPForbidden()
                return web.Response(body=data,content_type='image/jpeg',headers=HEADERS)
            m=await client.get_messages(entity,ids=mid)
            if not m:raise ValueError('Archivo no disponible.')
            protected=bool(getattr(entity,'noforwards',False) or getattr(m,'noforwards',False))
            if protected and request.query.get('download')=='1':raise web.HTTPForbidden(reason='Solo visualización.')
            info=media_info(m)
            if not info:raise ValueError('Archivo no disponible o temporal.')
            if thumb:
                # Telegram thumbnails are small; never substitute a full video download.
                if not (getattr(m,'photo',None) or getattr(getattr(m,'document',None),'thumbs',None)):return web.Response(status=204,headers=HEADERS)
                data=await client.download_media(m,file=bytes,thumb='m' if getattr(m,'photo',None) else 0)
                if not data:return web.Response(status=204,headers=HEADERS)
                if len(data)>2*1024*1024:raise ValueError('Miniatura demasiado grande.')
                if not authorized(actor):raise web.HTTPForbidden()
                await require_source(self.s,uid,utils.get_peer_id(entity),client=client,entity=entity,actor=actor)
                return web.Response(body=data,content_type='image/jpeg',headers=HEADERS)
            size=info['size'];start=0;end=size-1;status=200
            if request.headers.get('Range'):
                match=re.fullmatch(r'bytes=(\d+)-(\d*)',request.headers['Range'])
                if not match or not size:raise web.HTTPRequestRangeNotSatisfiable()
                start=int(match[1]);end=min(size-1,int(match[2]) if match[2] else size-1);status=206
                if start>end or start>=size:raise web.HTTPRequestRangeNotSatisfiable()
            safe=bool(re.fullmatch(r'(?:image/(?:jpeg|png|webp|gif)|video/(?:mp4|webm)|audio/(?:ogg|mpeg|mp4|aac|wav|webm))',info['mime']))
            if protected and not safe:raise web.HTTPForbidden(reason='Este archivo protegido no admite visualización en el panel.')
            headers={**HEADERS,'Content-Type':info['mime'] if safe else 'application/octet-stream','Accept-Ranges':'bytes',
             'Content-Disposition':('inline' if safe and request.query.get('download')!='1' else 'attachment')+'; filename="'+re.sub(r'[^a-zA-Z0-9._-]','_',info['name'])+'"'}
            if size:headers['Content-Length']=str(end-start+1)
            if status==206:headers['Content-Range']=f'bytes {start}-{end}/{size}'
            response=web.StreamResponse(status=status,headers=headers);await response.prepare(request)
            aligned=start-start%4096;position=aligned;checked=0
            try:
                iterator=client.iter_download(m.media,offset=aligned,request_size=262144,chunk_size=262144)
                async for chunk in iterator:
                    if not authorized(actor):break
                    if time.monotonic()-checked>5:
                        await require_account(self.s,uid,actor)
                        await require_source(self.s,uid,utils.get_peer_id(entity),client=client,entity=entity,actor=actor)
                        checked=time.monotonic()
                    left=max(0,start-position);right=min(len(chunk),end-position+1) if size else len(chunk)
                    if right>left:await response.write(chunk[left:right])
                    position+=len(chunk)
                    if size and position>end:break
            except (ConnectionResetError,asyncio.CancelledError):raise
            finally:await iterator.close()
            await response.write_eof();return response
    async def handle(self,request):
        try:
            if not self.s.ready:return web.json_response({'error':'El servicio está iniciándose.'},status=503,headers=HEADERS)
            op=request.match_info['op'];post=request.method=='POST'
            if post:
                expected=urlsplit(self.s.url)
                if request.headers.get('Origin')!=f'{expected.scheme}://{expected.netloc}':raise web.HTTPForbidden()
            if op=='login' and post:
                self.rate('login:'+str(request.remote),15);body=await request.json();data=self.unpack(body.get('token'),'access');actor=integer(data.get('actor'))
                if not authorized(actor) or data.get('version')!=(await self.s.repo.setting(f'web-panel-version:{self.s.bot_id}:{actor}') or '0'):raise web.HTTPUnauthorized()
                csrf=secrets.token_urlsafe(32);value=self.pack('session',actor=actor,version=data['version'],csrf=csrf)
                response=web.json_response({'ok':True},headers=HEADERS);response.set_cookie('descargas_panel',value,httponly=True,secure=expected.scheme=='https',samesite='Strict',max_age=400*86400,path=request.path.split('/api/')[0] or '/');return response
            try:session=await self.session(request)
            except ValueError:raise web.HTTPUnauthorized()
            actor=session['actor'];self.rate('actor:'+str(actor),240)
            if post and not secrets.compare_digest(request.headers.get('X-Panel-CSRF',''),session['csrf']):raise web.HTTPForbidden()
            uid=integer(request.query.get('account')) if op in ('chats','messages','gallery','media','thumb','avatar','account-profile','account-avatar') else None
            if op=='me' and request.method=='GET':return web.json_response({'actor':str(actor),'csrf':session['csrf']},headers=HEADERS)
            if op=='logout' and post:
                response=web.json_response({'ok':True},headers=HEADERS);response.del_cookie('descargas_panel',path=request.path.split('/api/')[0]);return response
            if op=='revoke' and post:
                await self.s.repo.set_setting(f'web-panel-version:{self.s.bot_id}:{actor}',secrets.token_hex(16));return web.json_response({'ok':True},headers=HEADERS)
            if request.method!='GET':raise web.HTTPNotFound()
            if op=='accounts':result=await self.accounts(actor,integer(request.query.get('after','0'),0))
            elif op=='account-profile':
                profile=await self.account_profile(actor,uid);result={k:profile[k] for k in ('name','username')};result['avatar']=bool(profile['file'])
            elif op=='account-avatar':return await self.account_avatar(actor,uid)
            elif op=='chats':result=await self.chats(actor,uid,request.query.get('cursor',''))
            elif op in ('messages','gallery'):result=await self.messages(actor,uid,request.query.get('peer',''),integer(request.query.get('before','0'),0),op=='gallery',request.query.get('q',''))
            elif op in ('media','thumb','avatar'):return await self.media(request,actor,uid,request.query.get('peer',''),integer(request.query.get('message','1')),op=='thumb',op=='avatar')
            else:raise web.HTTPNotFound()
            if not authorized(actor):raise web.HTTPForbidden()
            return web.json_response(result,headers=HEADERS)
        except errors.FloodWaitError as exc:return web.json_response({'error':f'Telegram pide esperar {exc.seconds} segundos.','retry':exc.seconds},status=429,headers=HEADERS)
        except ValueError as exc:return web.json_response({'error':str(exc)},status=409,headers=HEADERS)
        except web.HTTPException as exc:return web.json_response({'error':{401:'Abre tu panel desde /menu en Telegram.',403:'Acceso no autorizado.',404:'Opción no disponible.',429:'Espera un momento antes de repetir.',416:'Rango no disponible.'}.get(exc.status,'Solicitud no válida.')},status=exc.status,headers=HEADERS)
        except TimeoutError:return web.json_response({'error':'La consulta tardó demasiado. Puedes reintentar.'},status=504,headers=HEADERS)
        except Exception:return web.json_response({'error':'No se pudo completar la consulta. Reintenta en un momento.'},status=503,headers=HEADERS)

def mount_panel(app,service):
    panel=TelegramPanel(service)
    async def index(request):
        base=request.path.rstrip('/')
        return web.Response(text=(ROOT/'index.html').read_text().replace('__PANEL_BASE__',html.escape(base,quote=True)),content_type='text/html',headers=HEADERS)
    async def asset(request):
        filename=request.match_info['name']
        if filename not in ('app.js','style.css','wallpaper.svg'):raise web.HTTPNotFound()
        return web.FileResponse(ROOT/filename,headers=HEADERS)
    prefixes={'/panel',urlsplit(service.url).path.rstrip('/')+'/panel'}
    for prefix in prefixes:
        app.router.add_get(prefix,index);app.router.add_get(prefix+'/',index)
        app.router.add_get(prefix+'/assets/{name}',asset);app.router.add_route('*',prefix+'/api/{op}',panel.handle)
    return panel
