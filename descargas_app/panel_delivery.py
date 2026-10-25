"""Durable, actor-scoped panel deliveries using the shared archive exporter."""
import asyncio
import json
import logging
import time
import uuid
from aiohttp import web
from telethon import utils,errors
from .creator_access import ACTOR,require_account,require_source,reviewer
from .moderation import authorized,SOURCE
LOG=logging.getLogger(__name__)


def archive_link(chat,mid):
    value=str(chat)
    return f'https://t.me/c/{value[4:]}/{mid}' if value.startswith('-100') else None


async def enqueue(panel,actor,data):
    from .web_panel import integer,media_info
    uid=integer(data.get('account'));raw=data.get('messages',[])
    if not isinstance(raw,list):raise ValueError('Selecciona los archivos que quieres descargar.')
    limit=10 if reviewer(actor) else 500
    if not 1<=len(raw)<=limit:raise ValueError(f'Selecciona entre 1 y {limit} archivos.')
    ids=list(dict.fromkeys(integer(x) for x in raw))
    # Validate selection before creating durable work; workers revalidate later.
    async with panel.account(actor,uid) as (client,generation):
        entity=await panel.target(actor,uid,generation,client,data.get('peer',''))
        source=utils.get_peer_id(entity)
        rows=await client.get_messages(entity,ids=ids)
        found={m.id:m for m in rows if m and utils.get_peer_id(m.peer_id)==source and media_info(m)}
        if set(found)!=set(ids):raise ValueError('Un archivo seleccionado ya no está disponible.')
    token=uuid.uuid4().hex;key=f'panel-delivery:{panel.s.bot_id}:{token}'
    job={'key':key,'bot_id':panel.s.bot_id,'uid':uid,'actor':actor,'source':source,'generation':generation,
         'handle':data.get('peer',''),'ids':ids,'state':'queued','created':time.time(),'panel_delivery':True}
    await panel.s.repo.set_setting(key,panel.s.repo.encode(job).decode())
    return {'ok':True,'job':token,'message':'Descarga registrada. Recibirás la confirmación en el bot.'}


async def check(service,job):
    if not authorized(job['actor']):raise ValueError('Acceso retirado.')
    await require_account(service,job['uid'],job['actor'])
    if reviewer(job['actor']) and len(job['ids'])>10:raise ValueError('El límite actual es 10 archivos seleccionados.')
    row=await service.repo.account(job['uid'])
    if not row or not row['session'] or row['generation']!=job['generation']:raise ValueError('La sesión de la cuenta cambió.')
    raw=await service.repo.setting(job['key'])
    if not raw or service.repo.decode(raw.encode())['state']!='running':return False
    from .moderation import ensure_allowed
    await ensure_allowed(service,job['uid'],job['source'])
    if (await service.repo.policy(service.bot_id,job['uid'],job['source']))['locked']:raise ValueError('El usuario o el grupo tiene /lock.')
    return True


async def finish(s,job):
    await s.repo.set_setting(job['key'].replace('panel-delivery:','panel-delivery-history:',1),s.repo.encode(job).decode())
    await s.repo.pool.execute('DELETE FROM descarga_settings WHERE key=$1',job['key'])


async def save(s,job):await s.repo.set_setting(job['key'],s.repo.encode(job).decode())


async def process(s,job):
    from .web_panel import TelegramPanel
    from .group_download import archive_items
    from .service import album
    from .media_cache import cache_key
    if not await check(s,job):raise ValueError('La entrega ya no está activa.')
    if not s.almacen:raise ValueError('ALMACEN no está configurado.')
    panel=TelegramPanel(s);actor=job['actor'];uid=job['uid']
    async with panel.account(actor,uid,timeout=3600) as (client,generation):
        entity=await panel.target(actor,uid,generation,client,job['handle'])
        me=await client.get_me();name=' '.join(filter(None,[me.first_name,me.last_name])) or str(uid)
        rows=await client.get_messages(entity,ids=job['ids']);rows=sorted((m for m in rows if m),key=lambda m:m.id)
        if len(rows)!=len(job['ids']):raise ValueError('Un archivo seleccionado ya no está disponible.')
        done=set();dm=reviewer(actor)
        for message in rows:
            if utils.get_peer_id(message.peer_id)!=job['source']:raise ValueError('El mensaje no corresponde al origen.')
            group=getattr(message,'grouped_id',None) or message.id
            if group in done:continue
            done.add(group);items=await album(client,entity,message)
            key=(job['source'],group);identity=cache_key(s.bot_id,s.almacen,key,items)
            result=await archive_items(s,job,client,key,items,name)
            if result is None:raise ValueError('Hay otra transferencia en curso. Vuelve a solicitar la descarga.')
            cached=await s.repo.media_cache(identity)
            if not cached or cached['state']!='ready':raise ValueError('No hay copia confirmada en ALMACEN; no se enviará contenido incierto.')
            ids=s.repo.decode(cached['payload'])['ids']
            if len(ids)!=len(items):raise ValueError('El álbum guardado requiere revisión.')
            link=archive_link(s.almacen,ids[0]);origin=archive_link(job['source'],items[0].id)
            note=('Ya se descargó a ALMACEN.' if result==0 else 'Guardado en ALMACEN.')+f'\nSolicitante: {actor}\nCuenta utilizada: {name} ({uid})'
            if origin:note+='\nEnlace de origen: '+origin
            if link:note+='\nALMACEN: '+link
            await require_source(s,uid,job['source'],client=client,entity=entity,actor=actor)
            if not await check(s,job):raise ValueError('Acceso retirado.')
            # Audit each requester separately without publishing the media twice.
            await s.api('sendMessage',{'chat_id':s.almacen,'text':note,'reply_parameters':{'message_id':ids[0]}})
            # Preserve accessible replied-to text as a separate reply, avoiding
            # caption truncation or modification of the original album captions.
            if result:
                replies=set()
                for item in items:
                    reply_id=getattr(item,'reply_to_msg_id',None)
                    if reply_id and reply_id not in replies:
                        replies.add(reply_id);reply=await client.get_messages(entity,ids=reply_id)
                        if reply and getattr(reply,'message','') and utils.get_peer_id(reply.peer_id)==job['source']:
                            text='Mensaje al que responde el contenido:\n'+reply.message
                            for start in range(0,len(text),3500):
                                await s.api('sendMessage',{'chat_id':s.almacen,'text':text[start:start+3500],'reply_parameters':{'message_id':ids[0]}})
            if result:
                for item in items:
                    if not getattr(getattr(item,'replies',None),'replies',0):continue
                    try:
                        async for reply in client.iter_messages(entity,reply_to=item.id,reverse=True):
                            text=getattr(reply,'message','')
                            if not text:continue
                            reply_source=utils.get_peer_id(reply.peer_id)
                            await require_source(s,uid,reply_source,client=client,actor=actor)
                            if not await check(s,job):raise ValueError('Acceso retirado.')
                            text='Comentario asociado:\n'+text
                            for start in range(0,len(text),3500):
                                await s.api('sendMessage',{'chat_id':s.almacen,'text':text[start:start+3500],'reply_parameters':{'message_id':ids[0]}})
                    except errors.RPCError:
                        await s.api('sendMessage',{'chat_id':s.almacen,'text':'Telegram no permitió consultar todos los comentarios asociados.','reply_parameters':{'message_id':ids[0]}})
            if dm:
                selected=[stored for item,stored in zip(items,ids) if item.id in job['ids']]
                # Only selected media, with no administrative caption in the DM.
                delivered=await s.api('copyMessages',{'chat_id':actor,'from_chat_id':s.almacen,'message_ids':sorted(selected),'remove_caption':True})
                if not isinstance(delivered,list) or len(delivered)!=len(selected):raise ValueError('Telegram no confirmó todos los archivos del DM; revisa lo recibido antes de repetir.')
            await s.tell(actor,note)


async def run(s):
    while not s.stopping:
        try:
            rows=await s.repo.pool.fetch('SELECT key,value FROM descarga_settings WHERE key LIKE $1 ORDER BY key LIMIT 100',f'panel-delivery:{s.bot_id}:%')
            for row in rows:
                job=s.repo.decode(row['value'].encode())
                if job.get('state') not in ('queued','running'):continue
                async with s.repo.pool.acquire() as conn:
                    locked=await conn.fetchval('SELECT pg_try_advisory_lock(752,hashtext($1))',row['key'])
                    if not locked:continue
                    try:
                        raw=await s.repo.setting(row['key']);job=s.repo.decode(raw.encode())
                        if job['state'] not in ('queued','running'):continue
                        if job['state']=='running':raise ValueError('La entrega se interrumpió y requiere revisión antes de repetirla.')
                        job['state']='running';await save(s,job)
                        marker=ACTOR.set(job['actor']);source=SOURCE.set(job['source'])
                        try:
                            async with asyncio.timeout(3600):await process(s,job)
                        finally:ACTOR.reset(marker);SOURCE.reset(source)
                        job['state']='done';await finish(s,job)
                    except asyncio.CancelledError:raise
                    except Exception as exc:
                        job['state']='review';job['error']=type(exc).__name__;await finish(s,job)
                        LOG.warning('Panel delivery requires review (%s)',type(exc).__name__)
                        try:await s.tell(job['actor'],'No se completó toda la entrega. '+(str(exc) if isinstance(exc,ValueError) else 'Requiere revisión para evitar duplicados.'))
                        except Exception:pass
                    finally:await conn.execute('SELECT pg_advisory_unlock(752,hashtext($1))',row['key'])
        except asyncio.CancelledError:raise
        except Exception as exc:LOG.warning('Panel delivery loop delayed (%s)',type(exc).__name__)
        await asyncio.sleep(3)
