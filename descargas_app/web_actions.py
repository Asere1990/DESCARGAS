"""Explicit account operations for the web panel; Telegram remains the authority."""
import asyncio
import io
import json
import re
import tempfile
import shutil
import time
from telethon import types,functions,errors,utils
from aiohttp import web
from .moderation import authorized
from .creator_access import require_account,require_source
from .account_chat import effective_permissions
from .group_admin import applicable,need,target_user,role,defaults_copy,MEMBER,active_bans

class PanelActions:
    def __init__(self,panel):self.p=panel;self.s=panel.s
    async def permissions(self,client,entity):
        if isinstance(entity,types.User):
            return None,{'role':'Cuenta vinculada','send':{k:True for k in ('text','previews','photos','videos','documents','audio','voices')},'rights':{},'grant':[],'group':False,'channel':False,'basic':False,'defaults':[]}
        member=await client.get_permissions(entity,'me');defaults=await client.get_permissions(entity)
        send=effective_permissions(entity,member,defaults)
        rights={flag:bool(member.is_creator or (member.is_admin and getattr(getattr(member.participant,'admin_rights',None),flag,False))) for flag,label in applicable(entity)}
        if isinstance(entity,types.Chat):
            rights.update({f:bool(member.is_admin) for f in ('change_info','delete_messages','ban_users','invite_users','pin_messages')});rights['add_admins']=bool(member.is_creator)
        if not member.is_admin and not member.has_left and not getattr(entity,'broadcast',False):
            bans=active_bans(member)
            for flag in ('pin_messages','change_info','invite_users'):
                rights[flag]=not (getattr(defaults,flag,False) or getattr(bans,flag,False))
        return member,{'role':role(member),'send':send,'rights':rights,'grant':[{'flag':f,'label':l} for f,l in applicable(entity) if rights[f]],'group':True,'channel':bool(getattr(entity,'broadcast',False)),'basic':isinstance(entity,types.Chat),'defaults':[{'flag':f,'label':l,'allowed':not bool(getattr(defaults_copy(getattr(entity,'default_banned_rights',None)),f,False))} for f,l in MEMBER]}
    def need(self,member,entity,flag):
        if isinstance(entity,types.Chat) and member.is_admin and flag!='add_admins':return
        need(member,flag)
    async def resolve(self,actor,input):
        from .web_panel import peer_data,name
        uid=self.p_integer(input.get('account'));target=self.user_id(input.get('target'))
        async with self.p.account(actor,uid) as (client,generation):
            entity=await client.get_entity(target if isinstance(target,str) else types.PeerUser(target))
            if not isinstance(entity,types.User):raise ValueError('Para iniciar un chat, escribe el @username o ID de un usuario o bot.')
            await require_source(self.s,uid,entity.id,client=client,entity=entity,actor=actor)
            return {'id':str(entity.id),'name':name(entity),'type':'private','handle':self.p.pack('peer',actor=actor,uid=uid,generation=generation,**peer_data(entity,uid)), 'date':0,'unread':0,'preview':'','avatar':bool(getattr(entity,'photo',None))}
    async def inspect(self,actor,uid,handle,administrators=False,target=None,details=False):
        async with self.p.account(actor,uid) as (client,generation):
            entity=await self.p.target(actor,uid,generation,client,handle)
            member,info=await self.permissions(client,entity)
            info.update(name=getattr(entity,'title',None) or getattr(entity,'first_name',''),source=str(utils.get_peer_id(entity)))
            if details and info['group']:
                full=await client(functions.channels.GetFullChannelRequest(entity) if isinstance(entity,types.Channel) else functions.messages.GetFullChatRequest(entity.id))
                info['about']=getattr(full.full_chat,'about','') or ''
            if administrators:
                if not info['group']:raise ValueError('Esta conversación no es un grupo o canal.')
                users=[]
                kwargs={}
                if isinstance(entity,types.Channel):kwargs['filter']=types.ChannelParticipantsAdmins()
                async for user in client.iter_participants(entity,**kwargs):
                    participant=getattr(user,'participant',None)
                    if not isinstance(entity,types.Channel) and not isinstance(participant,(types.ChatParticipantCreator,types.ChatParticipantAdmin)):continue
                    users.append({'id':str(user.id),'name':' '.join(filter(None,[user.first_name,user.last_name])) or str(user.id),'username':getattr(user,'username',None) or '', 'rank':getattr(participant,'rank','') or '', 'owner':isinstance(participant,(types.ChannelParticipantCreator,types.ChatParticipantCreator)),'rights':[f for f,l in applicable(entity) if getattr(getattr(participant,'admin_rights',None),f,False)]})
                info['administrators']=users
            if target:
                if not info['group']:raise ValueError('Selecciona un grupo o canal.')
                user=await target_user(self.s,client,entity,self.user_id(target))
                try:permissions=await client.get_permissions(entity,user)
                except errors.UserNotParticipantError:permissions=None
                info['user']={'id':str(user.id),'name':' '.join(filter(None,[user.first_name,user.last_name])) or str(user.id),'username':user.username or '', 'role':role(permissions) if permissions else 'No pertenece al grupo','rank':getattr(getattr(permissions,'participant',None),'rank','') or '','rights':[f for f,l in applicable(entity) if permissions and (permissions.is_creator or getattr(getattr(permissions.participant,'admin_rights',None),f,False))]}
            if not authorized(actor):raise web.HTTPForbidden()
            await require_source(self.s,uid,utils.get_peer_id(entity),client=client,entity=entity,actor=actor)
            return info
    def user_id(self,value):
        value=str(value or '').strip()
        if re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{3,31}',value):return value
        return self.p_integer(value)
    def p_integer(self,value):
        from .web_panel import integer
        return integer(value)
    async def reserve(self,actor,uid,source,input):
        request=input.get('request','')
        if not isinstance(request,str) or not re.fullmatch(r'[a-f0-9]{32}',request):raise ValueError('No se pudo identificar la operación. Vuelve a abrir el panel.')
        key=f'web-action:{self.s.bot_id}:{actor}:{request}'
        row=await self.s.repo.pool.fetchval('INSERT INTO descarga_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO NOTHING RETURNING key',key,json.dumps({'state':'sending','account':uid,'source':source,'action':input['action'],'created':time.time()}))
        if not row:raise ValueError('Esta operación ya se procesó o requiere revisión. Comprueba Telegram antes de repetirla.')
        return key
    async def known_message(self,client,entity,value):
        mid=self.p_integer(value);message=await client.get_messages(entity,ids=mid)
        if not message or getattr(message,'peer_id',None) is None or utils.get_peer_id(message.peer_id)!=utils.get_peer_id(entity):raise ValueError('El mensaje no pertenece a este chat o ya no está disponible.')
        return message
    async def execute(self,actor,input,file=None):
        uid=self.p_integer(input.get('account'));handle=input.get('peer','');action=input.get('action','')
        if action not in ('send','upload','edit','delete','pin','unpin','read','promote','demote','ban','unban','restrict','unrestrict','invite','group-info','defaults','forward'):raise ValueError('Acción no disponible.')
        async with self.p.account(actor,uid,timeout=None if file else 60) as (client,generation):
            entity=await self.p.target(actor,uid,generation,client,handle);source=utils.get_peer_id(entity)
            member,info=await self.permissions(client,entity)
            if action=='upload' and not file:raise ValueError('Adjunta un archivo.')
            if file and action!='upload':raise ValueError('Tipo de envío no válido.')
            text=input.get('text','')
            if not isinstance(text,str):raise ValueError('Texto no válido.')
            reply=None
            if input.get('reply') is not None:reply=(await self.known_message(client,entity,input['reply'])).id
            message=None;target=None;participant=None;destination=None
            if action in ('send','upload'):
                if not text.strip() and not file:raise ValueError('Escribe un mensaje o adjunta un archivo.')
                permission='text' if not file else {'photo':'photos','video':'videos','audio':'audio','voice':'voices','document':'documents'}[file['kind']]
                if not info['send'].get(permission):raise ValueError('Telegram no permite ese envío con esta cuenta en este chat.')
            elif action in ('edit','delete','pin','unpin','forward'):
                message=await self.known_message(client,entity,input.get('message'))
                if action=='forward':
                    if getattr(entity,'noforwards',False) or getattr(message,'noforwards',False) or getattr(message,'ttl_period',None) or getattr(getattr(message,'media',None),'ttl_seconds',None):raise ValueError('Telegram protege este mensaje contra reenvíos.')
                    destination=await self.p.target(actor,uid,generation,client,input.get('destination',''))
                    _,destination_info=await self.permissions(client,destination)
                    from .web_panel import media_info
                    media=media_info(message)
                    flag={'photo':'photos','video':'videos','audio':'audio','voice':'voices'}.get(media['type'],'documents') if media else 'text'
                    if not destination_info['send'].get(flag):raise ValueError('La cuenta no puede publicar ese contenido en el destino.')
                if action=='edit':
                    if not message.out and not info['rights'].get('edit_messages'):raise ValueError('La cuenta no puede editar ese mensaje.')
                    if not text.strip():raise ValueError('Escribe el texto nuevo.')
                if action=='delete' and not message.out and not isinstance(entity,types.User) and not info['rights'].get('delete_messages'):raise ValueError('La cuenta no puede eliminar ese mensaje.')
                if action in ('pin','unpin') and info['group'] and not info['rights'].get('pin_messages'):raise ValueError('La cuenta no tiene permiso para fijar mensajes.')
            elif action!='read':
                if not info['group']:raise ValueError('Esta acción requiere un grupo o canal.')
                if action=='group-info':
                    if not info['rights'].get('change_info'):raise ValueError('La cuenta no tiene permiso para editar la información de este chat.')
                    if not isinstance(input.get('title'),str) or not input['title'].strip() or len(input['title'])>128 or not isinstance(input.get('about',''),str) or len(input.get('about',''))>255:raise ValueError('Escribe un título y una descripción válidos.')
                elif action=='defaults':
                    self.need(member,entity,'ban_users')
                    if info['channel']:raise ValueError('Los canales solo permiten publicar a sus administradores.')
                    if not isinstance(input.get('allowed'),list) or not set(input['allowed'])<={f for f,l in MEMBER}:raise ValueError('Permisos no válidos.')
                else:
                    target=await target_user(self.s,client,entity,self.user_id(input.get('target')))
                    try:participant=await client.get_permissions(entity,target)
                    except errors.UserNotParticipantError:participant=None
                    if participant and participant.is_creator:raise ValueError('Telegram no permite cambiar al propietario con esta operación.')
                    if action in ('promote','demote'):
                        self.need(member,entity,'add_admins')
                        if participant and participant.is_admin and not member.is_creator and not getattr(participant.participant,'can_edit',False):raise ValueError('Telegram no permite a esta cuenta modificar a ese administrador.')
                        if action=='promote':
                            selected=input.get('rights',[])
                            if not isinstance(selected,list) or not selected or not set(selected)<={r['flag'] for r in info['grant']}:raise ValueError('Solo puedes otorgar permisos que tenga la cuenta vinculada.')
                            if isinstance(entity,types.Chat) and set(selected)!={r['flag'] for r in info['grant']}:raise ValueError('Telegram concede los permisos conjuntos al administrador de un grupo básico.')
                            if not isinstance(input.get('rank',''),str) or len(input.get('rank',''))>16:raise ValueError('La etiqueta puede tener hasta 16 caracteres.')
                    elif action=='invite':
                        if not info['rights'].get('invite_users'):
                            defaults=getattr(entity,'default_banned_rights',None);individual=getattr(getattr(member,'participant',None),'banned_rights',None)
                            if info['channel'] or member.has_left or getattr(defaults,'invite_users',False) or getattr(individual,'invite_users',False):raise ValueError('La cuenta no tiene permiso para invitar a ese usuario.')
                    else:
                        if isinstance(entity,types.Chat) and action in ('restrict','unrestrict','unban'):raise ValueError('Telegram solo permite esta acción detallada en supergrupos y canales.')
                        self.need(member,entity,'ban_users')
                        if participant and participant.is_admin:raise ValueError('Retira primero el cargo de administrador de ese usuario.')
            if not authorized(actor):raise web.HTTPForbidden()
            await require_account(self.s,uid,actor);await require_source(self.s,uid,source,client=client,entity=entity,actor=actor)
            receipt=await self.reserve(actor,uid,source,input)
            try:
                result={'ok':True,'action':action}
                if action=='send':
                    sent=await client.send_message(entity,text,parse_mode=None,reply_to=reply,link_preview=info['send'].get('previews',False));result['message']=sent.id
                elif action=='upload':
                    if not file:raise ValueError('Adjunta el archivo.')
                    sent=await client.send_file(entity,file['stream'],caption=text,parse_mode=None,reply_to=reply,force_document=file['kind']=='document',voice_note=file['kind']=='voice',supports_streaming=file['kind']=='video',mime_type=file['mime']);result['message']=sent.id
                elif action=='forward':
                    sent=await client.forward_messages(destination,message.id,from_peer=entity);result['message']=sent.id
                elif action=='edit':await client.edit_message(entity,message.id,text,parse_mode=None,link_preview=info['send'].get('previews',False))
                elif action=='delete':await client.delete_messages(entity,[message.id],revoke=True)
                elif action in ('pin','unpin'):
                    await (client.pin_message(entity,message.id,notify=False) if action=='pin' else client.unpin_message(entity,message.id))
                elif action=='read':await client.send_read_acknowledge(entity,max_id=self.p_integer(input['message']) if input.get('message') else 0)
                elif action in ('promote','demote'):
                    if isinstance(entity,types.Channel):
                        rights=types.ChatAdminRights(**{r:True for r in input['rights']}) if action=='promote' else types.ChatAdminRights()
                        await client(functions.channels.EditAdminRequest(entity,target,rights,input.get('rank','') if action=='promote' else ''))
                    else:await client(functions.messages.EditChatAdminRequest(entity.id,target,action=='promote'))
                elif action in ('ban','unban','restrict','unrestrict'):
                    if isinstance(entity,types.Channel):
                        rights=types.ChatBannedRights(until_date=0,**({'view_messages':True} if action=='ban' else {'send_messages':True} if action=='restrict' else {}))
                        await client(functions.channels.EditBannedRequest(entity,target,rights))
                    elif action=='ban':await client(functions.messages.DeleteChatUserRequest(entity.id,target))
                    else:raise ValueError('Telegram solo permite estas restricciones detalladas en supergrupos y canales.')
                elif action=='invite':
                    if isinstance(entity,types.Channel):await client(functions.channels.InviteToChannelRequest(entity,[target]))
                    else:await client(functions.messages.AddChatUserRequest(entity.id,target,0))
                elif action=='group-info':
                    if isinstance(entity,types.Channel):await client(functions.channels.EditTitleRequest(entity,input['title'].strip()))
                    else:await client(functions.messages.EditChatTitleRequest(entity.id,input['title'].strip()))
                    await client(functions.messages.EditChatAboutRequest(entity,input.get('about','')))
                elif action=='defaults':
                    rights=defaults_copy(getattr(entity,'default_banned_rights',None))
                    for flag,label in MEMBER:setattr(rights,flag,flag not in input['allowed'])
                    await client(functions.messages.EditChatDefaultBannedRightsRequest(entity,rights))
                await self.s.repo.set_setting(receipt,json.dumps({'state':'done',**result}))
                return result
            except (Exception,asyncio.CancelledError) as exc:
                state='rejected' if isinstance(exc,(errors.RPCError,ValueError)) else 'review'
                await self.s.repo.set_setting(receipt,json.dumps({'state':state,'action':action}))
                if isinstance(exc,asyncio.CancelledError):raise
                if isinstance(exc,errors.RPCError):raise ValueError('Telegram rechazó la operación: '+type(exc).__name__+'. Revisa el estado antes de repetirla.') from None
                if isinstance(exc,ValueError):raise
                raise ValueError('No se pudo confirmar la operación. Revisa Telegram antes de repetirla; no se reintentará automáticamente.') from None
    async def read_upload(self,request):
        # Per-request streaming tempfile: no file is retained after the explicit send.
        reader=await request.multipart();data=None;stream=None;name='';mime='application/octet-stream';total=0
        try:
            while part:=await reader.next():
                if part.name=='data':
                    raw=await part.read(decode=False)
                    if len(raw)>20000:raise ValueError('Formulario demasiado grande.')
                    data=json.loads(raw)
                elif part.name=='file':
                    if stream is not None:raise ValueError('Envía un archivo por operación.')
                    name=re.sub(r'[^\w .()-]','_',part.filename or 'archivo')[:120]
                    stream=tempfile.TemporaryFile();mime=part.headers.get('Content-Type','application/octet-stream').split(';')[0]
                    while chunk:=await part.read_chunk(262144):
                        total+=len(chunk)
                        if shutil.disk_usage(tempfile.gettempdir()).free<len(chunk)+64*1024*1024:raise ValueError('No queda espacio temporal para completar este archivo. Inténtalo más tarde.')
                        stream.write(chunk)
            if not isinstance(data,dict) or stream is None or not total:raise ValueError('Adjunta un archivo válido.')
            kind=data.get('kind','document')
            if kind not in ('photo','video','audio','voice','document'):raise ValueError('Tipo de archivo no válido.')
            stream.seek(0)
            # Telethon needs a filename to preserve the extension when inferring media.
            class NamedFile:
                def __init__(self,f):self.f=f;self.name=name
                def read(self,*args):return self.f.read(*args)
                def seek(self,*args):return self.f.seek(*args)
                def tell(self):return self.f.tell()
                def seekable(self):return True
            return data,{'stream':NamedFile(stream),'mime':mime,'kind':kind,'size':total,'close':stream.close}
        except BaseException:
            if stream is not None:stream.close()
            raise
