"""Persistent download restrictions and trusted ALMACEN provenance."""
from contextvars import ContextVar
import re

SOURCE=ContextVar('download_source',default=None)

class DownloadBlocked(ValueError):
    pass

async def ensure_allowed(service,uid,source=None):
    if await service.repo.is_blocked(service.bot_id,uid,source):
        raise DownloadBlocked('Bot fuera de servicio')

async def command(service,message):
    text=message.get('text','').strip()
    match=re.match(r'^/(ban|unban)(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?$',text,re.S)
    if not match:return False
    chat=message.get('chat',{});actor=message.get('from',{})
    if chat.get('type')!='private' and chat.get('id')!=service.almacen:return True
    # Trust only Telegram's own sender_chat for anonymous ALMACEN admins.
    anonymous=message.get('sender_chat',{}).get('id')==service.almacen and chat.get('id')==service.almacen
    if match.group(2):
        me=await service.api('getMe',{})
        if match.group(2).lower()!=str(me.get('username','')).lower():return True
    if not anonymous and (not actor.get('id') or actor.get('is_bot') or message.get('sender_chat')):return True
    actor_id=service.almacen if anonymous else actor['id']
    async def reply(text):
        await service.api('sendMessage',{'chat_id':chat['id'],'text':text,
            'reply_parameters':{'message_id':message['message_id']}})
    try:
        member={'status':'administrator'} if anonymous else await service.api('getChatMember',{'chat_id':service.almacen,'user_id':actor_id})
        if member.get('status') not in ('creator','administrator'):
            await reply('Solo los administradores de ALMACEN pueden usar este comando.');return True
        raw=(match.group(3) or '').strip()
        if raw:
            if not re.fullmatch(r'-?[1-9][0-9]{0,15}',raw):
                await reply('Usa /ban ID o /unban ID. Para bloquear ambos, responde al contenido en ALMACEN con /ban.');return True
            targets=[int(raw)]
        else:
            original=message.get('reply_to_message',{})
            if chat.get('id')!=service.almacen or not original.get('message_id'):
                await reply('Indica un ID o responde al contenido en ALMACEN.');return True
            origin=await service.repo.archive_origin(service.bot_id,service.almacen,original['message_id'])
            if not origin:
                await reply('Este contenido no tiene un origen registrado. Usa el comando con cada ID por separado.');return True
            targets=[origin['uid'],origin['source']]
        if any(not (-2**63<t<2**63) or t==0 for t in targets):raise ValueError('ID no válido.')
        await service.repo.set_blocks(service.bot_id,targets,match.group(1)=='ban',actor_id)
        action='Bloqueado' if match.group(1)=='ban' else 'Desbloqueado'
        await reply('\n'.join(f'{action}: {"usuario" if t>0 else "grupo"} {t}' for t in targets))
    except ValueError as exc:await reply(str(exc))
    return True
