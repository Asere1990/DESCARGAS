"""Private on-demand preparation of Telegram Stars purchases for iPhone downloads.

A dedicated, verified Telegram connection streams ONE requested file at a time
to a bounded temporary disk file. Saved sessions, download workers, job queues,
and account locks are never modified. Prepared files expire automatically.
"""
import asyncio
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from aiohttp import web
from telethon import errors, types, utils

from .account_chat import connected_parallel
from .creator_access import ACTOR, require_account, require_source
from .moderation import authorized
from .paid_media import purchased_items, reveal_purchased

LOG = logging.getLogger(__name__)
MAX_BYTES = 4 * 1024 * 1024 * 1024  # Telegram supports media up to 4 GiB; disk guard still applies
MIN_FREE = 128 * 1024 * 1024
READY_TTL = 20 * 60
CAP_TTL = 5 * 60
PENDING_TTL = 120 * 60  # allow a large, slow file to complete
ERROR_TTL = 3 * 60
MAX_JOBS = 12


def safe_filename(info, message_id):
    mime = info.get('mime') or 'application/octet-stream'
    suffix = {
        'video/mp4': '.mp4', 'video/quicktime': '.mov',
        'video/x-m4v': '.m4v', 'video/webm': '.webm',
        'image/jpeg': '.jpg', 'image/png': '.png',
        'image/webp': '.webp', 'image/gif': '.gif',
    }.get(mime, '')
    original = str(info.get('name') or '').strip()
    name = re.sub(r'[^A-Za-z0-9._-]', '_', original)[:120].strip('.')
    if not name or name.lower() in ('archivo', 'video', 'foto'):
        name = 'telegram-' + str(message_id)
    if suffix and not name.lower().endswith(suffix):
        if not re.search(r'\.[A-Za-z0-9]{2,6}\Z', name):
            name += suffix
    return name or ('telegram-' + str(message_id) + suffix)


class PreparedDownloads:
    def __init__(self, panel):
        self.p = panel
        self.s = panel.s
        self.jobs = {}
        self.slot = asyncio.Semaphore(1)
        self.directory = None
        self.sweeper = None

    async def start(self):
        if self.sweeper is None:
            self.sweeper = asyncio.create_task(self._sweep_loop())

    async def close(self):
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
            self.sweeper = None
        tasks = [job['task'] for job in self.jobs.values()
                 if job.get('task') and not job['task'].done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None
        self.jobs.clear()

    async def _sweep_loop(self):
        while True:
            await asyncio.sleep(45)
            self._prune()

    def _prune(self):
        now = time.monotonic()
        for key, job in list(self.jobs.items()):
            if job['state'] in ('queued', 'fetching') and now - job['created'] < PENDING_TTL:
                continue
            if job['state'] in ('queued', 'fetching'):
                task = job.get('task')
                if task and not task.done():
                    task.cancel()
                continue
            if now > job['expires']:
                self._remove(key)

    def _remove(self, key):
        job = self.jobs.pop(key, None)
        if job and job.get('file'):
            try:
                Path(job['file']).unlink(missing_ok=True)
            except OSError:
                LOG.warning("Unable to delete expired temporary download")

    def _owned(self, actor, version, id):
        job = self.jobs.get(id)
        if (not job or not secrets.compare_digest(str(job['actor']), str(actor))
                or job['version'] != version):
            raise ValueError('Esta preparación ya no está disponible. Solicítala de nuevo.')
        return job

    async def _check(self, job):
        actor, uid = job['actor'], job['uid']
        if not authorized(actor):
            raise ValueError('Acceso administrativo revocado.')
        await require_account(self.s, uid, actor)
        row = await self.s.repo.account(uid)
        if not row or not row['session'] or row['generation'] != job['generation']:
            raise ValueError('La cuenta vinculada cambió. Abre nuevamente el panel.')
        version = await self.s.repo.setting(f'web-panel-version:{self.s.bot_id}:{actor}') or '0'
        if version != job['version']:
            raise ValueError('El acceso administrativo cambió.')
        await require_source(self.s, uid, job['source'], actor=actor)

    async def begin(self, actor, version, payload):
        from .web_panel import integer, source_id
        if not isinstance(payload, dict):
            raise ValueError('Solicitud de descarga incorrecta.')
        uid = integer(payload.get('account'))
        peer = payload.get('peer')
        details = self.p.unpack(peer, 'peer')
        row = await self.s.repo.account(uid)
        if not row or not row['session']:
            raise ValueError('La cuenta vinculada no está disponible.')
        generation = row['generation']
        if any(details.get(key) != value for key, value in
               (('actor', actor), ('uid', uid), ('generation', generation))):
            raise ValueError('La conversación cambió. Vuelve a abrirla.')
        source = source_id(details)
        await require_account(self.s, uid, actor)
        await require_source(self.s, uid, source, actor=actor)

        purchase = payload.get('purchase')
        if purchase is not None:
            self.p.unpack(purchase, 'purchase')
            index = integer(payload.get('index', 0), 0)
            if index >= 100:
                raise ValueError('Índice de archivo no válido.')
            message_id = None
        else:
            message_id = integer(payload.get('message'))
            index = integer(payload.get('paid_index', 0), 0)
            if index >= 100:
                raise ValueError('Índice de archivo no válido.')
        identity = (actor, uid, generation, source, purchase or message_id, index)
        self._prune()
        for job in self.jobs.values():
            if job['identity'] == identity and job['state'] in ('queued', 'fetching', 'ready'):
                return self._status(job)
            if job['uid'] == uid and job['state'] in ('queued', 'fetching'):
                raise ValueError('Ya hay un archivo preparándose para esta cuenta. Espera a que termine.')
        if len(self.jobs) >= MAX_JOBS:
            raise ValueError('Hay demasiados archivos temporales. Inténtalo después.')

        job = dict(
            id=secrets.token_urlsafe(24), identity=identity, actor=actor,
            version=version, uid=uid, generation=generation, source=source,
            peer=peer, purchase=purchase, index=index, message=message_id,
            state='queued', created=time.monotonic(), expires=time.monotonic()+PENDING_TTL,
            received=0, total=0, name='', mime='', file=None, error='',
            capability=None, capability_expires=0)
        self.jobs[job['id']] = job
        job['task'] = asyncio.create_task(self._prepare(job))
        return self._status(job)

    def _status(self, job):
        now = time.monotonic()
        state = job['state']
        progress = (100 if state == 'ready' else
                    min(99, int(job['received'] * 100 / job['total']))
                    if job['total'] else 0)
        data = dict(job=job['id'], state=state,
                    progress=progress, received=job['received'],
                    total=job['total'], name=job['name'], error=job['error'])
        if state == 'ready':
            if not job['capability'] or job['capability_expires'] - now < 60:
                job['capability'] = secrets.token_urlsafe(32)
                job['capability_expires'] = now + CAP_TTL
            data['file_token'] = job['capability']
        return data

    async def status(self, actor, version, id):
        self._prune()
        job = self._owned(actor, version, id)
        await self._check(job)
        return self._status(job)

    async def _prepare(self, job):
        actor = job['actor']
        marker = ACTOR.set(actor)
        part = None
        try:
            async with self.slot, asyncio.timeout(90 * 60):
                job['state'] = 'fetching'
                await self._check(job)
                async with connected_parallel(self.s, job['uid']) as client:
                    client.flood_sleep_threshold = 0
                    entity = await self.p.target(actor, job['uid'], job['generation'],
                                                 client, job['peer'])
                    if utils.get_peer_id(entity) != job['source']:
                        raise ValueError('La conversación ya no coincide.')
                    if job['purchase'] is not None:
                        from .panel_purchases import resolve
                        item = await resolve(self.p, actor, job['uid'], job['generation'],
                                             client, entity, job['purchase'], job['index'])
                    else:
                        original = await client.get_messages(entity, ids=job['message'])
                        if not original or not isinstance(getattr(original, 'media', None),
                                                          types.MessageMediaPaidMedia):
                            raise ValueError('Esta publicación no es un archivo comprado con Estrellas.')
                        await reveal_purchased(client, entity, [original])
                        owned = purchased_items(original)
                        if not 0 <= job['index'] < len(owned):
                            raise ValueError('El archivo comprado no está disponible.')
                        item = owned[job['index']]
                    from .web_panel import media_info
                    info = media_info(item)
                    if not info or info['type'] not in ('video', 'photo'):
                        raise ValueError('Solo se pueden preparar videos y fotos comprados.')
                    total = int(info.get('size') or 0)
                    if total > MAX_BYTES:
                        raise ValueError('Este archivo supera el máximo de 4 GB admitido por Telegram.')
                    job['total'] = total
                    job['name'] = safe_filename(info, job['message'] or job['index'])
                    job['mime'] = info['mime']
                    if self.directory is None:
                        self.directory = tempfile.TemporaryDirectory(prefix='telegram-preparado-')
                    path = Path(self.directory.name)
                    if shutil.disk_usage(path).free < max(total, 0) + MIN_FREE:
                        raise ValueError('El servidor no tiene espacio temporal suficiente para este video grande; no se modificó la cuenta vinculada.')
                    part = path / (job['id'] + '.part')
                    target = path / (job['id'] + '.ready')
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    fd = os.open(part, flags, 0o600)
                    last_check = time.monotonic()
                    try:
                        with os.fdopen(fd, 'wb', buffering=0) as f:
                            iterator = client.iter_download(item.media, offset=0,
                                request_size=262144, chunk_size=262144)
                            try:
                                async for chunk in iterator:
                                    if not chunk:
                                        continue
                                    if job['received'] + len(chunk) > MAX_BYTES:
                                        raise ValueError('El archivo supera el máximo de 4 GB admitido por Telegram.')
                                    if shutil.disk_usage(path).free < MIN_FREE:
                                        raise ValueError('No hay espacio temporal suficiente.')
                                    await asyncio.to_thread(f.write, chunk)
                                    job['received'] += len(chunk)
                                    if time.monotonic() - last_check > 5:
                                        await self._check(job)
                                        last_check = time.monotonic()
                                    await asyncio.sleep(0)
                            finally:
                                await iterator.close()
                    finally:
                        if part.exists() and job['received'] == 0:
                            part.unlink(missing_ok=True)
                    if total and job['received'] != total:
                        raise ValueError('Descarga de Telegram incompleta. Inténtala de nuevo.')
                    await self._check(job)
                    os.replace(part, target)
                    part = None
                    job['file'] = str(target)
                    job['state'] = 'ready'
                    job['expires'] = time.monotonic() + READY_TTL
        except asyncio.CancelledError:
            job['state'] = 'error'
            job['error'] = 'La preparación se interrumpió. Solicítala de nuevo.'
            job['expires'] = time.monotonic() + ERROR_TTL
            raise
        except errors.FloodWaitError as exc:
            job['state'] = 'error'
            job['error'] = f'Telegram solicita esperar {exc.seconds} segundos antes de reintentar.'
            job['expires'] = time.monotonic() + ERROR_TTL
        except TimeoutError:
            job['state'] = 'error'
            job['error'] = 'La preparación tardó demasiado. Puedes volver a intentarlo.'
            job['expires'] = time.monotonic() + ERROR_TTL
        except ValueError as exc:
            job['state'] = 'error'
            job['error'] = str(exc)
            job['expires'] = time.monotonic() + ERROR_TTL
        except Exception:
            LOG.exception('Private on-demand Telegram media preparation failed')
            job['state'] = 'error'
            job['error'] = 'La preparación falló. Puedes reintentar desde el panel.'
            job['expires'] = time.monotonic() + ERROR_TTL
        finally:
            if part is not None:
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass
            ACTOR.reset(marker)

    async def serve(self, request):
        """5-minute read capability for Telegram's native downloadFile API.

        The token cannot list conversations or open sessions; it reads only the
        single already prepared file. Verify account generation and grant on
        every request so unlinking/revoking access also revokes this link.
        """
        token = request.query.get('token', '')
        if not token or len(token) > 128:
            raise web.HTTPNotFound()
        now = time.monotonic()
        job = next((j for j in self.jobs.values()
                    if j['state'] == 'ready' and j['capability']
                    and j['capability_expires'] > now
                    and secrets.compare_digest(j['capability'], token)), None)
        if not job or not job['file'] or not Path(job['file']).is_file():
            raise web.HTTPNotFound()
        try:
            await self._check(job)
        except ValueError:
            raise web.HTTPNotFound() from None
        headers = {
            'Cache-Control': 'private, no-store',
            'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer',
            'Content-Disposition': 'attachment; filename="' + job['name'] + '"',
            # Never let WebKit render the MP4 as a top-level QuickLook page.
            # File name/extension remain intact; only the download response is generic.
            'Content-Type': 'application/octet-stream',
            'Access-Control-Allow-Origin': 'https://web.telegram.org',
            'Vary': 'Origin',
            'Accept-Ranges': 'bytes',
        }
        return web.FileResponse(job['file'], headers=headers)
