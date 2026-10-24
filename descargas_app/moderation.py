"""Persistent download restrictions and trusted ALMACEN provenance."""
from contextvars import ContextVar
import re
from pathlib import Path

ADMIN_FILE=Path(__file__).resolve().parent.parent/'admins.txt'

def authorized(uid):
    try:
        entries={int(token) for line in ADMIN_FILE.read_text().splitlines() for token in line.split('#',1)[0].replace(',',' ').split() if token.isdecimal()}
        from .creator_access import ids
        return uid in entries or uid in ids('police.txt')
    except (OSError,ValueError):return False

SOURCE=ContextVar('download_source',default=None)

class DownloadBlocked(ValueError):
    pass

async def ensure_allowed(service,uid,source=None):
    from .creator_access import ACTOR,police,require_consent
    actor=ACTOR.get()
    reader=getattr(service.repo,"creator_registration",None)
    if actor is not None and police(actor) and reader and await reader(service.bot_id,uid):
        await require_consent(service,actor,uid)
        return
    if authorized(uid):return
    from .protected import require_source
    require_source(source,uid)
    if await service.repo.is_blocked(service.bot_id,uid,source):
        raise DownloadBlocked('Bot fuera de servicio')
    if getattr(service,'billing',None):await service.billing.require(uid,source)

async def command(service,message):
    text=message.get('text','').strip()
    match=re.match(r'^/(ban|unban|pay|free|lock|unlock)(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?$',text,re.S)
    if not match:return False
    chat=message.get('chat',{});actor=message.get('from',{})
    if chat.get('type')!='private' and chat.get('id')!=service.almacen:return True
    if match.group(2):
        me=await service.api('getMe',{})
        if match.group(2).lower()!=str(me.get('username','')).lower():return True
    actor_id=actor.get('id')
    async def reply(text):
        await service.api('sendMessage',{'chat_id':chat['id'],'text':text,
            'reply_parameters':{'message_id':message['message_id']}})
    try:
        if message.get('sender_chat') or actor.get('is_bot'):
            await reply('Envía este comando desde tu cuenta, sin modo anónimo.');return True
        if not actor_id or not authorized(actor_id):
            await reply('Comando disponible solo para administradores autorizados en admins.txt.');return True
        raw=(match.group(3) or '').strip()
        if raw:
            if not re.fullmatch(r'-?[1-9][0-9]{0,15}',raw):
                await reply('Usa /ban ID o /unban ID. Para bloquear ambos, responde al contenido en ALMACEN con /ban.');return True
            targets=[int(raw)]
        else:
            if match.group(1) in ('lock','unlock'):
                from .export_inventory import reply_lock
                await reply_lock(service,message,match.group(1),actor_id)
                return True
            if match.group(1) not in ('ban','unban'):
                await reply('Indica el ID del usuario, grupo o canal.');return True
            original=message.get('reply_to_message',{})
            if chat.get('id')!=service.almacen or not original.get('message_id'):
                await reply('Indica un ID o responde al contenido en ALMACEN.');return True
            origin=await service.repo.archive_origin(service.bot_id,service.almacen,original['message_id'])
            if not origin:
                await reply('Este contenido no tiene un origen registrado. Usa el comando con cada ID por separado.');return True
            targets=[origin['uid'],origin['source']]
        if any(not (-2**63<t<2**63) or t==0 for t in targets):raise ValueError('ID no válido.')
        operation=match.group(1)
        if operation=='free' and any(target<0 for target in targets):
            await reply('Usa /free telegram_id con el ID positivo del usuario.');return True
        if operation in ('ban','unban'):
            await service.repo.set_blocks(service.bot_id,targets,operation=='ban',actor_id)
            action='Bloqueado' if operation=='ban' else 'Desbloqueado'
        else:
            for target in targets:await service.repo.policy_set(service.bot_id,target,'pay' if operation in ('pay','free') else 'locked',operation not in ('free','unlock'),actor_id)
            action={'pay':'Pago mensual activado para','free':'Requisito de pago retirado para','lock':'Almacenamiento bloqueado para','unlock':'Almacenamiento permitido para'}[operation]
        await reply('\n'.join(f'{action}: {"usuario" if t>0 else "grupo"} {t}' for t in targets))
    except ValueError as exc:await reply(str(exc))
    return True
