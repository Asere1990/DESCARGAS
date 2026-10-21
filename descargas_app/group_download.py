"""Administrative, resumable history export; independent from requested DM jobs."""
import asyncio
import logging
import re
import tempfile
import time
import math
from telethon import types, utils, functions, errors
from .moderation import authorized, ensure_allowed, SOURCE
from .protected import require_entity
from .service import MAX_FILE, MAX_ALBUM, archive_attribution, apply_archive_caption, prepare_media_for_bot
from .transfer import prepare_album
from .media_cache import cache_key, append_archive_source

LOG = logging.getLogger(__name__)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS descarga_export_uncertain(
 bot_id BIGINT NOT NULL,almacen BIGINT NOT NULL,source BIGINT NOT NULL,
 kind TEXT NOT NULL,file_id BIGINT NOT NULL,media_key TEXT NOT NULL,
 PRIMARY KEY(bot_id,almacen,source,kind,file_id));
CREATE TABLE IF NOT EXISTS descarga_group_exports(
 bot_id BIGINT NOT NULL, uid BIGINT NOT NULL, source BIGINT NOT NULL,
 actor BIGINT NOT NULL, generation BIGINT NOT NULL, cursor BIGINT NOT NULL DEFAULT 0,
 total BIGINT NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'paused',
 sending BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(bot_id,uid,source));
ALTER TABLE descarga_group_exports ADD COLUMN IF NOT EXISTS scan BIGINT NOT NULL DEFAULT 0;
ALTER TABLE descarga_group_exports ADD COLUMN IF NOT EXISTS resume_at DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE descarga_group_exports ADD COLUMN IF NOT EXISTS media_filter TEXT NOT NULL DEFAULT 'all';
ALTER TABLE descarga_group_exports ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT 'download';
'''


def parse_command(text):
    match = re.fullmatch(r'/(descargar|stop)(?:@[A-Za-z0-9_]+)?(?:\s+(\d+)\s+(-\d+)(?:\s+(video|videos|foto|fotos|gif|gifs))?)?\s*', text, re.IGNORECASE)
    if not match:
        return None
    kind=(match.group(4) or 'all').lower()
    kind={'videos':'video','foto':'fotos','gifs':'gif'}.get(kind,kind)
    if match.group(1).lower()=='stop' and kind!='all':return None
    return match.group(1).lower(), int(match.group(2) or 0), int(match.group(3) or 0), kind


def media_kind(message):
    if getattr(message,'gif',None):return 'gif'
    if getattr(message,'photo',None):return 'fotos'
    if getattr(message,'video',None):return 'video'
    return 'other'


async def command(service, message):
    text = message.get('text', '').strip()
    if not re.match(r'^/(?:descargar|stop)(?:@|\s|$)', text):
        return False
    actor = message.get('from', {}).get('id')
    chat = message.get('chat', {})
    if chat.get('type') != 'private' or not authorized(actor):
        return True
    parsed = parse_command(text)
    if not parsed or not 0 < parsed[1] < 2**63 or not -(2**63) < parsed[2] < 0:
        await service.tell(actor, 'Uso: /descargar telegram_id group_id [video|fotos|gif]\nPausar: /stop telegram_id group_id')
        return True
    action, uid, source, media_filter = parsed
    pool = service.repo.pool
    if action == 'stop':
        row = await pool.fetchrow("UPDATE descarga_group_exports SET state='paused' WHERE bot_id=$1 AND uid=$2 AND source=$3 RETURNING cursor", service.bot_id, uid, source)
        await service.tell(actor, '⏸ Pausa solicitada. El archivo o álbum en curso terminará; /descargar continuará desde el avance guardado.' if row else 'No hay una descarga registrada para esa cuenta y grupo.')
        return True
    cooldown=await pool.fetchval('SELECT max(resume_at) FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2',service.bot_id,uid) or 0
    if cooldown>time.time():
        await service.tell(actor,f'Telegram exige esperar todavía {math.ceil(cooldown-time.time())} segundos. Después, vuelve a enviar /descargar {uid} {source}.')
        return True
    if not service.almacen or source == service.almacen:
        await service.tell(actor, 'Configura ALMACEN y elige un origen diferente al destino.')
        return True
    account = await service.repo.account(uid)
    if not account or not account['session']:
        await service.tell(actor, 'Esa cuenta no está conectada al bot.')
        return True
    # Do not silently replay a possibly accepted Telegram upload after a crash.
    row = await pool.fetchrow('SELECT * FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2 AND source=$3', service.bot_id, uid, source)
    if row and row['sending']:
        # Same cross-worker lock as _run: a paused upload may still be finishing.
        async with pool.acquire() as conn:
            slot=f'export:{service.bot_id}'
            locked=await conn.fetchval('SELECT pg_try_advisory_lock(751,hashtext($1))',slot)
            if not locked:
                await service.tell(actor,'Hay un envío en curso. Espera a que termine antes de reanudar.')
                return True
            try:
                await conn.execute("UPDATE descarga_group_exports SET sending=FALSE,state='paused' WHERE bot_id=$1 AND uid=$2 AND source=$3",service.bot_id,uid,source)
            finally:
                await conn.execute('SELECT pg_advisory_unlock(751,hashtext($1))',slot)
        # Keep publishing cache entries: only the uncertain content stays blocked.
        row=dict(row,sending=False,state='paused')
        await service.tell(actor,'Se retomará la revisión. El envío anterior incierto quedará pendiente sin repetirse; el resto puede continuar.')
    if row and row['state']=='running':
        await service.tell(actor,'La revisión del grupo ya está activa. Usa /stop para pausarla.')
        return True
    await pool.execute('''INSERT INTO descarga_group_exports(bot_id,uid,source,actor,generation,state,phase,media_filter)
        VALUES($1,$2,$3,$4,$5,'running','count',$6) ON CONFLICT(bot_id,uid,source)
        DO UPDATE SET actor=excluded.actor,generation=excluded.generation,state='running',phase='count',media_filter=excluded.media_filter,cursor=0,resume_at=0,scan=descarga_group_exports.scan+1 ''', service.bot_id, uid, source, actor, account['generation'], media_filter)
    await service.tell(actor, '▶️ Descarga registrada hacia ALMACEN. Primero contará fotos, videos y GIF del historial accesible. Después descargará el tipo solicitado, omitirá los archivos ya guardados y avisará aquí al terminar.')
    return True


async def checked(service, job):
    if getattr(service,'role','worker')=='worker' and await service.repo.setting(f'dedicated_export:{service.bot_id}')=='enabled':return False
    if not authorized(job['actor']):
        raise ValueError('El administrador ya no está autorizado.')
    row = await service.repo.pool.fetchrow('SELECT * FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2 AND source=$3', job['bot_id'], job['uid'], job['source'])
    if not row or row['state'] != 'running' or row.get('scan',0)!=job.get('scan',0):
        return False
    if row.get('resume_at',0)>time.time():return False
    account = await service.repo.account(job['uid'])
    if not account or not account['session'] or account['generation'] != job['generation']:
        raise ValueError('La sesión fue desconectada. Vuelve a conectar la cuenta.')
    await ensure_allowed(service, job['uid'], job['source'])
    if (await service.repo.policy(service.bot_id, job['uid'], job['source']))['locked']:
        raise ValueError('El usuario o el grupo tiene /lock; no se permite almacenar.')
    stored = await service.repo.setting('runtime_config')
    if not stored or service.repo.decode(stored.encode()) != service.runtime_config:
        raise ValueError('Cambió la configuración. Espera al reinicio del servicio.')
    return True


async def save_cursor(service, job, cursor, count=0):
    await service.repo.pool.execute('''UPDATE descarga_group_exports SET cursor=$4,total=total+$5,sending=FALSE
        WHERE bot_id=$1 AND uid=$2 AND source=$3 AND scan=$6''', job['bot_id'], job['uid'], job['source'], cursor, count,job.get('scan',0))


async def users_busy(service):
    return bool(await service.repo.pool.fetchval("SELECT 1 FROM descarga_jobs WHERE bot_id=$1 AND state IN ('queued','preparing','sending') LIMIT 1",service.bot_id))


async def archive_items(service, job, client, key, items, name):
    uid, source = job['uid'], job['source']
    from .export_inventory import excluded,stored,remember
    if await excluded(service,items):return 0
    if await stored(service,source,items):return 0
    size=sum(m.file.size or 0 for m in items)
    if size>MAX_ALBUM or any((m.file.size or 0)>MAX_FILE for m in items):
        raise ValueError('El archivo supera 500 MB o el álbum supera 1 GB.')
    identity=cache_key(service.bot_id,service.almacen,key,items)
    async def known():
        cached=await service.repo.media_cache(identity)
        if not cached:
            from .export_inventory import files
            for kind,fid in files(items):
                if await service.repo.pool.fetchval('SELECT 1 FROM descarga_export_uncertain WHERE bot_id=$1 AND almacen=$2 AND source=$3 AND kind=$4 AND file_id=$5',service.bot_id,service.almacen,source,kind,fid):
                    raise ValueError('Archivo pendiente de revisión de un envío anterior; no se repetirá con otro filtro.')
            return False
        if cached['state']!='ready':raise ValueError('El contenido necesita revisión; no se repetirá una subida incierta.')
        await remember(service,source,items,service.repo.decode(cached['payload'])['ids'])
        return True
    # Never hold the shared media lock while preparing a background download.
    async with service.repo.media_lock(identity):
        if await known():return 0
    if await users_busy(service):return None
    attribution=await archive_attribution(service,uid,name)
    await append_archive_source(attribution,items[0],source)
    with tempfile.TemporaryDirectory(prefix='descargas-group-') as directory:
        async def prepare(path,item):return await prepare_media_for_bot(service.bot,client,path,item)
        media=await prepare_album(client,items,directory,prepare,MAX_FILE,MAX_ALBUM)
        if not await checked(service,job) or await users_busy(service):return None
        await service.repo.pace('archive-group',3.1*len(items))
        if not await checked(service,job) or await users_busy(service):return None
        if await excluded(service,items):return 0
        try:target=await service.bot.get_input_entity(service.almacen)
        except ValueError:
            target_id,target_type=utils.resolve_id(service.almacen)
            if target_type is types.PeerChannel:
                chats=await service.bot(functions.channels.GetChannelsRequest([types.InputChannel(target_id,0)]))
                target=utils.get_input_peer(chats.chats[0])
            elif target_type is types.PeerChat:target=types.InputPeerChat(target_id)
            else:raise ValueError('ALMACEN debe ser un grupo o canal.')
        async with service.repo.media_lock(identity):
            if await known():return 0
            if await users_busy(service) or not await checked(service,job):return None
            await service.repo.pace('bot-send',0.06*len(items))
            claimed=await service.repo.pool.fetchval("UPDATE descarga_group_exports SET sending=TRUE WHERE bot_id=$1 AND uid=$2 AND source=$3 AND scan=$4 AND state='running' RETURNING 1",service.bot_id,uid,source,job.get('scan',0))
            if not claimed:return None
            from .export_inventory import files
            await service.repo.pool.executemany('INSERT INTO descarga_export_uncertain VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING',[(service.bot_id,service.almacen,source,kind,fid,identity) for kind,fid in files(items)])
            await service.repo.media_publishing(identity)
            try:
                result=await service.bot.send_file(target,media if len(items)>1 else media[0],
                    caption=[m.message or '' for m in items] if len(items)>1 else items[0].message or '',
                    formatting_entities=[m.entities or [] for m in items] if len(items)>1 else items[0].entities or [],parse_mode=None)
            except errors.FloodWaitError as exc:
                request=getattr(exc,'request',None)
                while getattr(request,'query',None) is not None:request=request.query
                # <=10 items means a single send RPC. Telegram explicitly rejected it;
                # uploads themselves do not publish messages. Never clear timeouts.
                if len(items)<=10 and isinstance(request,(functions.messages.SendMediaRequest,
                    functions.messages.SendMultiMediaRequest,functions.messages.UploadMediaRequest)):
                    await service.repo.pool.execute("DELETE FROM descarga_media_cache WHERE key=$1 AND state='publishing'",identity)
                    await service.repo.pool.execute('DELETE FROM descarga_export_uncertain WHERE media_key=$1',identity)
                    await service.repo.pool.execute('UPDATE descarga_group_exports SET sending=FALSE WHERE bot_id=$1 AND uid=$2 AND source=$3 AND scan=$4',service.bot_id,uid,source,job.get('scan',0))
                raise
            delivered=result if isinstance(result,list) else [result]
            if len(delivered)!=len(items):raise ValueError('El álbum no se guardó completo; requiere revisión.')
            ids=[m.id for m in delivered]
            await service.repo.register_origins(service.bot_id,service.almacen,ids,uid,source)
            from .file_cache import payload as file_payload
            reusable=file_payload(service,key,items,delivered=delivered)
            await service.repo.media_ready(identity,{'ids':ids},file_cache=reusable)
            await remember(service,source,items,ids)
            await service.repo.pool.execute('DELETE FROM descarga_export_uncertain WHERE media_key=$1',identity)
            await apply_archive_caption(service,ids[0],attribution,delivered[0])
            return len(items)


async def step(service, job):
    """One history page per lock, releasing the user's session between pages."""
    if not await checked(service, job):
        return
    # Convert an in-progress metadata-only scan from the previous version once.
    # Start its single pass at zero, retaining inventory/totals and fencing stale
    # workers with the scan generation. Interactive requests are untouched.
    if job.get('phase')=='index':
        await service.repo.pool.execute("UPDATE descarga_group_exports SET phase='download',cursor=0,scan=scan+1 WHERE bot_id=$1 AND uid=$2 AND source=$3 AND scan=$4 AND state='running' AND NOT sending AND phase='index'",job['bot_id'],job['uid'],job['source'],job.get('scan',0))
        return
    service.export_uid=job['uid']
    service.export_generation=job['generation']
    async with service.repo.account_lock(job['uid'], wait=False):
        if not await checked(service, job):
            return
        session = await service.get_session(job['uid'])
        client = service.client(session)
        client.flood_sleep_threshold=0 # All account FloodWaits reach the durable manual cooldown.
        try:
            service.export_operation='connect'
            await client.connect()
            service.export_operation='authorization'
            if not await client.is_user_authorized():
                raise ValueError('La sesión fue revocada. Conecta de nuevo la cuenta.')
            service.export_operation='resolve_source'
            entity = await service.resolve(client, job['source'])
            if not isinstance(entity, (types.Channel, types.Chat)) or utils.get_peer_id(entity) != job['source']:
                raise ValueError('El origen debe ser un grupo o canal accesible por esa cuenta.')
            require_entity(entity, job['uid'])
            service.export_operation='get_account'
            me = await client.get_me()
            name = ' '.join(filter(None, [me.first_name, me.last_name])) or 'Usuario'
            if job.get('phase')=='count':
                counts=[]
                for filter_type in (types.InputMessagesFilterPhotos,types.InputMessagesFilterVideo,types.InputMessagesFilterGif):
                    service.export_operation='count_media'
                    result=await client.get_messages(entity,limit=0,filter=filter_type())
                    counts.append(result.total)
                await service.tell(job['actor'],f'📊 {entity.title}\nFotos: {counts[0]}\nVideos: {counts[1]}\nGIF: {counts[2]}\nFiltro: {job.get("media_filter","all")}\nCantidades del historial visible; pueden cambiar. Comienza la descarga.')
                await service.repo.pool.execute("UPDATE descarga_group_exports SET phase='download' WHERE bot_id=$1 AND uid=$2 AND source=$3 AND scan=$4 AND state='running'",job['bot_id'],job['uid'],job['source'],job.get('scan',0))
                return
            cursor = job['cursor']
            seen = set()
            service.export_operation='history'
            messages = [m async for m in client.iter_messages(entity, min_id=cursor, reverse=True, limit=30)]
            for message in messages:
                if message.id <= cursor:
                    continue
                if service.stopping or await users_busy(service) or not await checked(service, job):
                    return
                try:
                    count=0
                    if message.photo or message.document or getattr(message, 'paid_stars', None) or type(message.media).__name__ == 'MessageMediaPaidMedia':
                        known_key=(job['source'],getattr(message,'grouped_id',None) or message.id)
                        if known_key in seen:
                            cursor=message.id
                            await save_cursor(service,job,cursor)
                            from .export_inventory import resolve_pending
                            await resolve_pending(service,job,message.id)
                            continue
                        service.export_operation='read_album'
                        await service.repo.pace(f'export-source-read:{job["uid"]}',3.1)
                        if await users_busy(service) or not await checked(service,job):return
                        if str(job['source']).startswith('-100'):
                            link = f'https://t.me/c/{str(job["source"])[4:]}/{message.id}'
                            key,items=await service.source_items(client,link)
                        elif getattr(entity,'username',None):
                            key,items=await service.source_items(client,f'https://t.me/{entity.username}/{message.id}')
                        else:
                            from .service import album
                            from .paid_media import purchased_items
                            paid=purchased_items(message)
                            items=paid if paid is not None else await album(client,entity,message)
                            key=(job['source'],message.grouped_id or message.id)
                        # Check source file inventory and this item's existing
                        # cache, then immediately archive only what is missing.
                        service.export_operation='archive'
                        selected=items
                        if job.get('media_filter','all')!='all':
                            # A subset must never bypass an uncertain full-album upload.
                            full_cache=await service.repo.media_cache(cache_key(service.bot_id,service.almacen,key,items))
                            if full_cache:
                                if full_cache['state']!='ready':raise ValueError('Álbum pendiente de revisión; no se repetirá.')
                                from .export_inventory import remember
                                await remember(service,job['source'],items,service.repo.decode(full_cache['payload'])['ids'])
                            from .export_inventory import excluded
                            selected=[] if await excluded(service,items) else [item for item in items if media_kind(item)==job['media_filter']]
                            from .export_inventory import stored
                            selected=[item for item in selected if not await stored(service,job['source'],[item])]
                        count = 0 if key in seen or not selected else await archive_items(service, job, client, key, selected, name)
                        if count is None:
                            return
                        seen.add(key)
                        cursor = message.id
                        await save_cursor(service, job, cursor, count)
                    else:
                        cursor = message.id
                        await save_cursor(service, job, cursor)
                    from .export_inventory import resolve_pending
                    await resolve_pending(service,job,message.id)
                    if count:return # Release account lock after one delivered album/file.
                except asyncio.CancelledError:raise
                except Exception as exc:
                    # Sending may already have succeeded: never advance past an ambiguous upload.
                    uncertain=await service.repo.pool.fetchval('SELECT sending FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2 AND source=$3',job['bot_id'],job['uid'],job['source'])
                    if uncertain or isinstance(exc,errors.FloodWaitError):raise
                    if not await checked(service,job):return
                    await service.repo.pool.execute('''INSERT INTO descarga_export_pending VALUES($1,$2,$3,$4,$5)
                        ON CONFLICT(bot_id,uid,source,message_id) DO UPDATE SET error=excluded.error''',job['bot_id'],job['uid'],job['source'],message.id,type(exc).__name__)
                    cursor=message.id
                    await save_cursor(service,job,cursor)
            if len(messages) < 30:
                # Uploads can take minutes: include messages arriving after this page was fetched.
                service.export_operation='latest_message'
                latest=await client.get_messages(entity,limit=1)
                if latest and latest[0].id>cursor:return
                row = await service.repo.pool.fetchrow("UPDATE descarga_group_exports SET state='done' WHERE bot_id=$1 AND uid=$2 AND source=$3 AND state='running' AND scan=$4 RETURNING total", service.bot_id, job['uid'], job['source'],job.get('scan',0))
                if row:
                    pending=await service.repo.pool.fetchval('SELECT count(*) FROM descarga_export_pending WHERE bot_id=$1 AND uid=$2 AND source=$3',job['bot_id'],job['uid'],job['source']) or 0
                    status='⏸ Revisión terminada con archivos pendientes' if pending else '✅ Descarga completada'
                    try:await service.tell(job['actor'], f'{status} de {getattr(entity,"title",job["source"])} ({job["source"]}).\nCuenta: {job["uid"]}\nArchivos nuevos guardados en ALMACEN: {row["total"]}.\nPendientes con error: {pending}. Una nueva orden /descargar revisará también los mensajes anteriores.')
                    except Exception:pass
        finally:
            await client.disconnect()


async def _run(service):
    while not service.stopping:
        try:
            jobs = await service.repo.pool.fetch("SELECT * FROM descarga_group_exports WHERE bot_id=$1 AND state='running' AND resume_at<=$2 ORDER BY uid,source LIMIT 100", service.bot_id,time.time())
            for job in jobs:
                if service.stopping:return
                # Cross-worker serialization; separate from interactive job/account locks.
                async with service.repo.pool.acquire() as conn:
                    slot = f'export:{job["bot_id"]}'
                    locked = await conn.fetchval('SELECT pg_try_advisory_lock(751,hashtext($1))', slot)
                    if not locked:continue
                    marker = SOURCE.set(job['source'])
                    try:
                        fresh = await conn.fetchrow('SELECT * FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2 AND source=$3', service.bot_id, job['uid'], job['source'])
                        if not fresh or fresh['state'] != 'running':continue
                        if fresh['sending']:
                            await conn.execute('UPDATE descarga_group_exports SET sending=FALSE WHERE bot_id=$1 AND uid=$2 AND source=$3',service.bot_id,job['uid'],job['source'])
                            fresh=dict(fresh,sending=False)
                        # Interactive requests always take priority for this account.
                        busy = await users_busy(service)
                        if not busy:
                            async with asyncio.timeout(3600):
                                await step(service, fresh)
                    except asyncio.CancelledError:raise
                    except Exception as exc:
                        if isinstance(exc,errors.FloodWaitError):
                            await defer_flood(service,fresh,exc)
                            continue
                        # A busy account is temporary, not a failed export.
                        if isinstance(exc,ValueError) and str(exc).startswith('Espera a que termine'):
                            continue
                        await conn.execute("UPDATE descarga_group_exports SET state='paused' WHERE bot_id=$1 AND uid=$2 AND source=$3", service.bot_id, job['uid'], job['source'])
                        LOG.warning('Group export paused (%s)', type(exc).__name__)
                        try:
                            from .service import error_message
                            detail = str(exc) if isinstance(exc,ValueError) else error_message(exc)
                            await service.tell(job['actor'], f'⏸ Descarga pausada. Cuenta {job["uid"]}, origen {job["source"]}.\n{detail}\nEl avance quedó guardado.')
                        except Exception:pass
                    finally:
                        SOURCE.reset(marker)
                        await conn.execute('SELECT pg_advisory_unlock(751,hashtext($1))', slot)
        except asyncio.CancelledError:raise
        except Exception as exc:
            LOG.warning('Group export loop delayed (%s)', type(exc).__name__)
        await asyncio.sleep(3)


async def list_groups(service, actor, argument):
    from .account_directory import destinations
    await destinations(service,actor,argument)


class ExportContext:
    """Keep export lock connections out of the interactive workers' pool."""
    def __init__(self, service, repo):
        self.service=service
        self.repo=repo
        self._source_entities={}

    def __getattr__(self, name):
        return getattr(self.service,name)

    async def resolve(self, client, peer):
        from .service import Service
        # A new client is opened for every page/album. Cache by authorized
        # account generation, not client identity, to avoid listing all dialogs
        # again after every disconnect. Never share access hashes across accounts.
        key=(self.export_uid,self.export_generation,peer)
        entity=self._source_entities.get(key)
        if entity is None:
            entity=await Service.resolve(self,client,peer)
            require_entity(entity,self.export_uid)
            if len(self._source_entities)>=128:
                self._source_entities.pop(next(iter(self._source_entities)))
            self._source_entities[key]=entity
        # Membership/content access is still verified by Telegram on every message fetch.
        require_entity(entity,self.export_uid)
        return entity

    async def source_items(self, client, link):
        from .service import Service
        key,items=await Service.source_items(self,client,link)
        await ensure_allowed(self,self.export_uid,key[0])
        return key,items

    async def get_session(self, uid):
        row=await self.repo.account(uid)
        return self.cipher.decrypt(bytes(row['session'])).decode() if row and row['session'] else ''


async def run(service):
    import asyncpg
    from .pg_store import Repository
    repo=Repository(service.repo.url,service.cipher,pool_size=4)
    try:
        repo.pool=await asyncpg.create_pool(repo.url,min_size=0,max_size=4,command_timeout=20,
            max_inactive_connection_lifetime=60)
        await _run(ExportContext(service,repo))
    finally:
        await repo.close()


async def defer_flood(service,job,exc):
    request=getattr(exc,'request',None)
    chain=[]
    for _ in range(8):
        if request is None:break
        chain.append(type(request).__name__)
        request=getattr(request,'query',None)
    LOG.warning('Group export FloodWait uid=%s source=%s phase=%s request=%s seconds=%s',
        job['uid'],job['source'],getattr(service,'export_operation','unknown'),
        '>'.join(chain) or 'unknown',exc.seconds)
    sending=await service.repo.pool.fetchval('SELECT sending FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2 AND source=$3',job['bot_id'],job['uid'],job['source'])
    seconds=max(60,int(exc.seconds)+10)
    await service.repo.pool.execute("UPDATE descarga_group_exports SET resume_at=GREATEST(resume_at,$3),state=CASE WHEN state='running' THEN 'paused' ELSE state END WHERE bot_id=$1 AND uid=$2",service.bot_id,job['uid'],time.time()+seconds)
    note=' El envío incierto quedará pendiente de revisión sin repetirse; los demás podrán continuar.' if sending else ''
    try:await service.tell(job['actor'],f'⏳ Telegram pide {exc.seconds} segundos; aplicamos una pausa prudente de {seconds} segundos para la cuenta {job["uid"]}. La reactivación será manual: cuando termine la espera, usa /descargar {job["uid"]} {job["source"]}'+(' '+job['media_filter'] if job.get('media_filter','all')!='all' else '')+f'. El avance quedó guardado.{note}')
    except Exception:pass
