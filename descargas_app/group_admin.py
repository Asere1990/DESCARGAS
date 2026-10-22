"""Private, generation-bound Telegram management; independent of download jobs."""
import asyncio
import copy
import json
import re
import time
import uuid
from datetime import datetime,timezone
from telethon import errors, functions, types, utils
from .moderation import authorized
from .account_chat import connected

ADMIN=(('change_info','Editar información'),('post_messages','Publicar mensajes'),
       ('edit_messages','Editar mensajes'),('delete_messages','Eliminar mensajes'),
       ('ban_users','Expulsar y restringir usuarios'),('invite_users','Invitar usuarios'),
       ('pin_messages','Fijar mensajes'),('manage_call','Administrar llamadas'),
       ('manage_topics','Administrar temas'),('post_stories','Publicar historias'),
       ('edit_stories','Editar historias'),('delete_stories','Eliminar historias'),
       ('anonymous','Permanecer anónimo'),('manage_direct_messages','Administrar mensajes directos'),
       ('manage_ranks','Administrar etiquetas'),('manage_welcome_messages','Administrar bienvenidas'),
       ('add_admins','Añadir administradores'))
MEMBER=(('send_plain','Enviar texto'),('send_photos','Enviar fotos'),('send_videos','Enviar videos'),
        ('send_docs','Enviar archivos'),('send_audios','Enviar audio'),('send_voices','Enviar notas de voz'),
        ('send_roundvideos','Enviar videomensajes'),('send_stickers','Enviar stickers'),('send_gifs','Enviar GIF'),
        ('send_polls','Enviar encuestas'),('embed_links','Vista previa de enlaces'),
        ('invite_users','Invitar usuarios'),('pin_messages','Fijar mensajes'),('change_info','Editar información'),
        ('edit_rank','Editar su etiqueta'),('send_reactions','Enviar reacciones'))
MEDIA=('send_photos','send_videos','send_docs','send_audios','send_voices','send_roundvideos','send_stickers','send_gifs','send_games','send_inline')


def key(s,actor):return f'group-admin:{s.bot_id}:{actor}'
def pending_key(s,actor):return key(s,actor)+':input'

def parse_numbers(text,size):
    if not re.fullmatch(r'\s*[1-9][0-9]*(?:\s*,\s*[1-9][0-9]*)*\s*',text):
        raise ValueError('Escribe números separados por comas, por ejemplo: 1,3,4.')
    values={int(v) for v in text.split(',')}
    if any(v>size for v in values):raise ValueError('Hay un número que no aparece en esta lista.')
    return sorted(values)


def applicable(entity):
    broadcast=bool(getattr(entity,'broadcast',False))
    return [(flag,label) for flag,label in ADMIN
            if (flag not in ('post_messages','edit_messages','manage_direct_messages') or broadcast)
            and (flag not in ('anonymous','manage_ranks','manage_welcome_messages') or not broadcast)
            and (flag!='manage_topics' or getattr(entity,'forum',False))]


def role(member):
    return 'Propietario' if member.is_creator else 'Administrador' if member.is_admin else 'Miembro regular'


def rights_snapshot(entity,member):
    rights=getattr(member.participant,'admin_rights',None)
    return {'role':role(member),'checked':int(time.time()),'rights':{
        label:bool(member.is_creator or (member.is_admin and getattr(rights,flag,False)))
        for flag,label in applicable(entity)}}


def entity_snapshot(entity):
    from types import SimpleNamespace
    rights=getattr(entity,'admin_rights',None)
    return rights_snapshot(entity,SimpleNamespace(is_creator=bool(getattr(entity,'creator',False)),
        is_admin=bool(rights),participant=SimpleNamespace(admin_rights=rights)))


def header(snapshot):
    if not snapshot:return 'Permisos: aún no consultados. Usa GRUPO INFO.\n'
    granted=[label for label,value in snapshot['rights'].items() if value]
    denied=[label for label,value in snapshot['rights'].items() if not value]
    return (f'Rol: {snapshot["role"]}\nPermisos administrativos (última consulta):\n'
            +'✅ '+(', '.join(granted) or 'Ninguno')+'\n❌ '+(', '.join(denied) or 'Ninguno')+'\n')


def buttons(row):
    return [[{'text':label,'callback_data':f'manual:{action}:{row["token"]}:{row["position"]}'}]
            for action,label in [('ga','𝐀𝐃𝐃 𝐀𝐃𝐌𝐈𝐍'),('gr','𝐑𝐄𝐌𝐎𝐕𝐄 𝐀𝐃𝐌𝐈𝐍'),
                                 ('gu','𝐈𝐍𝐅𝐎 𝐔𝐒𝐔𝐀𝐑𝐈𝐎'),('gl','𝐈𝐍𝐅𝐎 𝐀𝐃𝐌𝐈𝐍𝐒'),('gi','𝐆𝐑𝐔𝐏𝐎 𝐈𝐍𝐅𝐎')]]


async def load(s,actor,which):
    value=await s.repo.setting(which(s,actor))
    try:return json.loads(value) if isinstance(value,str) and value.startswith('{') else None
    except (ValueError,TypeError):return None


async def clear(s,actor):
    await s.repo.set_setting(key(s,actor),'')
    await s.repo.set_setting(pending_key(s,actor),'')


async def bind(s,actor,row):
    await clear(s,actor)
    if row['source']<0:
        data=s.repo.decode(row['payload'])
        await s.repo.set_setting(key(s,actor),json.dumps({'token':row['token'],'uid':row['uid'],
            'source':row['source'],'generation':data['generation'],'expires':time.time()+1800}))


async def validate(s,actor,state):
    if not authorized(actor) or not state or state.get('expires',0)<=time.time():
        raise ValueError('La ficha del grupo venció. Vuelve a abrirla con /menu antes de administrar.')
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,state['token'])
    account=await s.repo.account(state['uid'])
    if (not row or row['actor']!=actor or row['closed'] or row['source']!=state['source']
        or row['uid']!=state['uid'] or not account or not account['session']
        or account['generation']!=state['generation']):
        raise ValueError('La cuenta o el grupo cambió. Abre de nuevo su menú.')
    return row


async def entity_for(s,client,state):
    from .manual_gallery import entity_for as resolve
    entity=await resolve(s,client,state['uid'],state['source'],state['generation'])
    entity=await client.get_entity(entity)
    if utils.get_peer_id(entity)!=state['source']:raise ValueError('El grupo no coincide.')
    return entity


def need(member,flag):
    rights=getattr(member.participant,'admin_rights',None)
    if not (member.is_creator or (member.is_admin and getattr(rights,flag,False))):
        raise ValueError('La cuenta conectada no tiene permiso para realizar ese cambio.')


async def target_user(s,client,entity,uid):
    """Resolve with this account's access hashes; never invent an access hash."""
    if isinstance(uid,str) and uid.startswith('@'):
        target=await client.get_entity(uid)
        if not isinstance(target,types.User):raise ValueError('El destinatario debe ser una persona o un bot.')
        return target
    try:
        target=await client.get_entity(types.PeerUser(uid))
        if isinstance(target,types.User) and target.id==uid:return target
    except (ValueError,errors.PeerIdInvalidError,errors.UserIdInvalidError):pass
    # A connected account's public username can resolve its ID even before joining.
    account=await s.repo.account(uid)
    if account and account.get('profile'):
        username=s.repo.decode(account['profile']).get('username')
        if username:
            try:
                target=await client.get_entity('@'+username.lstrip('@'))
                if isinstance(target,types.User) and target.id==uid:return target
            except (ValueError,errors.UsernameInvalidError,errors.UsernameNotOccupiedError):pass
    # Bounded lookup also resolves private users without public usernames.
    async for user in client.iter_participants(entity,limit=200):
        if user.id==uid:return user
    raise ValueError('No pude identificar ese ID con esta cuenta. Puedes escribir su @username en esta selección. Telegram debe permitir que la cuenta acceda a ese usuario o bot.')


def active_bans(participant):
    rights=getattr(getattr(participant,'participant',None),'banned_rights',None)
    expiry=getattr(rights,'until_date',None)
    stamp=expiry.timestamp() if isinstance(expiry,datetime) else expiry or 0
    return rights if stamp<=0 or stamp>datetime.now(timezone.utc).timestamp() else None


def defaults_copy(rights):
    result=copy.copy(rights) if rights else types.ChatBannedRights(until_date=0)
    if result.send_messages:
        for flag in MEDIA+('send_plain','send_polls','embed_links'):setattr(result,flag,True)
        result.send_messages=False
    if result.send_media:
        for flag in MEDIA:setattr(result,flag,True)
        result.send_media=False
    return result


async def prompt(s,actor,state,stage,**extra):
    await s.repo.set_setting(pending_key(s,actor),json.dumps(dict(state,stage=stage,**dict(extra,draft=uuid.uuid4().hex))))


async def callback(s,actor,query):
    if not authorized(actor) or query.get('message',{}).get('chat',{}).get('id')!=actor:
        raise ValueError('Solo administradores autorizados en el DM del bot.')
    parts=query['data'].split(':')
    if len(parts)!=4:raise ValueError('Abre la botonera actual.')
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,parts[2])
    if not row or row['actor']!=actor or row['closed'] or row['source']>=0 or row['message_id']!=query['message']['message_id']:
        raise ValueError('Abre la botonera actual de ese grupo.')
    if row['busy']:raise ValueError('Espera a que termine la descarga de esta cuenta.')
    for field in ('manual-search','manual-number','manual-anchor'):
        await s.repo.set_setting(f'{field}:{s.bot_id}:{actor}','')
    await bind(s,actor,row)
    state=await load(s,actor,key)
    await validate(s,actor,state)
    action=parts[1]
    if action in ('ga','gr','gu'):
        await prompt(s,actor,state,{'ga':'add_id','gr':'remove_id','gu':'info_id'}[action])
        return await s.tell(actor,f'Cuenta: {row["uid"]} · Grupo: {row["source"]}\nEscribe el telegram_id del usuario o bot. /menu cierra esta selección.')
    await execute(s,actor,state,'admins' if action=='gl' else 'group_info',None,None)


async def input_text(s,actor,text,update_id):
    state=await load(s,actor,pending_key)
    if not state:return False
    await validate(s,actor,state)
    stage=state['stage']
    if stage=='admins':
        users=s.repo.decode(bytes.fromhex(state['admins']))['users']
        if not re.fullmatch(r'[1-9][0-9]*',text) or not 1<=int(text)<=len(users):
            raise ValueError('Escribe un número de esta lista de administradores.')
        selected=users[int(text)-1]
        await execute(s,actor,dict(state,admin_selection=selected),'admin_info',selected['id'],update_id)
    elif stage.endswith('_id'):

        if re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{3,31}',text):target=text
        elif re.fullmatch(r'[1-9][0-9]{0,18}',text) and int(text)<2**63:target=int(text)
        else:raise ValueError('Escribe un telegram_id positivo válido o el @username del usuario o bot.')
        await execute(s,actor,state,stage,target,update_id)
    elif stage=='grant':
        numbers=parse_numbers(text,len(state['choices']))
        await prompt(s,actor,state,'rank',selected=[state['choices'][i-1][0] for i in numbers])
        await s.tell(actor,'Escribe la etiqueta del administrador (máximo 16 caracteres), o un guion - para dejarla vacía.')
    elif stage=='rank':
        rank='' if text=='-' else text.strip()
        if len(rank.encode('utf-16-le'))//2>16:raise ValueError('La etiqueta admite hasta 16 caracteres.')
        await execute(s,actor,dict(state,rank=rank),'promote',state['target'],update_id)
    elif stage=='defaults':
        numbers=parse_numbers(text,len(state['choices']))
        await execute(s,actor,dict(state,selected=[state['choices'][i-1][0] for i in numbers]),'defaults',None,update_id)
    return True


async def command(s,actor,text,update_id):
    match=re.fullmatch(r'/(ban|unban|free|restringir)(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?',text,re.S)
    if not match:return False
    state=await load(s,actor,key)
    if not state:return False
    if match[2]:
        me=await s.api('getMe',{})
        if match[2].casefold()!=me.get('username','').casefold():return False
    await validate(s,actor,state)
    value=(match[3] or '').strip()
    if not re.fullmatch(r'[1-9][0-9]{0,18}',value) or int(value)>=2**63:
        raise ValueError('En esta ficha, usa /'+match[1]+' telegram_id positivo. /menu cierra el contexto del grupo.')
    await execute(s,actor,state,match[1],int(value),update_id)
    return True


async def execute(s,actor,state,action,target_id,update_id):
    # The same actor's draft/commands cannot race between web instances.
    reserved=False
    async with s.repo.pool.acquire() as conn:
        lock=f'{s.bot_id}:{actor}'
        if not await conn.fetchval('SELECT pg_try_advisory_lock(757,hashtext($1))',lock):
            raise ValueError('Hay una operación administrativa en curso. Espera unos segundos.')
        try:
            async with connected(s,state['uid']) as client,asyncio.timeout(45):
                client.flood_sleep_threshold=0
                row=await validate(s,actor,state)
                if row['busy']:raise ValueError('Espera a que termine la descarga de esta cuenta.')
                active=await load(s,actor,key)
                if not active or active['token']!=state['token']:raise ValueError('Cambió el grupo activo. Abre su menú de nuevo.')
                entity=await entity_for(s,client,state)
                member=await client.get_permissions(entity,'me')
                context=f'Cuenta: {state["uid"]}\nGrupo: {entity.title} ({state["source"]})\n'
                if not isinstance(entity,types.Channel):raise ValueError('La administración detallada requiere un supergrupo o canal de Telegram.')
                if target_id==state['uid'] and action not in ('info_id','admin_info'):raise ValueError('No se permite modificar a la propia cuenta conectada desde este menú.')
                target=None;participant=None
                if target_id:
                    selection=state.get('admin_selection') if action=='admin_info' else None
                    if selection:
                        target=await client.get_entity(types.InputUser(selection['id'],selection['access_hash']))
                        if not isinstance(target,types.User) or target.id!=target_id:raise ValueError('El administrador no coincide. Vuelve a abrir INFO ADMINS.')
                    else:target=await target_user(s,client,entity,target_id)
                    target_id=target.id
                    if target_id==state['uid'] and action not in ('info_id','admin_info'):raise ValueError('No se permite modificar a la propia cuenta conectada desde este menú.')
                    try:participant=await client.get_permissions(entity,target)
                    except errors.UserNotParticipantError:participant=None
                if action in ('info_id','admin_info'):
                    bans=active_bans(participant)
                    banned=bool(bans and bans.view_messages)
                    restricted=bool(bans and any(getattr(bans,f,False) for f,_ in MEMBER) and not banned)
                    present=bool(participant and not participant.has_left and not getattr(participant.participant,'left',False) and not banned)
                    details=header(rights_snapshot(entity,participant)) if participant else 'Rol: No pertenece al grupo\n'
                    if participant and not participant.is_admin:
                        from .account_chat import effective_permissions
                        perms=effective_permissions(entity,participant,getattr(entity,'default_banned_rights',None))
                        details+='Envío: '+', '.join(label+(' ✅' if perms[flag] else ' ❌') for flag,label in [('text','Texto'),('photos','Fotos'),('videos','Videos'),('documents','Archivos'),('gifs','GIF')])+'\n'
                    details+=f'Miembro actual: {"Sí" if present else "No"}\nEtiqueta: {getattr(getattr(participant,"participant",None),"rank",None) or "Sin etiqueta"}\n'
                    await prompt(s,actor,state,'admins' if action=='admin_info' else 'info_id')
                    return await s.tell(actor,context+identity(target)+details+f'Baneado: {"Sí" if banned else "No"}\nRestringido: {"Sí" if restricted else "No"}\n\n/ban {target_id}\n/unban {target_id}\n/restringir {target_id}\n/free {target_id}\nEstos comandos actúan en este grupo. /menu cierra este contexto.')
                if action=='admins':
                    return await list_admins(s,actor,state,client,entity,context)
                if action=='group_info':
                    await remember_rights(s,actor,state,rights_snapshot(entity,member))
                    defaults=defaults_copy(getattr(entity,'default_banned_rights',None))
                    choices=list(MEMBER)+([('manage_topics','Crear y administrar temas')] if getattr(entity,'forum',False) else [])
                    if getattr(entity,'broadcast',False):choices=[]
                    lines='\n'.join(f'{i}. {label}: {"❌" if getattr(defaults,flag,False) else "✅"}' for i,(flag,label) in enumerate(choices,1))
                    can=bool(member.is_creator or (member.is_admin and getattr(getattr(member.participant,'admin_rights',None),'ban_users',False)))
                    if can and choices:await prompt(s,actor,state,'defaults',choices=choices)
                    return await s.tell(actor,context+header(rights_snapshot(entity,member))+'\nPERMISOS DE LOS MIEMBROS\n'+(lines or 'Canal: solo los administradores autorizados publican.')+('\n\nEscribe los números separados por comas para cambiar entre permitido y prohibido. Los demás se conservan.' if can and choices else '\nEsta cuenta no puede ajustar estos permisos.'))
                if participant and participant.is_creator:raise ValueError('No se pueden modificar los permisos del propietario.')
                if action=='remove_id' and (not participant or not participant.is_admin):raise ValueError('Ese usuario no es administrador de este grupo.')
                if action in ('add_id','promote','remove_id'):
                    need(member,'add_admins')
                    if participant and participant.is_admin and not member.is_creator and not getattr(participant.participant,'can_edit',False):
                        raise ValueError('Esta cuenta no puede editar a ese administrador.')
                else:need(member,'ban_users')
                if action=='add_id':
                    own=getattr(member.participant,'admin_rights',None)
                    choices=[(f,l) for f,l in applicable(entity) if member.is_creator or getattr(own,f,False)]
                    await prompt(s,actor,state,'grant',target=target_id,choices=choices)
                    return await s.tell(actor,context+'Selecciona los permisos que otorgarás, separados por comas (ejemplo: 1,3,4):\n'+'\n'.join(f'{i}. {l}' for i,(_,l) in enumerate(choices,1)))
                if action=='promote':
                    allowed={f for f,_ in applicable(entity) if member.is_creator or getattr(getattr(member.participant,'admin_rights',None),f,False)}
                    if not state['selected'] or not set(state['selected'])<=allowed:raise ValueError('Los permisos disponibles cambiaron. Vuelve a seleccionar ADD ADMIN.')
                if action in ('ban','restringir') and participant and participant.is_admin:
                    raise ValueError('Primero retira el cargo de administrador con REMOVE ADMIN.')
                if action=='free' and participant and getattr(active_bans(participant),'view_messages',False):
                    raise ValueError('Ese usuario está baneado. Usa /unban para permitir que vuelva a entrar.')
                if action=='unban' and participant and not getattr(active_bans(participant),'view_messages',False):
                    raise ValueError('Ese usuario no está baneado. Para quitar restricciones usa /free.')
                # Durable reservation BEFORE an RPC: duplicate updates cannot repeat a mutation.
                if update_id is None:raise ValueError('No se pudo identificar esta orden. Vuelve a intentarlo.')
                receipt=f'group-admin-action:{s.bot_id}:{update_id}'
                if await s.repo.setting(receipt):raise ValueError('Esta orden ya se procesó o necesita revisión. Consulta INFO antes de repetirla.')
                await validate(s,actor,state)
                active=await load(s,actor,key)
                if not active or active['token']!=state['token']:raise ValueError('Cambió el grupo activo. Abre su menú de nuevo.')
                if state.get('draft'):
                    pending=await load(s,actor,pending_key)
                    if not pending or pending.get('draft')!=state['draft']:raise ValueError('La selección de permisos cambió. Abre la opción de nuevo.')
                if not authorized(actor):raise ValueError('No autorizado.')
                await s.repo.set_setting(receipt,'started')
                reserved=True
                await s.repo.set_setting(pending_key(s,actor),'')
                if action in ('promote','remove_id'):
                    rights=types.ChatAdminRights(**{f:True for f in state['selected']}) if action=='promote' else types.ChatAdminRights()
                    await client(functions.channels.EditAdminRequest(entity,target,rights,state.get('rank','') if action=='promote' else ''))
                elif action=='defaults':
                    rights=defaults_copy(getattr(entity,'default_banned_rights',None))
                    for flag in state['selected']:setattr(rights,flag,not bool(getattr(rights,flag,False)))
                    await client(functions.messages.EditChatDefaultBannedRightsRequest(entity,rights))
                else:
                    rights=types.ChatBannedRights(until_date=0,view_messages=True) if action=='ban' else types.ChatBannedRights(until_date=0,send_messages=True) if action=='restringir' else types.ChatBannedRights(until_date=0)
                    await client(functions.channels.EditBannedRequest(entity,target,rights))
                await s.repo.set_setting(receipt,'done')
                return await s.tell(actor,'✅ Cambio aplicado en Telegram.\n'+context+(f'Usuario: {target_id}' if target_id else 'Permisos de los miembros actualizados.'))
        except errors.FloodWaitError as exc:
            raise ValueError(f'Telegram pide esperar {exc.seconds} segundos. No se repetirá el cambio automáticamente; consulta INFO antes de reintentarlo.') from exc
        except errors.RPCError as exc:
            raise ValueError(f'Telegram rechazó el cambio o la consulta: {type(exc).__name__}. Consulta INFO para revisar el estado actual.') from exc
        except TimeoutError as exc:
            raise ValueError('Telegram no confirmó la operación. Consulta INFO antes de repetir para comprobar su resultado.') from exc
        except (ConnectionError,OSError) as exc:
            if reserved:raise ValueError('No se pudo confirmar el cambio. Consulta INFO antes de repetirlo.') from exc
            raise
        finally:await conn.execute('SELECT pg_advisory_unlock(757,hashtext($1))',lock)


async def remember_rights(s,actor,state,snapshot):
    async with s.repo.pool.acquire() as conn,conn.transaction():
        row=await conn.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2 FOR UPDATE',s.bot_id,state['token'])
        if not row or row['actor']!=actor or row['closed']:return
        data=s.repo.decode(row['payload']);data['account_rights']=snapshot
        await conn.execute('UPDATE descarga_manual_boards SET payload=$3 WHERE bot_id=$1 AND token=$2',s.bot_id,row['token'],s.repo.encode(data))
    from .manual_board import render
    await render(s,row)


def identity(user):
    name=' '.join(filter(None,(getattr(user,'first_name',None),getattr(user,'last_name',None))))
    name=' '.join(name.split())[:128] or 'Sin nombre'
    return (f'Nombre en Telegram: {name}\nTelegram ID: {user.id}\n'
            f'Username: {"@"+user.username if getattr(user,"username",None) else "Sin username"}\n'
            f'Tipo: {"Bot" if getattr(user,"bot",False) else "Cuenta regular"}\n')


async def list_admins(s,actor,state,client,entity,context):
    users=[];seen=set()
    # Telegram filters administrators on the server; no scan of all members.
    async for user in client.iter_participants(entity,filter=types.ChannelParticipantsAdmins()):
        if not authorized(actor):raise ValueError('No autorizado.')
        if user.id in seen:continue
        seen.add(user.id)
        name=' '.join(filter(None,(getattr(user,'first_name',None),getattr(user,'last_name',None))))
        users.append({'id':user.id,'access_hash':getattr(user,'access_hash',None) or 0,
            'name':' '.join(name.split())[:128] or 'Sin nombre','username':getattr(user,'username',None),
            'bot':bool(getattr(user,'bot',False)),
            'owner':isinstance(getattr(user,'participant',None),types.ChannelParticipantCreator)})
    await validate(s,actor,state)
    active=await load(s,actor,key)
    if not active or active['token']!=state['token']:raise ValueError('Cambió el grupo activo. Abre INFO ADMINS otra vez.')
    users.sort(key=lambda u:(not u['owner'],u['name'].casefold(),u['id']))
    if not users:return await s.tell(actor,context+'Telegram no devolvió administradores visibles para esta cuenta.')
    # Access hashes belong to the connected account and stay encrypted at rest.
    await prompt(s,actor,state,'admins',admins=s.repo.encode({'users':users}).hex())
    title=context+f'ADMINISTRADORES · {len(users)}\nEscribe un número para ver su ficha y permisos actuales.\n\n'
    text=title
    for i,user in enumerate(users,1):
        line=f'{i}. {"⭐️" if user["owner"] else "👨🏻‍💻"} {user["name"]} · {"Bot" if user["bot"] else "Cuenta regular"}\nID: {user["id"]} · {"@"+user["username"] if user["username"] else "Sin username"}\n\n'
        if len((text+line).encode('utf-16-le'))//2>3500:
            await s.tell(actor,text);text=title
        text+=line
    await s.tell(actor,text)
