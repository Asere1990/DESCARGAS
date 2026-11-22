"""Explicit, resumable Telegram ranges using Web Galerías reader accounts."""
import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from aiohttp import web
from telethon import errors, types, utils

from . import history_import, media_text
from .core import identifier
from .media_types import classify

LOG = logging.getLogger("web-galerias-intervals")
SCHEMA = """
CREATE TABLE IF NOT EXISTS wg_interval_sources (
    folder BIGINT NOT NULL REFERENCES wg_folders(id) ON DELETE CASCADE,
    peer BIGINT NOT NULL,
    source BIGINT NOT NULL UNIQUE REFERENCES wg_folders(id) ON DELETE CASCADE,
    PRIMARY KEY(folder, peer)
);
CREATE TABLE IF NOT EXISTS wg_interval_jobs (
    id BIGSERIAL PRIMARY KEY,
    uid BIGINT,
    peer BIGINT,
    first_link TEXT NOT NULL,
    last_link TEXT NOT NULL,
    first_message BIGINT,
    last_message BIGINT,
    folder BIGINT REFERENCES wg_folders(id) ON DELETE CASCADE,
    source BIGINT REFERENCES wg_folders(id) ON DELETE CASCADE,
    cursor BIGINT NOT NULL DEFAULT 0,
    photos BIGINT NOT NULL DEFAULT 0,
    videos BIGINT NOT NULL DEFAULT 0,
    albums BIGINT NOT NULL DEFAULT 0,
    copied BIGINT NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued',
    phase TEXT NOT NULL DEFAULT 'count',
    available DOUBLE PRECISION NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    revision BIGINT NOT NULL DEFAULT 0,
    reply_cursor BIGINT NOT NULL DEFAULT 0,
    reply_available DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS wg_interval_threads (
    source BIGINT NOT NULL REFERENCES wg_folders(id) ON DELETE CASCADE,
    anchor BIGINT NOT NULL,
    cursor BIGINT NOT NULL DEFAULT 0,
    available DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY(source,anchor)
);
ALTER TABLE wg_interval_threads ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON wg_interval_threads FROM PUBLIC;
CREATE INDEX IF NOT EXISTS wg_interval_jobs_queue ON wg_interval_jobs(state,available,id);
ALTER TABLE wg_interval_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE wg_interval_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON wg_interval_sources, wg_interval_jobs FROM PUBLIC;
"""


class Interrupted(Exception):
    """An administrator changed this job while it was running."""


def message_link(value):
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("Pega enlaces de mensajes de Telegram.")
    url = urlsplit(value.strip())
    if url.scheme != "https" or url.netloc.lower() not in ("t.me", "www.t.me"):
        raise ValueError("Pega enlaces HTTPS de mensajes de Telegram.")
    parts = url.path.strip("/").split("/")
    private = parts[0] == "c"
    if private:
        parts = parts[1:]
    if len(parts) not in (2, 3) or not all(re.fullmatch(r"[0-9]{1,10}", p) for p in parts[1:]):
        raise ValueError("Usa el enlace de un mensaje o álbum, no una invitación.")
    message = int(parts[-1])
    if not 0 < message < 2**31 - 101:
        raise ValueError("El mensaje del enlace no es válido.")
    if private and re.fullmatch(r"[1-9][0-9]{0,14}", parts[0]):
        return int("-100" + parts[0]), message
    if not private and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,31}", parts[0]):
        return parts[0].lower(), message
    raise ValueError("El grupo o canal del enlace no es válido.")


def media_kind(message):
    if (getattr(message, "noforwards", False) or getattr(message, "ttl_period", None)
            or getattr(getattr(message, "media", None), "ttl_seconds", None)):
        return None
    return classify(message)[0]


async def overview(g):
    db = g.db()
    jobs = await db.fetch("""
        SELECT j.id::text, uid::text, j.peer::text, first_link, last_link, j.folder::text,
               COALESCE(f.public_name,f.name) AS folder_name, photos,videos,albums,
               copied,state,phase,j.error
        FROM wg_interval_jobs j LEFT JOIN wg_folders f ON f.id=j.folder
        ORDER BY j.id DESC
    """)
    accounts = await db.fetch("SELECT uid::text FROM wg_history_readers ORDER BY uid")
    return {"jobs": [dict(row) for row in jobs], "accounts": [row["uid"] for row in accounts]}


async def operation(g, actor, op, data):
    if not getattr(g, "interval_import_ready", False):
        raise web.HTTPServiceUnavailable(text="La importación por intervalos aún no está disponible.")
    if op == "status":
        return await overview(g)
    db = g.db()
    if op == "count":
        uid = identifier(data.get("account"))
        first, last = data.get("first"), data.get("last")
        try:
            first_peer, first_id = message_link(first)
            last_peer, last_id = message_link(last)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        same_reference_type = isinstance(first_peer, int) == isinstance(last_peer, int)
        if first_id > last_id or (same_reference_type and first_peer != last_peer):
            raise web.HTTPBadRequest(text="Usa dos enlaces del mismo grupo, en orden del inicio al final.")
        async with db.acquire() as conn, conn.transaction():
            await lock_reader(conn, uid)
            if await conn.fetchval("""
                SELECT count(*) FROM wg_interval_jobs WHERE uid=$1 AND peer IS NULL
            """, uid) >= 10:
                raise web.HTTPConflict(text="Ya hay 10 intervalos pendientes de contar para esta cuenta.")
            job = await conn.fetchval("""
                INSERT INTO wg_interval_jobs(uid,first_link,last_link)
                VALUES($1,$2,$3) RETURNING id
            """, uid, first.strip(), last.strip())
        await g.log(actor, "admin-interval-count")
        return {"id": str(job)}
    if op == "unlink":
        uid = identifier(data.get("account"))
        async with db.acquire() as conn, conn.transaction():
            await lock_reader(conn, uid)
            if await conn.fetchval("""
                SELECT EXISTS(SELECT 1 FROM wg_history_imports
                    WHERE uid=$1 AND state IN ('queued','running'))
            """, uid):
                raise web.HTTPConflict(text="La cuenta también está importando por la vía de historial.")
            await conn.execute("""
                UPDATE wg_interval_jobs SET uid=NULL, state=CASE
                    WHEN state='done' THEN state ELSE 'paused' END,
                    error='',revision=revision+1 WHERE uid=$1
            """, uid)
            await conn.execute("DELETE FROM wg_history_readers WHERE uid=$1", uid)
        await g.log(actor, "admin-interval-unlink")
        return {}
    job_id = identifier(data.get("id"))
    async with db.acquire() as conn, conn.transaction():
        replacement = None
        if op == "account":
            replacement = identifier(data.get("account"))
            await lock_reader(conn, replacement)
        job = await conn.fetchrow("SELECT * FROM wg_interval_jobs WHERE id=$1 FOR UPDATE", job_id)
        if not job:
            raise web.HTTPNotFound(text="Este intervalo ya no existe.")
        if op == "start":
            if job["state"] != "counted" or not job["uid"] or not (job["photos"] + job["videos"]):
                raise web.HTTPConflict(text="Primero calcula el intervalo.")
            folder, source = await destination(conn, job["peer"], data)
            await conn.execute("""
                UPDATE wg_interval_jobs SET folder=$2,source=$3,phase='copy',
                    state='queued',available=0,error='',revision=revision+1,reply_cursor=last_message
                WHERE id=$1
            """, job_id, folder, source)
        elif op in ("pause", "stop"):
            if job["state"] not in ("queued", "running", "paused", "failed"):
                raise web.HTTPConflict(text="El intervalo no está en ejecución.")
            await conn.execute("""
                UPDATE wg_interval_jobs SET state=$2,error='',revision=revision+1 WHERE id=$1
            """, job_id, "paused" if op == "pause" else "stopped")
        elif op == "resume":
            if not job["uid"] or job["state"] not in ("paused", "stopped", "failed"):
                raise web.HTTPConflict(text="Elige una cuenta vinculada para reanudar.")
            state = "counted" if job["phase"] == "count" and job["peer"] is not None else "queued"
            await conn.execute("""
                UPDATE wg_interval_jobs SET state=$2,available=0,error='',revision=revision+1
                WHERE id=$1
            """, job_id, state)
        elif op == "account":
            if job["state"] in ("running", "queued"):
                raise web.HTTPConflict(text="Pausa el intervalo antes de cambiar la cuenta.")
            if job["peer"] is not None:
                await check_limit(conn, replacement, job["peer"], job_id)
            await conn.execute("""
                UPDATE wg_interval_jobs SET uid=$2,error='',revision=revision+1 WHERE id=$1
            """, job_id, replacement)
        elif op == "delete":
            await conn.execute("DELETE FROM wg_interval_jobs WHERE id=$1", job_id)
        else:
            raise web.HTTPNotFound()
    await g.log(actor, "admin-interval-" + op)
    return {}


async def lock_reader(conn, uid):
    if not await conn.fetchval("SELECT uid FROM wg_history_readers WHERE uid=$1 FOR UPDATE", uid):
        raise web.HTTPConflict(text="Vincula la cuenta directamente en Web Galerías.")


async def check_limit(conn, uid, peer, job_id):
    if await conn.fetchval("""
        SELECT count(*) FROM wg_interval_jobs WHERE uid=$1 AND peer=$2 AND id<>$3
    """, uid, peer, job_id) >= 10:
        raise web.HTTPConflict(text="El máximo es 10 intervalos por cuenta y grupo.")


async def destination(conn, peer, data):
    name = data.get("name")
    if name is not None:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise web.HTTPBadRequest(text="Escribe un nombre de carpeta de hasta 100 caracteres.")
        folder = await new_folder(conn, name.strip())
    else:
        folder = identifier(data.get("folder"))
        if not await conn.fetchval("""
            SELECT id FROM wg_folders WHERE id=$1 AND NOT EXISTS (
                SELECT 1 FROM wg_folder_sources WHERE source_folder=$1
            ) FOR UPDATE
        """, folder):
            raise web.HTTPNotFound(text="No se encontró la carpeta de destino.")
    source = await conn.fetchval("""
        SELECT source FROM wg_interval_sources WHERE folder=$1 AND peer=$2
    """, folder, peer)
    if source is None:
        source = await new_folder(conn, f"Intervalos de Telegram · {peer}")
        await conn.execute("""
            INSERT INTO wg_folder_sources(parent_folder,source_folder,created)
            VALUES($1,$2,$3)
        """, folder, source, time.time())
        await conn.execute("INSERT INTO wg_interval_sources VALUES($1,$2,$3)", folder, peer, source)
    return folder, source


async def new_folder(conn, name):
    return await conn.fetchval("""
        INSERT INTO wg_folders(peer,name,public_name,created,source_enabled)
        VALUES(4611686018427387904 + nextval('wg_history_source_seq'),$1,$1,$2,FALSE)
        RETURNING id
    """, name, time.time())


@asynccontextmanager
async def reader(g, uid):
    token = await g.db().fetchval("SELECT session FROM wg_history_readers WHERE uid=$1", uid)
    if not token:
        raise history_import.ImportPaused("Vincula la cuenta en Web Galerías.")
    client = g.s.client(g.s.cipher.decrypt(token).decode())
    client.flood_sleep_threshold = 0
    try:
        await client.connect()
        me = await client.get_me()
        if not me or me.id != uid:
            raise history_import.ImportPaused("La sesión venció. Vuelve a vincular la cuenta.")
        yield client
    finally:
        await client.disconnect()


async def resolve_peer(client, reference):
    if isinstance(reference, int):
        async for dialog in client.iter_dialogs():
            if dialog.id == reference and (dialog.is_group or dialog.is_channel):
                return dialog.entity
        raise history_import.ImportPaused("La cuenta debe estar en el grupo o canal indicado.")
    entity = await client.get_entity(reference)
    if not isinstance(entity, (types.Channel, types.Chat)) or getattr(entity, "left", False):
        raise history_import.ImportPaused("La cuenta debe estar en el grupo o canal indicado.")
    return entity


def require_copy_allowed(entity):
    if getattr(entity, "noforwards", False):
        raise history_import.ImportPaused("Telegram marca el grupo o canal como contenido protegido.")


async def count_interval(g, job, client, guard):
    from descargas_app.service import album

    first_peer, first_id = message_link(job["first_link"])
    last_peer, last_id = message_link(job["last_link"])
    entity = await resolve_peer(client, first_peer)
    peer = utils.get_peer_id(entity)
    if first_peer != last_peer and utils.get_peer_id(await resolve_peer(client, last_peer)) != peer:
        raise history_import.ImportPaused("Los dos enlaces deben ser del mismo grupo o canal.")
    require_copy_allowed(entity)
    first = await client.get_messages(entity, ids=first_id)
    last = first if last_id == first_id else await client.get_messages(entity, ids=last_id)
    if not first or not last:
        raise history_import.ImportPaused("La cuenta no puede leer alguno de los mensajes de inicio o final.")
    low = min(item.id for item in await album(client, entity, first))
    high = max(item.id for item in await album(client, entity, last))
    photos = videos = 0
    albums = set()
    async for message in client.iter_messages(entity, min_id=low - 1, max_id=high + 1):
        await guard()
        kind = media_kind(message)
        photos += kind == "photo"
        videos += kind == "video"
        if kind and message.grouped_id:
            albums.add(message.grouped_id)
    async with g.db().acquire() as conn, conn.transaction():
        await lock_reader(conn, job["uid"])
        await check_limit(conn, job["uid"], peer, job["id"])
        await conn.execute("""
            UPDATE wg_interval_jobs SET peer=$3,first_message=$4,last_message=$5,
                cursor=$4::bigint-1,photos=$6,videos=$7,albums=$8,state='counted'
            WHERE id=$1 AND revision=$2 AND state='running'
        """, job["id"], job["revision"], peer, low, high, photos, videos, len(albums))


async def copy_interval(g, job, client, guard, locks):
    from .sync import backup_one, catalog

    db = g.db()
    entity = await resolve_peer(client, job["peer"])
    require_copy_allowed(entity)
    source = job["source"]
    async for message in client.iter_messages(
        entity, reverse=True, min_id=job["cursor"], max_id=job["last_message"] + 1
    ):
        await guard()
        if not getattr(getattr(message, "reply_to", None), "reply_to_peer_id", None):
            parent = await media_text.capture_reply(g, source, message)
            if parent:
                await media_text.enqueue(g, source, (parent, message.id))
        kind = media_kind(message)
        copied = False
        if kind:
            # Overlapping intervals share this source and the original R2 key.
            async with locks[hash((source, message.id)) % len(locks)]:
                await guard()
                await catalog(g, source, message)
                item = await db.fetchrow("""
                    SELECT * FROM wg_media WHERE folder=$1 AND message=$2
                """, source, message.id)
                if item and item["state"] == "pending":
                    await backup_one(g, source, item, message, client, guard)
                state = await db.fetchval("""
                    SELECT state FROM wg_media WHERE folder=$1 AND message=$2
                """, source, message.id)
                if state not in ("ready", "removed"):
                    raise history_import.ImportPaused("La copia no se confirmó en R2.")
                copied = state == "ready"
                if copied:
                    await media_text.enqueue(g, source, (message.id,))
                    if getattr(getattr(message, "replies", None), "comments", False):
                        await db.execute("""
                            INSERT INTO wg_interval_threads(source,anchor) VALUES($1,$2)
                            ON CONFLICT DO NOTHING
                        """, source, message.id)
        if copied or kind:
            await db.execute("""
                UPDATE wg_interval_jobs SET cursor=$3,copied=copied+$4
                WHERE id=$1 AND revision=$2 AND state='running'
            """, job["id"], job["revision"], message.id, int(copied))


async def process(g, job, locks):
    db = g.db()
    state, error, available = "done", "", 0
    last_check = float("-inf")

    async def guard():
        nonlocal last_check
        now = time.monotonic()
        if now - last_check < 1:
            return
        if not await db.fetchval("""
            SELECT 1 FROM wg_interval_jobs WHERE id=$1 AND revision=$2 AND state='running'
        """, job["id"], job["revision"]):
            raise Interrupted()
        last_check = now

    try:
        async with reader(g, job["uid"]) as client:
            if job["phase"] == "count":
                await count_interval(g, job, client, guard)
                return
            await copy_interval(g, job, client, guard, locks)
    except Interrupted:
        return
    except errors.FloodWaitError as exc:
        state, available = "queued", time.time() + max(1, exc.seconds)
        error = f"Telegram pidió esperar {exc.seconds} s. Se reanudará automáticamente."
        await db.execute("""
            UPDATE wg_interval_jobs SET available=GREATEST(available,$2)
            WHERE uid=$1 AND state='queued'
        """, job["uid"], available)
    except history_import.SESSION_ERRORS:
        state, error = "failed", "La sesión venció. Vuelve a vincular la cuenta."
    except (history_import.ImportPaused, ValueError, web.HTTPException) as exc:
        state, error = "failed", (exc.text if isinstance(exc, web.HTTPException) else str(exc))[:200]
    except Exception as exc:
        LOG.warning("Interval %s paused (%s)", job["id"], type(exc).__name__)
        state, error = "failed", "No se pudo completar. Pulsa Reanudar para intentarlo de nuevo."
    await db.execute("""
        UPDATE wg_interval_jobs SET state=$3,error=$4,available=$5
        WHERE id=$1 AND revision=$2 AND state='running'
    """, job["id"], job["revision"], state, error, available)


async def follow_replies(g, job):
    """Index later group replies and channel discussions in bounded batches."""
    db = g.db()
    source = job["source"]
    delay = 90
    try:
        async with asyncio.timeout(40), reader(g, job["uid"]) as client:
            entity = await resolve_peer(client, job["peer"])
            require_copy_allowed(entity)
            if not getattr(entity, "broadcast", False):
                cursor = job["reply_cursor"]
                async for message in client.iter_messages(entity, reverse=True, min_id=cursor, limit=80):
                    header = getattr(message, "reply_to", None)
                    parent = getattr(header, "reply_to_msg_id", None)
                    if parent and not getattr(header, "reply_to_peer_id", None) and await db.fetchval("""
                        SELECT EXISTS(SELECT 1 FROM wg_media WHERE folder=$1 AND message=$2
                            AND state='ready') OR EXISTS(SELECT 1 FROM wg_replies
                            WHERE folder=$1 AND reply_message=$2)
                    """, source, parent):
                        anchor = await media_text.capture_reply(g, source, message)
                        if anchor:
                            await media_text.enqueue(g, source, (anchor, message.id))
                    cursor = message.id
                await db.execute("""
                    UPDATE wg_interval_jobs SET reply_cursor=$3 WHERE id=$1 AND revision=$2
                """, job["id"], job["revision"], cursor)
            if getattr(entity, "broadcast", False):
                threads = await db.fetch("""
                    SELECT anchor,cursor FROM wg_interval_threads WHERE source=$1 AND available<=$2
                    ORDER BY available,anchor LIMIT 4
                """, source, time.time())
                for thread in threads:
                    await follow_thread(g, client, entity, source, thread)
                delay = 30
    except errors.FloodWaitError as exc:
        delay = max(delay, exc.seconds)
        await db.execute("""
            UPDATE wg_interval_jobs SET available=GREATEST(available,$2)
            WHERE uid=$1 AND state='queued'
        """, job["uid"], time.time() + delay)
    except (history_import.ImportPaused, *history_import.SESSION_ERRORS):
        delay = 600
    except Exception as exc:
        LOG.warning("Interval reply check %s deferred (%s)", job["id"], type(exc).__name__)
    await db.execute("""
        UPDATE wg_interval_jobs SET reply_available=$3 WHERE id=$1 AND revision=$2
    """, job["id"], job["revision"], time.time() + delay)


async def follow_thread(g, client, entity, source, thread):
    db = g.db()
    anchor, cursor = thread["anchor"], thread["cursor"]
    delay = 180
    try:
        async for message in client.iter_messages(
            entity, reply_to=anchor, reverse=True, min_id=cursor, limit=40
        ):
            text = (message.message or "").strip()
            if text and not (getattr(message, "noforwards", False)
                             or getattr(message, "ttl_period", None)):
                # Discussion IDs are from another chat. Reserve negative IDs
                # in the local reply index so they cannot collide with posts.
                await db.execute("""
                    INSERT INTO wg_replies(folder,parent_message,reply_message,words)
                    VALUES($1,$2,$3,$4) ON CONFLICT(folder,reply_message)
                    DO UPDATE SET words=excluded.words
                """, source, anchor, -message.id, text[:1500])
                await media_text.enqueue(g, source, (anchor,))
            cursor = message.id
    except errors.MsgIdInvalidError:
        delay = 3600
    await db.execute("""
        UPDATE wg_interval_threads SET cursor=GREATEST(cursor,$3),available=$4
        WHERE source=$1 AND anchor=$2
    """, source, anchor, cursor, time.time() + delay)


async def work(g):
    db = g.db()
    locks = [asyncio.Lock() for _ in range(64)]
    tasks = {}
    await db.execute("UPDATE wg_interval_jobs SET state='queued' WHERE state='running'")
    try:
        while not g.closed:
            for task in tuple(tasks):
                if task.done():
                    del tasks[task]
                    task.result()
            if len(tasks) < 4:
                accounts = [job["uid"] for job, _ in tasks.values()]
                saturated = [uid for uid in set(accounts) if accounts.count(uid) >= 2]
                active_jobs = [job["id"] for job, _ in tasks.values()]
                job = await db.fetchrow("""
                    UPDATE wg_interval_jobs SET state='running',error='' WHERE id=(
                        SELECT id FROM wg_interval_jobs
                        WHERE state='queued' AND available<=$1 AND uid IS NOT NULL
                            AND NOT (uid=ANY($2::bigint[])) AND NOT (id=ANY($3::bigint[]))
                        ORDER BY available,id LIMIT 1
                    ) RETURNING *
                """, time.time(), saturated, active_jobs)
                if job:
                    tasks[asyncio.create_task(process(g, job, locks))] = (job, False)
                    continue
                if not any(following for _, following in tasks.values()):
                    job = await db.fetchrow("""
                        SELECT * FROM wg_interval_jobs WHERE state='done' AND uid IS NOT NULL
                        AND reply_available<=$1 AND NOT (uid=ANY($2::bigint[]))
                        ORDER BY reply_available,id LIMIT 1
                    """, time.time(), saturated)
                    if job:
                        tasks[asyncio.create_task(follow_replies(g, job))] = (job, True)
            await asyncio.sleep(1)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(g):
    db = g.db()
    while not g.closed:
        try:
            if getattr(g, "history_import_ready", False):
                await db.execute(SCHEMA)
                g.interval_import_ready = True
                async with db.acquire() as leader:
                    if await leader.fetchval("SELECT pg_try_advisory_lock(784,25)"):
                        try:
                            await work(g)
                        finally:
                            await leader.execute("SELECT pg_advisory_unlock(784,25)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Interval worker retrying (%s)", type(exc).__name__)
        await asyncio.sleep(5)
