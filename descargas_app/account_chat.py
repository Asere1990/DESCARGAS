"""Private administrative account permissions and explicit single text sends."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import re
import time
from telethon import errors, types
from .moderation import authorized

SCHEMA='''
CREATE TABLE IF NOT EXISTS descarga_account_messages(
 bot_id BIGINT NOT NULL,update_id BIGINT NOT NULL,actor BIGINT NOT NULL,
 uid BIGINT NOT NULL,source BIGINT NOT NULL,state TEXT NOT NULL,
 message_id BIGINT,PRIMARY KEY(bot_id,update_id));
'''


def selection_key(service, actor, uid):
    return f'account_groups:{service.bot_id}:{actor}:{uid}'


async def remember_groups(service, actor, uid, ids):
    await service.repo.set_setting(selection_key(service,actor,uid),
        json.dumps({'ids':ids,'expires':time.time()+1800}))


@asynccontextmanager
async def connected(service, uid):
    async with service.repo.account_lock(uid,wait=False):
        session=await service.get_session(uid)
        if not session:raise ValueError('Esa cuenta no está conectada al bot.')
        client=service.client(session)
        try:
            await client.connect()
            if not await client.is_user_authorized():raise ValueError('La sesión fue revocada. Conecta de nuevo la cuenta.')
            me=await client.get_me()
            if me.id!=uid:raise ValueError('La cuenta conectada no coincide con el Telegram ID.')
            yield client
        finally:await client.disconnect()


def effective_permissions(entity, participant, defaults):
    """Combine Telegram defaults and active individual restrictions, not bot rules."""
    role='Creador' if participant.is_creator else 'Administrador' if participant.is_admin else 'Miembro'
    gone=bool(participant.has_left or getattr(participant.participant,'left',False) or getattr(entity,'left',False))
    bans=[defaults,getattr(participant.participant,'banned_rights',None)]
    active=[]
    now=datetime.now(timezone.utc).timestamp()
    for rights in bans:
        if rights is None:continue
        until=getattr(rights,'until_date',None)
        stamp=until.timestamp() if isinstance(until,datetime) else until or 0
        if stamp<=0 or stamp>now:active.append(rights)
    def denied(flag):return any(bool(getattr(b,flag,False)) for b in active)
    if gone or (not participant.is_admin and denied('view_messages')):
        base=False
    elif getattr(entity,'broadcast',False):
        base=bool(participant.is_creator or (participant.is_admin and participant.post_messages))
    else:base=True
    def allow(flag,media=False):
        if not base:return False
        if participant.is_admin:return True
        return not (denied('send_messages') or denied(flag) or (media and denied('send_media')))
    text=allow('send_plain')
    return {'role':role,'text':text,'links':text,'previews':text and allow('embed_links'),
        'photos':allow('send_photos',True),'videos':allow('send_videos',True),
        'documents':allow('send_docs',True),'audio':allow('send_audios',True),
        'voices':allow('send_voices',True),'stickers':allow('send_stickers',True),
        'gifs':allow('send_gifs',True),'polls':allow('send_polls')}


async def inspect_permissions(client, source):
    entity=None
    async for dialog in client.iter_dialogs():
        if dialog.id==source:
            entity=dialog.entity
            break
    if not isinstance(entity,(types.Chat,types.Channel)):
        raise ValueError('El destino debe ser un grupo o canal de esa cuenta.')
    # Refetch actual permissions; the earlier list is only a stable ID mapping.
    entity=await client.get_entity(entity)
    member=await client.get_permissions(entity,'me')
    defaults=await client.get_permissions(entity)
    return entity,effective_permissions(entity,member,defaults)


async def show_permissions(service, actor, argument):
    if not authorized(actor):return
    match=re.fullmatch(r'([1-9][0-9]{0,15})\s*,\s*([1-9][0-9]{0,5})',argument.strip())
    if not match:return await service.tell(actor,'Uso: telegram_id,número de lista. Ejemplo: 1609522430,3')
    uid,index=map(int,match.groups())
    saved=await service.repo.setting(selection_key(service,actor,uid))
    data=json.loads(saved) if saved else {}
    if data.get('expires',0)<time.time() or not 1<=index<=len(data.get('ids',[])):
        return await service.tell(actor,f'La lista venció o ese número no existe. Ejecuta /grupos {uid} otra vez.')
    source=data['ids'][index-1]
    try:
        async with connected(service,uid) as client,asyncio.timeout(60):
            entity,permissions=await inspect_permissions(client,source)
        labels=[('text','Escribir texto'),('links','Enviar enlaces en texto'),('previews','Vista previa de enlaces'),
            ('photos','Enviar fotos'),('videos','Enviar videos'),('documents','Enviar archivos'),
            ('audio','Enviar audio'),('voices','Enviar notas de voz'),('stickers','Enviar stickers'),
            ('gifs','Enviar GIF'),('polls','Enviar encuestas')]
        lines='\n'.join(f'{label}: {"✅ Sí" if permissions[key] else "❌ No"}' for key,label in labels)
        title=' '.join(entity.title.split())[:128]
        await service.tell(actor,f'🔐 PERMISOS · {title}\nCuenta: {uid}\nID: {source}\nRol: {permissions["role"]}\n\n{lines}\n\nSon permisos de Telegram. Los filtros de administradores/bots pueden borrar mensajes o enlaces.\n\n/chat {uid} {source} tu mensaje\n/descargar {uid} {source}')
    except asyncio.CancelledError:raise
    except Exception as exc:
        from .service import error_message
        await service.tell(actor,str(exc) if isinstance(exc,ValueError) else error_message(exc))


async def send_text(service, actor, argument, update_id):
    if not authorized(actor):return
    match=re.fullmatch(r'([1-9][0-9]{0,15})\s+(-[1-9][0-9]{0,15})\s+([\s\S]+)',argument)
    if not match or update_id is None:
        return await service.tell(actor,'Uso: /chat telegram_id group_id mensaje (también acepta channel_id).')
    uid,source=int(match[1]),int(match[2]);text=match[3]
    if not text.strip() or len(text.encode('utf-16-le'))//2>4096:
        return await service.tell(actor,'Escribe un mensaje de 1 a 4096 caracteres.')
    reserved=False
    try:
        async with connected(service,uid) as client,asyncio.timeout(60):
            entity,permissions=await inspect_permissions(client,source)
            if not permissions['text']:
                return await service.tell(actor,'❌ Esa cuenta no tiene permiso para escribir en ese grupo o canal.')
            if not authorized(actor):return
            row=await service.repo.pool.fetchrow('''INSERT INTO descarga_account_messages(bot_id,update_id,actor,uid,source,state)
                VALUES($1,$2,$3,$4,$5,'sending') ON CONFLICT DO NOTHING RETURNING update_id''',service.bot_id,update_id,actor,uid,source)
            if not row:
                return await service.tell(actor,'Esta orden /chat ya fue procesada o requiere revisión; no se repetirá.')
            reserved=True
            sent=await client.send_message(entity,text,parse_mode=None,link_preview=permissions['previews'])
            await service.repo.pool.execute("UPDATE descarga_account_messages SET state='sent',message_id=$3 WHERE bot_id=$1 AND update_id=$2",service.bot_id,update_id,sent.id)
        await service.tell(actor,f'✅ Mensaje enviado con la cuenta {uid} a {entity.title} ({source}).\nID del mensaje: {sent.id}')
    except asyncio.CancelledError:raise
    except Exception as exc:
        if reserved:
            state='rejected' if isinstance(exc,errors.RPCError) else 'review'
            await service.repo.pool.execute('UPDATE descarga_account_messages SET state=$3 WHERE bot_id=$1 AND update_id=$2',service.bot_id,update_id,state)
        if reserved and not isinstance(exc,errors.RPCError):
            return await service.tell(actor,'No se pudo confirmar el envío. Revisa el grupo antes de repetir /chat; podría haberse publicado.')
        from .service import error_message
        await service.tell(actor,str(exc) if isinstance(exc,ValueError) else error_message(exc))
