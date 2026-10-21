"""Administrative, resumable history export; independent from requested DM jobs."""
import asyncio
import logging
import re
import tempfile
from telethon import types, utils, functions, errors
from .moderation import authorized, ensure_allowed, SOURCE
from .protected import require_entity
from .service import MAX_FILE, MAX_ALBUM, archive_attribution, apply_archive_caption, prepare_media_for_bot
from .transfer import prepare_album
from .media_cache import cache_key, append_archive_source

LOG = logging.getLogger(__name__)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS descarga_group_exports(
 bot_id BIGINT NOT NULL, uid BIGINT NOT NULL, source BIGINT NOT NULL,
 actor BIGINT NOT NULL, generation BIGINT NOT NULL, cursor BIGINT NOT NULL DEFAULT 0,
 total BIGINT NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'paused',
 sending BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(bot_id,uid,source));
ALTER TABLE descarga_group_exports ADD COLUMN IF NOT EXISTS scan BIGINT NOT NULL DEFAULT 0;
'''


def parse_command(text):
    match = re.fullmatch(r'/(descargar|stop)(?:@[A-Za-z0-9_]+)?(?:\s+(\d+)\s+(-\d+))?\s*', text)
    if not match:
        return None
    return match.group(1), int(match.group(2) or 0), int(match.group(3) or 0)


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
        await service.tell(actor, 'Uso: /descargar telegram_id group_id\nPausar: /stop telegram_id group_id')
        return True
    action, uid, source = parsed
    pool = service.repo.pool
    if action == 'stop':
        row = await pool.fetchrow("UPDATE descarga_group_exports SET state='paused' WHERE bot_id=$1 AND uid=$2 AND source=$3 RETURNING cursor", service.bot_id, uid, source)
        await service.tell(actor, '⏸ Pausa solicitada. El archivo o álbum en curso terminará; /descargar continuará desde el avance guardado.' if row else 'No hay una descarga registrada para esa cuenta y grupo.')
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
        await service.tell(actor, 'Hay un envío en curso o pendiente de revisión. No se reiniciará para evitar duplicados.')
        return True
    if row and row['state']=='running':
        await service.tell(actor,'La revisión del grupo ya está activa. Usa /stop para pausarla.')
        return True
    await pool.execute('''INSERT INTO descarga_group_exports(bot_id,uid,source,actor,generation,state)
        VALUES($1,$2,$3,$4,$5,'running') ON CONFLICT(bot_id,uid,source)
        DO UPDATE SET actor=excluded.actor,generation=excluded.generation,state='running',cursor=0,scan=descarga_group_exports.scan+1 ''', service.bot_id, uid, source, actor, account['generation'])
    await service.tell(actor, '▶️ Descarga registrada hacia ALMACEN. Revisará todo el historial accesible, omitirá los archivos ya guardados y avisará aquí al terminar.')
    return True


async def checked(service, job):
    if not authorized(job['actor']):
        raise ValueError('El administrador ya no está autorizado.')
    row = await service.repo.pool.fetchrow('SELECT * FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2 AND source=$3', job['bot_id'], job['uid'], job['source'])
    if not row or row['state'] != 'running' or row.get('scan',0)!=job.get('scan',0):
        return False
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


async def archive_items(service, job, client, key, items, name):
    uid, source = job['uid'], job['source']
    from .export_inventory import excluded,stored,remember
    if await excluded(service,items):return 0
    if await stored(service,source,items):return 0
    size = sum(m.file.size or 0 for m in items)
    if size > MAX_ALBUM or any((m.file.size or 0) > MAX_FILE for m in items):
        raise ValueError('El archivo supera 500 MB o el álbum supera 1 GB.')
    identity = cache_key(service.bot_id, service.almacen, key, items)
    async with service.repo.media_lock(identity):
        cached = await service.repo.media_cache(identity)
        if cached:
            if cached['state'] != 'ready':
                raise ValueError('El contenido necesita revisión; no se repetirá una subida incierta.')
            saved=service.repo.decode(cached['payload'])
            await remember(service,source,items,saved['ids'])
            return 0
        attribution = await archive_attribution(service, uid, name)
        await append_archive_source(attribution, items[0], source)
        with tempfile.TemporaryDirectory(prefix='descargas-group-') as directory:
            async def prepare(path, item):
                return await prepare_media_for_bot(service.bot, client, path, item)
            media = await prepare_album(client, items, directory, prepare, MAX_FILE, MAX_ALBUM)
            if not await checked(service, job):
                return None
            await service.repo.pace('archive-group', 3.1 * len(items))
            if not await checked(service, job):
                return None
            if await excluded(service,items):return 0
            try:
                target = await service.bot.get_input_entity(service.almacen)
            except ValueError:
                target_id, target_type = utils.resolve_id(service.almacen)
                if target_type is types.PeerChannel:
                    # A bot already in ALMACEN can resolve its channel without
                    # relying on a previous worker's in-memory entity cache.
                    chats = await service.bot(functions.channels.GetChannelsRequest([types.InputChannel(target_id, 0)]))
                    target = utils.get_input_peer(chats.chats[0])
                elif target_type is types.PeerChat:
                    target = types.InputPeerChat(target_id)
                else:
                    raise ValueError('ALMACEN debe ser un grupo o canal.')
            await service.repo.pace('bot-send', 0.06 * len(items))
            # Durable uncertainty marker BEFORE Telegram receives any send.
            claimed=await service.repo.pool.fetchval("UPDATE descarga_group_exports SET sending=TRUE WHERE bot_id=$1 AND uid=$2 AND source=$3 AND scan=$4 AND state='running' RETURNING 1",service.bot_id,uid,source,job.get('scan',0))
            if not claimed:return None
            await service.repo.media_publishing(identity)
            result = await service.bot.send_file(target, media if len(items) > 1 else media[0],
                caption=[m.message or '' for m in items] if len(items) > 1 else items[0].message or '',
                formatting_entities=[m.entities or [] for m in items] if len(items) > 1 else items[0].entities or [], parse_mode=None)
            delivered = result if isinstance(result, list) else [result]
            if len(delivered) != len(items):
                raise ValueError('El álbum no se guardó completo; requiere revisión.')
            ids = [m.id for m in delivered]
            await service.repo.register_origins(service.bot_id, service.almacen, ids, uid, source)
            await apply_archive_caption(service, ids[0], attribution, delivered[0])
            # Existing cache lets subsequent link requests reuse these ALMACEN files.
            await service.repo.media_ready(identity, {'ids': ids})
            await remember(service,source,items,ids)
            return len(items)


async def step(service, job):
    """One history page per lock, releasing the user's session between pages."""
    if not await checked(service, job):
        return
    service.export_uid=job['uid']
    async with service.repo.account_lock(job['uid'], wait=False):
        if not await checked(service, job):
            return
        session = await service.get_session(job['uid'])
        client = service.client(session)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError('La sesión fue revocada. Conecta de nuevo la cuenta.')
            entity = await service.resolve(client, job['source'])
            if not isinstance(entity, (types.Channel, types.Chat)) or utils.get_peer_id(entity) != job['source']:
                raise ValueError('El origen debe ser un grupo o canal accesible por esa cuenta.')
            require_entity(entity, job['uid'])
            me = await client.get_me()
            name = ' '.join(filter(None, [me.first_name, me.last_name])) or 'Usuario'
            cursor = job['cursor']
            seen = set()
            messages = [m async for m in client.iter_messages(entity, min_id=cursor, reverse=True, limit=30)]
            for message in messages:
                if message.id <= cursor:
                    continue
                if service.stopping or not await checked(service, job):
                    return
                try:
                    if message.photo or message.document or getattr(message, 'paid_stars', None) or type(message.media).__name__ == 'MessageMediaPaidMedia':
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
                        count = 0 if key in seen else await archive_items(service, job, client, key, items, name)
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
            jobs = await service.repo.pool.fetch("SELECT * FROM descarga_group_exports WHERE bot_id=$1 AND state='running' ORDER BY uid,source LIMIT 100", service.bot_id)
            for job in jobs:
                if service.stopping:return
                # Cross-worker serialization; separate from interactive job/account locks.
                async with service.repo.pool.acquire() as conn:
                    slot = f'{job["bot_id"]}:{job["uid"]}:{job["source"]}'
                    locked = await conn.fetchval('SELECT pg_try_advisory_lock(751,hashtext($1))', slot)
                    if not locked:continue
                    marker = SOURCE.set(job['source'])
                    try:
                        fresh = await conn.fetchrow('SELECT * FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2 AND source=$3', service.bot_id, job['uid'], job['source'])
                        if not fresh or fresh['state'] != 'running':continue
                        if fresh['sending']:
                            raise ValueError('Envío pendiente de revisión tras una interrupción. No se repetirá automáticamente.')
                        # Interactive requests always take priority for this account.
                        busy = await conn.fetchval("SELECT 1 FROM descarga_jobs WHERE uid=$1 AND state IN ('queued','preparing','sending')", job['uid'])
                        if not busy:
                            async with asyncio.timeout(3600):
                                await step(service, fresh)
                    except asyncio.CancelledError:raise
                    except Exception as exc:
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
    """List live dialogs using the chosen account; never a global/statistics list."""
    if not authorized(actor):return
    if not re.fullmatch(r'[1-9][0-9]{0,15}',argument.strip()):
        await service.tell(actor,'Uso: /grupos telegram_id')
        return
    uid=int(argument.strip())
    client=None
    count=0
    ids=[]
    from .account_chat import remember_groups
    try:
        async with service.repo.account_lock(uid,wait=False):
            session=await service.get_session(uid)
            if not session:
                await service.tell(actor,'Esa cuenta no está conectada al bot.')
                return
            client=service.client(session)
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError('La sesión fue revocada. Conecta de nuevo la cuenta.')
            me=await client.get_me()
            if me.id!=uid:raise ValueError('La cuenta conectada no coincide con el Telegram ID.')
            await remember_groups(service,actor,uid,[])
            chunk=f'📋 GRUPOS Y CANALES · Cuenta {uid}\n\n'
            async with asyncio.timeout(180):
                async for dialog in client.iter_dialogs():
                    if not authorized(actor):return
                    entity=dialog.entity
                    if not isinstance(entity,(types.Chat,types.Channel)):
                        continue
                    if any(getattr(entity,flag,False) for flag in ('left','kicked','deactivated')) or getattr(entity,'migrated_to',None):
                        continue
                    count+=1
                    source=utils.get_peer_id(entity)
                    ids.append(source)
                    title=' '.join((getattr(entity,'title',None) or 'Sin nombre').split())[:128]
                    kind='Canal' if getattr(entity,'broadcast',False) else 'Grupo'
                    line=f'{count}. {title} · {kind}\nID: {source}\n/descargar {uid} {source}\n\n'
                    if len((chunk+line).encode('utf-16-le'))//2>3500:
                        await remember_groups(service,actor,uid,ids)
                        await service.tell(actor,chunk)
                        chunk=''
                    chunk+=line
            await remember_groups(service,actor,uid,ids)
            await service.tell(actor,chunk+f'✅ Total: {count} grupos y canales accesibles.\nPermisos: escribe {uid},número de lista (ejemplo: {uid},1). La lista dura 30 minutos.')
    except asyncio.CancelledError:raise
    except Exception as exc:
        from .service import error_message
        detail=str(exc) if isinstance(exc,ValueError) else error_message(exc)
        await service.tell(actor,f'No se pudo completar la lista de la cuenta {uid}. {detail}')
    finally:
        if client:await client.disconnect()


class ExportContext:
    """Keep export lock connections out of the interactive workers' pool."""
    def __init__(self, service, repo):
        self.service=service
        self.repo=repo

    def __getattr__(self, name):
        return getattr(self.service,name)

    async def resolve(self, client, peer):
        from .service import Service
        entity=await Service.resolve(self,client,peer)
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
