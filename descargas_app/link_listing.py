"""Admin-only media link listing, without downloads or export cursor changes."""
import asyncio
import math
import re
import time
from telethon import errors, types, utils
from .moderation import authorized, ensure_allowed
from .protected import require_entity
from .paid_media import purchased_items


def media_label(message):
    if getattr(message,'sticker',None):return None
    if getattr(message,'gif',None):return 'GIF'
    if getattr(message,'photo',None):return 'Foto'
    document=getattr(message,'document',None)
    mime=getattr(document,'mime_type','') or ''
    if mime.startswith('video/'):return 'Video'
    if mime.startswith('image/'):return 'Imagen'
    return None


async def run(service, actor, argument):
    if not authorized(actor):return
    match=re.fullmatch(r'([1-9][0-9]{0,18})\s+(-[1-9][0-9]{0,18})(?:\s+([0-9]{1,10}))?\s*',argument.strip())
    if not match:
        await service.tell(actor,'Uso: /enlace telegram_id group_id (también canal_id).')
        return
    uid,source=int(match[1]),int(match[2])
    cursor=int(match[3] or 0)
    if uid>=2**63 or source<=-(2**63) or cursor>=2**31:
        await service.tell(actor,'Los identificadores no son válidos.');return
    if not str(source).startswith('-100'):
        await service.tell(actor,'Este listado necesita el ID -100 de un supergrupo o canal para generar enlaces privados descargables.');return
    cooldown_key=f'link-list-wait:{service.bot_id}:{uid}'
    cooldown=max(float(await service.repo.setting(cooldown_key) or 0),await service.repo.pool.fetchval('SELECT max(resume_at) FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2',service.bot_id,uid) or 0)
    if cooldown>time.time():
        await service.tell(actor,f'Telegram exige esperar {math.ceil(cooldown-time.time())} segundos antes de consultar esa cuenta.');return
    account=await service.repo.account(uid)
    if not account or not account['session']:
        await service.tell(actor,'Esa cuenta no está conectada al bot.');return
    generation=account['generation']
    number=0;seen=set()
    await service.tell(actor,f'🔎 Buscando enlaces de multimedia con la cuenta {uid}. No descarga archivos. El listado se enviará por partes.')
    try:
        while not service.stopping:
            async with service.repo.account_lock(uid,wait=False):
                fresh=await service.repo.account(uid)
                if not authorized(actor) or not fresh or not fresh['session'] or fresh['generation']!=generation:
                    raise ValueError('La sesión o la autorización cambió; vuelve a solicitar el listado.')
                await ensure_allowed(service,uid,source)
                client=service.client(await service.get_session(uid))
                client.flood_sleep_threshold=0
                try:
                    await client.connect()
                    if not await client.is_user_authorized():raise ValueError('La cuenta debe conectarse de nuevo.')
                    entity=await service.resolve(client,source)
                    if not isinstance(entity,types.Channel) or utils.get_peer_id(entity)!=source:
                        raise ValueError('El grupo o canal no corresponde al ID indicado.')
                    require_entity(entity,uid)
                    await service.repo.pace(f'enlace-read:{uid}',3.1)
                    messages=[m async for m in client.iter_messages(entity,min_id=cursor,reverse=True,limit=100)]
                finally:
                    await client.disconnect()
            lines=[]
            for message in messages:
                try:items=purchased_items(message)
                except ValueError:items=[] # Never list paid previews as accessible media.
                eligible=any(media_label(m) for m in items) if items is not None else media_label(message)
                if eligible:
                    key=getattr(message,'grouped_id',None) or message.id
                    if key not in seen:
                        seen.add(key);number+=1
                        label='Álbum' if getattr(message,'grouped_id',None) or (items is not None and len(items)>1) else (media_label(items[0]) if items else media_label(message))
                        lines.append(f'{number}. {label}: https://t.me/c/{str(source)[4:]}/{message.id}')
                cursor=max(cursor,message.id)
            for offset in range(0,len(lines),15):
                await service.tell(actor,f'🔗 ENLACES · Cuenta {uid} · Origen {source}\n\n'+'\n'.join(lines[offset:offset+15]))
            if len(messages)<100:
                await service.tell(actor,f'✅ Listado terminado: {number} enlaces de multimedia. Un enlace por álbum; sin textos ni documentos de otros tipos.\nPara descargar: /descargar {uid} enlace')
                return
            await asyncio.sleep(0) # Yield between pages; the account lock is released.
    except errors.FloodWaitError as exc:
        await service.repo.set_setting(cooldown_key,time.time()+max(1,exc.seconds)+10)
        await service.tell(actor,f'⏳ Telegram pide esperar {exc.seconds} segundos. Listado parcial: {number} enlaces. Después continúa con /enlace {uid} {source} {cursor}')
    except ValueError as exc:
        await service.tell(actor,f'⏸ Listado interrumpido: {exc}\nPara continuar: /enlace {uid} {source} {cursor}')
