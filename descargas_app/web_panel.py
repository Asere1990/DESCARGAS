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
from telethon import types,utils,errors,functions
from .moderation import authorized
from .creator_access import ACTOR,require_account,require_source,reviewer,police
from .account_chat import connected, connected_parallel
from .paid_media import purchased_items, reveal_purchased
from .protected import require_entity
from .panel_features import extra_message

ROOT=Path(__file__).with_name('panel_web')
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin','X-Frame-Options':'DENY',
 'Content-Security-Policy':"default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data: blob:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}

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


def contact_vcard_content(uid,display_name,phone,username=''):
    """Tarjeta vCard: representación de metadatos existentes, sin conectar Telegram."""
    number=re.sub(r'[^0-9+]', '', str(phone or ''))
    if not re.fullmatch(r'\+?[0-9]{6,15}',number):
        raise ValueError('Esta cuenta no tiene un número telefónico válido.')
    def vescape(value):
        return (str(value or '').replace('\\','\\\\')
                .replace('\r\n','\n').replace('\r','\n')
                .replace('\n','\\n').replace(';','\\;').replace(',','\\,'))
    display=str(display_name or '').strip() or 'Cuenta Telegram'
    handle=str(username or '').strip().lstrip('@')
    note=f'Telegram ID: {uid}' + (f'\nTelegram: @{handle}' if handle else '')
    return ('\r\n'.join([
        'BEGIN:VCARD','VERSION:3.0','N:;'+vescape(display)+';;;',
        'FN:'+vescape(display),'TEL;TYPE=CELL:'+number,
        'NOTE:'+vescape(note),'END:VCARD',''
    ])).encode('utf-8')

def peer_data(entity,uid):
    if isinstance(entity,types.User):return {'kind':'self' if entity.id==uid else 'user','id':entity.id,'hash':entity.access_hash or 0,'is_bot':bool(getattr(entity,'bot',False)),'username':getattr(entity,'username',None) or ''}
    if isinstance(entity,types.Channel):return {'kind':'channel','id':entity.id,'hash':entity.access_hash or 0}
    if isinstance(entity,types.Chat):return {'kind':'chat','id':entity.id,'hash':0}
    raise ValueError('Conversación no disponible.')

def input_peer(data):
    return {'self':lambda:types.InputPeerSelf(),'user':lambda:types.InputPeerUser(data['id'],data['hash']),
      'channel':lambda:types.InputPeerChannel(data['id'],data['hash']),'chat':lambda:types.InputPeerChat(data['id'])}[data['kind']]()

def source_id(data):return data['id'] if data['kind'] in ('user','self') else -(1000000000000+data['id']) if data['kind']=='channel' else -data['id']

def message_expiry(message):
    period=getattr(message,'ttl_period',None)
    return message.date.timestamp()+period if period and message.date else None

def media_info(message):
    media=getattr(message,'media',None)
    expires=message_expiry(message)
    if getattr(media,'ttl_seconds',None) or (expires is not None and expires<=time.time()):return None
    if getattr(message,'photo',None):
        sizes=getattr(message.photo,'sizes',[]) or []
        size=max([getattr(x,'size',0) or max(getattr(x,'sizes',[]) or [0]) for x in sizes]+[0])
        return {'type':'photo','mime':'image/jpeg','size':size,'name':'foto.jpg','duration':0,'width':max([getattr(x,'w',0) for x in sizes]+[0]),'height':max([getattr(x,'h',0) for x in sizes]+[0])}
    doc=getattr(message,'document',None)
    if not doc:return None
    attrs=doc.attributes or [];filename=next((a.file_name for a in attrs if isinstance(a,types.DocumentAttributeFilename)),'archivo')
    video=next((a for a in attrs if isinstance(a,types.DocumentAttributeVideo)),None)
    audio=next((a for a in attrs if isinstance(a,types.DocumentAttributeAudio)),None)
    sticker=any(isinstance(a,types.DocumentAttributeSticker) for a in attrs)
    kind='video' if video else 'voice' if audio and audio.voice else 'audio' if audio else 'sticker' if sticker else 'document'
    return {'type':kind,'mime':doc.mime_type or 'application/octet-stream','size':doc.size,'name':filename[:150],
      'duration':getattr(video or audio,'duration',0) or 0,'width':getattr(video,'w',0) or 0,'height':getattr(video,'h',0) or 0}

def message_data(m,protected=False):
    expires=message_expiry(m)
    if getattr(getattr(m,'media',None),'ttl_seconds',None) or (expires is not None and expires<=time.time()):return None
    paid=isinstance(getattr(m,'media',None),types.MessageMediaPaidMedia)
    pieces=[]
    if paid:
        try:owned=purchased_items(m)
        except ValueError:owned=[]
        for item in owned:
            detail=media_info(item)
            if detail and detail['type'] in ('photo','video'):pieces.append(detail)
    info=pieces[0] if paid and pieces else None if paid else media_info(m)
    sender=getattr(m,'sender',None)
    return {'id':m.id,'date':m.date.timestamp() if m.date else 0,'out':bool(m.out),'text':str(getattr(m,'message','') or '')[:16000],
      'sender':name(sender) if sender else '', 'sender_id':getattr(m,'sender_id',None),
      'reply':getattr(getattr(m,'reply_to',None),'reply_to_msg_id',None),'grouped':str(m.grouped_id) if getattr(m,'grouped_id',None) else None,
      'media':info,'paid_media':pieces if paid else None,**({'expires_at':expires} if expires is not None else {}),
      'paid_stars':getattr(m.media,'stars_amount',0) if paid else None,
      'protected':bool(protected or getattr(m,'noforwards',False)),
      'reply_external':bool(getattr(getattr(m,'reply_to',None),'reply_to_peer_id',None)),**extra_message(m)}

async def access_link(s,actor):
    if not authorized(actor):raise ValueError('Acceso no autorizado.')
    version=await s.repo.setting(f'web-panel-version:{s.bot_id}:{actor}') or '0'
    code=s.cipher.encrypt(json.dumps({'purpose':'access','actor':actor,'bot':s.bot_id,'version':version}).encode()).decode()
    return s.url.rstrip('/')+'/panel#'+code

class TelegramPanel:
    def __init__(self,service):
        self.s=service;self.rates={};self.slots=asyncio.Semaphore(3);self.transfers=asyncio.Semaphore(2);self.paid_transfers=asyncio.Semaphore(1);self.profiles={};self.profile_slots=asyncio.Semaphore(2);self._paid_checked={}
        from .web_actions import PanelActions
        self.actions=PanelActions(self)
        from .panel_features import PanelFeatures
        self.features=PanelFeatures(self)
        from .panel_live import LiveHub
        self.live=LiveHub(self)
        from .panel_emoji import PremiumEmoji
        self.emoji=PremiumEmoji(self)
        from .panel_accounts import AccountOverview
        self.overview=AccountOverview(self)
        from .panel_background_search import BackgroundPanelSearch
        self.background_search=BackgroundPanelSearch(self)
        from .panel_favorites import PanelFavorites
        self.favorites=PanelFavorites(self)
        from .panel_reports import TelegramReports
        self.reports=TelegramReports(self)
        from .panel_prepared import PreparedDownloads
        self.prepared=PreparedDownloads(self)
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
            async with self.slots,asyncio.timeout(timeout):
                async with self.live.borrow(actor,uid) as live:
                    if live:
                        yield live
                    else:
                        entered=False
                        try:
                            async with connected(self.s,uid) as client:
                                entered=True
                                client.flood_sleep_threshold=0
                                row=await self.s.repo.account(uid)
                                yield client,row['generation']
                        except ValueError as exc:
                            # Downloads keep their existing exclusive lock. A control-panel
                            # request may use a separate verified session only when entry
                            # failed because that lock is busy; never replay an action.
                            if entered or str(exc) != 'Espera a que termine la transferencia en curso y vuelve a intentarlo.':
                                raise
                            async with connected_parallel(self.s,uid) as client:
                                client.flood_sleep_threshold=0
                                row=await self.s.repo.account(uid)
                                yield client,row['generation']
                if not authorized(actor):raise web.HTTPForbidden()
                await require_account(self.s,uid,actor)
        finally:ACTOR.reset(token)
    @asynccontextmanager
    async def paid_stream_account(self, actor, uid):
        """Independent read-only Telegram connection for large paid downloads.

        This does not acquire the linked account's transfer lock, nor occupy
        interactive panel slots. The saved session and workers remain unchanged.
        """
        if not authorized(actor):
            raise web.HTTPForbidden()
        await require_account(self.s, uid, actor)
        marker = ACTOR.set(actor)
        try:
            async with asyncio.timeout(90 * 60):
                async with connected_parallel(self.s, uid) as client:
                    client.flood_sleep_threshold = 0
                    row = await self.s.repo.account(uid)
                    if not row or not row['session']:
                        raise ValueError('La cuenta vinculada ya no está disponible.')
                    yield client, row['generation']
        finally:
            ACTOR.reset(marker)

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
        limited=reviewer(actor);page_size=5 if limited else 60
        # Read all status snapshots for this page in the same database round trip.
        # Account sessions remain encrypted and are never opened by this listing.
        rows=await self.s.repo.pool.fetch(
            """SELECT a.uid,a.profile,a.generation,md5(a.session) AS session_tag,
                      d.name,d.country,st.value AS status_cache
               FROM descarga_accounts a
               LEFT JOIN descarga_directory d ON d.uid=a.uid AND d.bot_id=$2
               LEFT JOIN descarga_settings st
                 ON st.key='panel-account-status:' || $2::text || ':' || a.uid::text
               WHERE a.session IS NOT NULL AND a.uid>$1
               ORDER BY a.uid LIMIT $3""",after,self.s.bot_id,page_size+1)
        items=[]
        for row in rows[:page_size]:
            try:await require_account(self.s,row['uid'],actor)
            except ValueError:continue
            profile=self.s.repo.decode(row['profile']) if row['profile'] else {}
            directory=self.s.repo.decode(row['name']) if row.get('name') else {}
            from .panel_accounts import metadata
            status=self.overview.cached_value(row.get('status_cache'),row.get('generation',0),row.get('session_tag',''))
            if limited:
                if not status:
                    try:
                        async with asyncio.timeout(4):status=await self.overview.check(actor,row['uid'])
                    except (ValueError,TimeoutError,errors.RPCError,OSError):continue
                if not status or status.get('state')!='active':continue
            items.append({'id':str(row['uid']),'generation':row.get('generation',0),'name':account_label(profile,directory),'username':profile.get('username') or '',
                          **metadata(profile,directory,row.get('country','')), 'status':status or {'state':'unknown','reason':'Sin verificar','checked':0}})
        return {'items':items,'next':str(rows[page_size-1]['uid']) if len(rows)>page_size else None,'reviewer':limited}
    async def account_name_search(self,actor,query,after=0):
        """Fast prefix search of SAVED account names, without a Telegram connection.

        Both the account profile and directory aliases are encrypted at rest.
        Decrypt them in memory; do not return, inspect, or lock a saved session.
        This is intentionally independent of active/revoked status for admins.
        """
        from .panel_background_search import fold
        if not authorized(actor):raise web.HTTPForbidden()
        if not isinstance(query,str) or len(query)>100:raise ValueError('Escribe hasta 100 caracteres.')
        prefix=fold(query)
        if not prefix:return {'items':[],'next':None,'total':0}
        if not 0<=after<=100000:raise ValueError('Página de nombres no válida.')
        limited=reviewer(actor)
        # Exactly one read-only SQL query, regardless of how many linked accounts
        # exist. Neither account_lock(), connected(), client() nor get_me() runs.
        rows=await self.s.repo.pool.fetch(
            """SELECT a.uid,a.profile,a.generation,md5(a.session) AS session_tag,
                      d.name,d.country,st.value AS status_cache
               FROM descarga_accounts a
               LEFT JOIN descarga_directory d ON d.uid=a.uid AND d.bot_id=$1
               LEFT JOIN descarga_settings st
                 ON st.key='panel-account-status:' || $1::text || ':' || a.uid::text
               WHERE a.session IS NOT NULL
               ORDER BY a.uid""",self.s.bot_id)
        matches=[]
        for row in rows:
            profile=self.s.repo.decode(row['profile']) if row['profile'] else {}
            directory=self.s.repo.decode(row['name']) if row.get('name') else {}
            if not isinstance(profile,dict):profile={}
            if not isinstance(directory,dict):directory={}
            saved=account_label(profile,directory)
            # Older saved profiles and the newer directory may hold different
            # names. Search all SAVED name variants, but never captions/chats.
            alternatives=[saved,profile.get('name'),
                          ' '.join(filter(None,(profile.get('first_name'),profile.get('last_name')))),
                          directory.get('name')]
            found=next((v for v in alternatives
                        if isinstance(v,str) and fold(v).startswith(prefix)),None)
            if not found:continue
            status=self.overview.cached_value(row.get('status_cache'),
                                               row.get('generation',0),
                                               row.get('session_tag',''))
            # A reviewer gets the same confirmed-active privacy boundary,
            # without forcing a Telegram identity check.
            if limited and (not status or status.get('state')!='active'):continue
            try:await require_account(self.s,row['uid'],actor)
            except ValueError:continue
            display=saved if fold(saved).startswith(prefix) else ' '.join(found.split())[:160]
            matches.append({'id':str(row['uid']),'name':display,
                            'username':profile.get('username') or '',
                            'generation':row.get('generation',0),
                            'status':status or {'state':'unknown','reason':'Sin verificar','checked':0}})
        matches.sort(key=lambda item:(fold(item['name']),int(item['id'])))
        return {'items':matches[after:after+60],
                'next':after+60 if len(matches)>after+60 else None,
                'total':len(matches)}

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
                items.append({'id':str(d.id),'handle':handle,'name':'Mensajes guardados' if d.id==uid else name(entity),'username':getattr(entity,'username',None) or '',
                 'type':'private' if isinstance(entity,types.User) else 'channel' if getattr(entity,'broadcast',False) else 'group',
                 'date':d.date.timestamp() if d.date else 0,'unread':d.unread_count,'pinned':bool(d.pinned),
                 'preview':last['text'] or (last['media']['type'] if last['media'] else '') if last else '',
                 'members':getattr(entity,'participants_count',None),'owner':bool(getattr(entity,'creator',False)),'admin':bool(getattr(entity,'admin_rights',None)) and not bool(getattr(entity,'creator',False)),'saved':d.id==uid,
                 'avatar':bool(getattr(entity,'photo',None)),'forum':bool(getattr(entity,'forum',False)),'contact':bool(getattr(entity,'contact',False)),'bot':bool(getattr(entity,'bot',False)),'archived':bool(getattr(d,'archived',False)),'muted':bool(getattr(getattr(getattr(d,'dialog',None),'notify_settings',None),'mute_until',None))})
            next_cursor=None
            if len(rows)>60:
                d=rows[59];next_cursor=self.pack('dialogs',actor=actor,uid=uid,generation=generation,offset=d.message.id if d.message else 0,date=d.date.timestamp() if d.date else 0,peer=peer_data(d.entity,uid))
            me=await client.get_me()
            return {'items':items,'next':next_cursor,'account':name(me),'generation':generation}
    async def saved_chat(self,actor,uid):
        async with self.account(actor,uid) as (client,generation):
            me=await client.get_me()
            if not me or me.id!=uid:raise ValueError('No se pudo verificar la identidad de esta cuenta.')
            await require_source(self.s,uid,uid,client=client,entity=me,actor=actor)
            return {'id':str(uid),'name':'Mensajes guardados','type':'private','saved':True,'avatar':False,
                    'handle':self.pack('peer',actor=actor,uid=uid,generation=generation,**peer_data(me,uid))}
    async def messages(self,actor,uid,handle,before=0,gallery=False,query='',topic=0):
        async with self.account(actor,uid) as (client,generation):
            entity=await self.target(actor,uid,generation,client,handle)
            kwargs={'limit':61,'offset_id':before}
            if topic:kwargs['reply_to']=topic
            # The server's PhotoVideo filter omits paid-media posts, including
            # purchases already unlocked for this exact account.
            if query:kwargs['search']=query[:100]
            rows=[m async for m in client.iter_messages(entity,**kwargs)]
            await reveal_purchased(client,entity,rows[:60],self._paid_checked,(uid,generation))
            items=[v for m in rows[:60] if (v:=message_data(m,bool(getattr(entity,'noforwards',False))))]
            if gallery:items=[v for v in items if v['media'] and v['media']['type'] in ('photo','video')]
            else:
                await self.features.replies(client,entity,items)
                if not before:await self.features.receipts(client,entity,items)
            try:await require_source(self.s,uid,utils.get_peer_id(entity),client=client,entity=entity,actor=actor)
            except ValueError:raise web.HTTPForbidden() from None
            return {'items':list(reversed(items)),'next':rows[59].id if len(rows)>60 else None,'title':name(entity),'status':self.features.status(entity)}
    async def media(self,request,actor,uid,handle,mid,thumb=False,avatar=False,purchase=None,index=0,paid_index=None,native_download=False):
        # Transfers are streamed and never saved to the download queue, ALMACEN or Dropbox.
        # Paid videos can take much longer than the 5-minute interactive limit.
        # The read-only connection does not touch the existing account locks.
        paid_download = purchase is not None and not thumb and (native_download or request.query.get('download') == '1')
        context = (self.paid_stream_account(actor, uid) if paid_download
                   else self.account(actor, uid, timeout=300))
        transfer_slot = self.paid_transfers if paid_download else self.transfers
        async with transfer_slot, context as (client,generation):
            entity=await self.target(actor,uid,generation,client,handle)
            if avatar:
                data=await client.download_profile_photo(entity,file=bytes,download_big=False)
                if not data:return web.Response(status=204,headers=HEADERS)
                await require_source(self.s,uid,utils.get_peer_id(entity),client=client,entity=entity,actor=actor)
                if not authorized(actor):raise web.HTTPForbidden()
                return web.Response(body=data,content_type='image/jpeg',headers=HEADERS)
            source_expiry=None
            if purchase is not None:
                from .panel_purchases import resolve
                m=await resolve(self,actor,uid,generation,client,entity,purchase,index)
            else:
                m=await client.get_messages(entity,ids=mid)
                if m:source_expiry=message_expiry(m)
                if m and isinstance(getattr(m,'media',None),types.MessageMediaPaidMedia):
                    await reveal_purchased(client,entity,[m],self._paid_checked,(uid,generation))
                    try:owned=purchased_items(m)
                    except ValueError:
                        raise ValueError('Esta cuenta no tiene acceso al contenido completo comprado con Estrellas.') from None
                    if paid_index is None:
                        paid_index=0 if len(owned)==1 else None
                    if paid_index is None or not 0<=paid_index<len(owned):
                        raise ValueError('Selecciona un archivo comprado válido.')
                    m=owned[paid_index]
                elif paid_index is not None:
                    raise ValueError('El mensaje seleccionado no contiene archivos comprados.')
            if not m or (source_expiry is not None and source_expiry<=time.time()):raise ValueError('Archivo no disponible.')
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
            if paid_download and (not size or size > 4 * 1024**3):
                raise ValueError('El video comprado no tiene un tamaño válido de hasta 4 GB.')
            if request.headers.get('Range'):
                match=re.fullmatch(r'bytes=(\d*)-(\d*)',request.headers['Range'])
                if not match or not size or not any(match.groups()):raise web.HTTPRequestRangeNotSatisfiable(headers={'Content-Range':f'bytes */{size}'})
                left,right=match.groups()
                if left:
                    start=int(left);end=min(size-1,int(right) if right else size-1)
                else:
                    suffix=int(right)
                    if not suffix:raise web.HTTPRequestRangeNotSatisfiable(headers={'Content-Range':f'bytes */{size}'})
                    start=max(0,size-suffix);end=size-1
                status=206
                if start>end or start>=size:raise web.HTTPRequestRangeNotSatisfiable(headers={'Content-Range':f'bytes */{size}'})
            safe=bool(re.fullmatch(r'(?:image/(?:jpeg|png|webp|gif)|video/(?:mp4|webm|quicktime|x-m4v)|audio/(?:ogg|mpeg|mp4|aac|wav|webm))',info['mime']))
            # Safari needs the actual extension when Telegram supplies a generic filename.
            filename=re.sub(r'[^a-zA-Z0-9._-]','_',info['name'])[:150] or 'archivo'
            if (native_download or request.query.get('download')=='1') and not re.search(r'\.[a-zA-Z0-9]{2,6}\Z',filename):
                suffix={'video/mp4':'.mp4','video/quicktime':'.mov','video/webm':'.webm',
                    'video/x-m4v':'.m4v','image/jpeg':'.jpg','image/png':'.png',
                    'image/webp':'.webp','image/gif':'.gif'}.get(info['mime'],'')
                filename+=suffix
            headers={**HEADERS,'Content-Type':'application/octet-stream' if paid_download else (info['mime'] if safe else 'application/octet-stream'),'Accept-Ranges':'bytes',
             'Content-Disposition':('inline' if safe and not native_download and request.query.get('download')!='1' else 'attachment')+'; filename="'+filename+'"'}
            if size:headers['Content-Length']=str(end-start+1)
            if status==206:headers['Content-Range']=f'bytes {start}-{end}/{size}'
            if native_download and request.method=='HEAD':
                return web.Response(status=status,headers=headers)
            response=web.StreamResponse(status=status,headers=headers);await response.prepare(request)
            if native_download:
                from .panel_native_stream import write_verified_range
                async def recheck_paid_access():
                    if not authorized(actor):
                        raise web.HTTPForbidden()
                    await require_account(self.s,uid,actor)
                    await require_source(
                        self.s,uid,utils.get_peer_id(entity),client=client,
                        entity=entity,actor=actor)
                return await write_verified_range(
                    response, client, m.media, start, end,
                    check=recheck_paid_access)
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
    async def contact_vcard_response(self,actor,uid):
        """La respuesta HTTP se abre como ficha nativa, nunca como hoja Compartir."""
        if not authorized(actor):
            raise web.HTTPForbidden()
        await require_account(self.s,uid,actor)
        row=await self.s.repo.pool.fetchrow(
            '''SELECT a.profile,d.name FROM descarga_accounts a
               LEFT JOIN descarga_directory d ON d.uid=a.uid AND d.bot_id=$2
               WHERE a.uid=$1 AND a.session IS NOT NULL''',
            uid,self.s.bot_id)
        if not row:
            raise ValueError('Cuenta vinculada no disponible.')
        profile=self.s.repo.decode(row['profile']) if row['profile'] else {}
        directory=self.s.repo.decode(row['name']) if row.get('name') else {}
        from .panel_accounts import metadata
        phone=metadata(profile,directory).get('phone') or profile.get('phone')
        card=contact_vcard_content(
            uid,account_label(profile,directory),phone,profile.get('username',''))
        return web.Response(
            body=card,
            content_type='text/vcard',charset='utf-8',
            headers={**HEADERS,
                'Content-Disposition':f'inline; filename="telegram-{uid}.vcf"',
            })

    async def handle(self,request):
        try:
            if not self.s.ready:return web.json_response({'error':'El servicio está iniciándose.'},status=503,headers=HEADERS)
            op=request.match_info['op'];post=request.method=='POST'
            if op=='prepared-file' and request.method in ('GET','HEAD'):
                return await self.prepared.serve(request)
            if op=='native-stars-file' and request.method in ('GET','HEAD'):
                from .panel_native_paid import serve
                return await serve(self,request)
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
            if post and op=='batch-download':
                data=await request.post()
                if not secrets.compare_digest(data.get('csrf',''),session['csrf']):raise web.HTTPForbidden()
                from .panel_batch import download
                return await download(self,request,actor,data)
            if post and not secrets.compare_digest(request.headers.get('X-Panel-CSRF',''),session['csrf']):raise web.HTTPForbidden()
            uid=integer(request.query.get('account')) if op in ('purchases','purchase-media','purchase-thumb','saved-chat','account-status','contact.vcf','accounts-search','chats','messages','gallery','member-list','gallery-count','chat-counts','media','thumb','avatar','account-profile','account-avatar','permissions','administrators','user-info','group-details','context','global-search','folders','topics','scheduled','library','library-thumb','events','changes','emoji') else None
            if op=='events' and request.method=='GET':return await self.live.stream(request,session,uid,request.query.get('peer',''))
            if op=='me' and request.method=='GET':return web.json_response({'actor':str(actor),'csrf':session['csrf'],'reviewer':reviewer(actor),'admin':police(actor),'zip_limit':10 if reviewer(actor) else 500},headers=HEADERS)
            if op=='logout' and post:
                response=web.json_response({'ok':True},headers=HEADERS);response.del_cookie('descargas_panel',path=request.path.split('/api/')[0]);return response
            if op=='revoke' and post:
                await self.s.repo.set_setting(f'web-panel-version:{self.s.bot_id}:{actor}',secrets.token_hex(16));return web.json_response({'ok':True},headers=HEADERS)
            if op=='account-name-search' and request.method=='GET':
                result=await self.account_name_search(actor,request.query.get('q',''),
                    integer(request.query.get('after','0'),0))
                return web.json_response(result,headers=HEADERS)
            if op=='favorites' and request.method=='GET':
                return web.json_response(await self.favorites.listing(actor),headers=HEADERS)
            if op=='favorite-accounts' and request.method=='GET':
                return web.json_response(
                    {'accounts':await self.favorites.account_listing(actor)},headers=HEADERS)
            if post and op in ('favorite-toggle','favorite-remove',
                               'favorite-account-toggle'):
                data=await request.json()
                if not isinstance(data,dict):raise ValueError('Favorito no válido.')
                if op=='favorite-toggle':
                    result=await self.favorites.toggle(actor,data)
                elif op=='favorite-account-toggle':
                    result=await self.favorites.toggle_account(actor,data)
                else:
                    result=await self.favorites.remove(actor,
                        integer(data.get('account')),int(data.get('source')))
                return web.json_response(result,headers=HEADERS)
            if post and op=='search-job-clear':
                return web.json_response(
                    await self.background_search.clear(actor,session['version']),
                    headers=HEADERS)
            if post and op in ('search-job-start','search-job-control'):
                data=await request.json()
                if not isinstance(data,dict):raise ValueError('Solicitud de búsqueda no válida.')
                if op=='search-job-start':
                    result=await self.background_search.begin(actor,session['version'],
                        data.get('mode'),data.get('query'))
                else:
                    result=await self.background_search.control(actor,session['version'],
                        str(data.get('id','')),data.get('action'))
                return web.json_response(result,headers=HEADERS)
            if op=='search-job-status' and request.method=='GET':
                return web.json_response(await self.background_search.status(
                    actor,session['version'],request.query.get('id','')),headers=HEADERS)
            if op=='search-job-results' and request.method=='GET':
                return web.json_response(await self.background_search.results(
                    actor,session['version'],request.query.get('id',''),
                    integer(request.query.get('after','0'),0)),headers=HEADERS)
            if post and op in ('report-begin','report-step'):
                data=await request.json()
                if not isinstance(data,dict):raise ValueError('Reporte no válido.')
                result=(await self.reports.begin(actor,data) if op=='report-begin'
                        else await self.reports.next(actor,data))
                return web.json_response(result,headers=HEADERS)
            if post and op=='prepare-start':
                return web.json_response(await self.prepared.begin(
                    actor,session['version'],await request.json()),headers=HEADERS)
            if post and op=='deliver-selection':
                from .panel_delivery import enqueue
                data=await request.json()
                if not isinstance(data,dict):raise ValueError('Solicitud no válida.')
                return web.json_response(await enqueue(self,actor,data),headers=HEADERS)
            if post and op in ('action','upload','resolve'):
                if op=='upload':
                    input,file=await self.actions.read_upload(request)
                    try:return web.json_response(await self.actions.execute(actor,input,file),headers=HEADERS)
                    finally:file['close']()
                input=await request.json()
                if not isinstance(input,dict):raise ValueError('Solicitud no válida.')
                if op=='resolve':return web.json_response(await self.actions.resolve(actor,input),headers=HEADERS)
                if input.get('action') in ('typing','reaction','vote','poll','saved-media','scheduled-send','scheduled-delete'):return web.json_response(await self.features.perform(actor,input),headers=HEADERS)
                return web.json_response(await self.actions.execute(actor,input),headers=HEADERS)
            if request.method!='GET':raise web.HTTPNotFound()
            if op=='accounts':result=await self.accounts(actor,integer(request.query.get('after','0'),0))
            elif op=='prepare-status':
                result=await self.prepared.status(
                    actor,session['version'],request.query.get('job',''))
            elif op=='account-status':result=await self.overview.check(actor,uid)
            elif op=='contact.vcf':return await self.contact_vcard_response(actor,uid)
            elif op=='purchases':
                from .panel_purchases import listing
                result=await listing(self,actor,uid,request.query.get('cursor',''),session['version'])
            elif op in ('purchase-media','purchase-thumb'):
                return await self.media(request,actor,uid,request.query.get('peer',''),1,thumb=op=='purchase-thumb',purchase=request.query.get('purchase',''),index=integer(request.query.get('index','0'),0))
            elif op=='saved-chat':result=await self.saved_chat(actor,uid)
            elif op=='accounts-search':result=await self.features.overview_search(actor,uid,request.query.get('q',''),request.query.get('cursor',''))
            elif op=='account-profile':
                profile=await self.account_profile(actor,uid);result={k:profile[k] for k in ('name','username')};result['avatar']=bool(profile['file'])
            elif op=='account-avatar':return await self.account_avatar(actor,uid)
            elif op in ('permissions','administrators','user-info','group-details'):result=await self.actions.inspect(actor,uid,request.query.get('peer',''),op=='administrators',request.query.get('target') if op=='user-info' else None,op=='group-details')
            elif op=='emoji':
                mime,data=await self.emoji.asset(actor,uid,request.query.get('peer',''),integer(request.query.get('message')),integer(request.query.get('document')));return web.Response(body=data,content_type=mime,headers=HEADERS)
            elif op=='changes':result=await self.features.changes(actor,uid,request.query.get('peer',''),request.query.get('ids',''))
            elif op=='chat-counts':result=await self.features.chat_counts(actor,uid,request.query.get('peer',''))
            elif op=='member-list':result=await self.features.member_list(actor,uid,request.query.get('peer',''),request.query.get('kind','members'),request.query.get('cursor',''))
            elif op=='gallery-count':result=await self.features.gallery_count(actor,uid,request.query.get('peer',''),integer(request.query.get('topic','0'),0))
            elif op=='context':result=await self.features.context(actor,uid,request.query.get('peer',''),integer(request.query.get('message')))
            elif op=='global-search':result=await self.features.search(actor,uid,request.query.get('q',''),request.query.get('cursor',''))
            elif op=='folders':result=await self.features.folders(actor,uid)
            elif op=='topics':result=await self.features.topics(actor,uid,request.query.get('peer',''),request.query.get('cursor',''))
            elif op=='scheduled':result=await self.features.scheduled(actor,uid,request.query.get('peer',''))
            elif op=='library':result=await self.features.library(actor,uid,request.query.get('kind','stickers'))
            elif op=='library-thumb':
                data=await self.features.library_thumb(actor,uid,request.query.get('document',''))
                if not data:return web.Response(status=204,headers=HEADERS)
                with Image.open(io.BytesIO(data)) as picture:
                    picture.thumbnail((256,256));out=io.BytesIO();picture.convert('RGB').save(out,format='JPEG',quality=80)
                return web.Response(body=out.getvalue(),content_type='image/jpeg',headers=HEADERS)
            elif op=='chats':result=await self.chats(actor,uid,request.query.get('cursor',''))
            elif op in ('messages','gallery'):result=await self.messages(actor,uid,request.query.get('peer',''),integer(request.query.get('before','0'),0),op=='gallery',request.query.get('q',''),integer(request.query.get('topic','0'),0))
            elif op in ('media','thumb','avatar'):
                paid_index=integer(request.query['paid_index'],0) if 'paid_index' in request.query else None
                return await self.media(request,actor,uid,request.query.get('peer',''),integer(request.query.get('message','1')),op=='thumb',op=='avatar',paid_index=paid_index)
            else:raise web.HTTPNotFound()
            if not authorized(actor):raise web.HTTPForbidden()
            return web.json_response(result,headers=HEADERS)
        except errors.FloodWaitError as exc:return web.json_response({'error':f'Telegram pide esperar {exc.seconds} segundos.','retry':exc.seconds},status=429,headers=HEADERS)
        except ValueError as exc:return web.json_response({'error':str(exc)},status=409,headers=HEADERS)
        except web.HTTPException as exc:return web.json_response({'error':{401:'Abre tu panel desde /menu en Telegram.',403:'Acceso no autorizado.',404:'Opción no disponible.',429:'Espera un momento antes de repetir.',416:'Rango no disponible.'}.get(exc.status,'Solicitud no válida.')},status=exc.status,headers=HEADERS)
        except errors.RPCError as exc:return web.json_response({'error':'Telegram rechazó la operación: '+type(exc).__name__},status=409,headers=HEADERS)
        except TimeoutError:return web.json_response({'error':'La consulta tardó demasiado. Puedes reintentar.'},status=504,headers=HEADERS)
        except Exception:return web.json_response({'error':'No se pudo completar la consulta. Reintenta en un momento.'},status=503,headers=HEADERS)

def mount_panel(app,service):
    panel=TelegramPanel(service)
    async def stop_live(app):await panel.live.close()
    async def start_prepared(app):await panel.prepared.start()
    async def stop_prepared(app):await panel.prepared.close()
    app.on_startup.append(start_prepared)
    app.on_startup.append(panel.background_search.start)
    app.on_shutdown.append(panel.background_search.stop)
    app.on_shutdown.append(stop_prepared)
    app.on_shutdown.append(stop_live)
    async def index(request):
        base=request.path.rstrip('/')
        return web.Response(text=(ROOT/'index.html').read_text().replace('__PANEL_BASE__',html.escape(base,quote=True)),content_type='text/html',headers=HEADERS)
    async def asset(request):
        filename=request.match_info['name']
        if filename not in ('app.js','style.css','wallpaper.svg','lottie-light.js','lottie-LICENSE.txt'):raise web.HTTPNotFound()
        return web.FileResponse(ROOT/filename,headers=HEADERS)
    prefixes={'/panel',urlsplit(service.url).path.rstrip('/')+'/panel'}
    for prefix in prefixes:
        app.router.add_get(prefix,index);app.router.add_get(prefix+'/',index)
        app.router.add_get(prefix+'/assets/{name}',asset);app.router.add_route('*',prefix+'/api/{op}',panel.handle)
    return panel
