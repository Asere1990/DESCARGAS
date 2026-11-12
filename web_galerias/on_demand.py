"""Prioridad bajo demanda de originales: aislada de DESCARGAS y DATOS.

Una solicitud de reproducción crea un trabajo durable para un único video ya
catalogado. Solo CLOUDFLARE lo procesa, respetando la misma cuenta, sus locks,
la cola de descargas principal y los controles de acceso de Web Galerías.
Los originales nunca se publican parcialmente: R2 confirma el multipart
antes de que el navegador reciba un enlace de reproducción.
"""
import asyncio
import logging
import os
import time

from aiohttp import web
from telethon import errors

from .core import identifier
from .downloads import temporary_link
from . import sync

log = logging.getLogger("web-galerias-priority")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wg_media_priority(
  media BIGINT PRIMARY KEY REFERENCES wg_media(id) ON DELETE CASCADE,
  requester BIGINT NOT NULL,
  requested_at DOUBLE PRECISION NOT NULL,
  next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'queued'
    CHECK(state IN ('queued','running')),
  last_error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS wg_media_priority_due
  ON wg_media_priority(next_attempt,requested_at DESC)
  WHERE state='queued';
"""

MAX_GLOBAL_PENDING = 48
MAX_USER_PENDING = 4
JOB_TTL_SECONDS = 3600


def active(g):
    task = getattr(g, "priority_task", None)
    return bool(task and not task.done() and not g.closed
                and g.storage.ready
                and os.getenv("GALERIAS_SYNC_ENABLED", "0") == "1")


async def _authorized_media(g, uid, media):
    mid = identifier(media)
    row = await g.db().fetchrow("""
      SELECT m.id,m.folder,m.message,m.kind,m.mime,m.size,m.filename,
             m.state,m.file_key,m.thumb_key,m.source_type,m.source_checks,
             f.active,f.peer,f.preferred_uid,f.source_uid
      FROM wg_media m JOIN wg_folders f ON f.id=m.folder
      WHERE m.id=$1
    """, mid)
    if (not row or not row["active"]
            or row["state"] not in ("preview", "ready")):
        raise web.HTTPNotFound(text="Archivo no disponible.")
    if not await g.permitted(uid, row["folder"]):
        raise web.HTTPForbidden(text="No tienes acceso a esta galería.")
    return dict(row)


def _result(g, uid, row, priority=None):
    mid = int(row["id"])
    available = row["state"] == "ready" and bool(row["file_key"])
    result = {
        "media": mid, "kind": row["kind"], "mime": row["mime"],
        "available": bool(available),
        "view": (
            f"/web-galerias/media/{mid}/view?t="
            + temporary_link(g, uid, mid, "view")
        ) if available else None,
        "thumb": (
            f"/web-galerias/media/{mid}/thumb?t="
            + temporary_link(g, uid, mid, "thumb")
        ) if row["thumb_key"] else None,
        "priority_state": "ready" if available else "idle",
        "retry_after": 0,
    }
    if not available and priority:
        wait = max(0, int(float(priority["next_attempt"] or 0) - time.time()))
        result["priority_state"] = (
            "waiting" if wait else str(priority["state"]))
        result["retry_after"] = wait
    return result


async def status(g, uid, media):
    row = await _authorized_media(g, uid, media)
    job = None
    if row["state"] != "ready" and active(g):
        job = await g.db().fetchrow(
            "SELECT state,next_attempt FROM wg_media_priority WHERE media=$1",
            row["id"])
    return _result(g, uid, row, job)


async def request(g, uid, media):
    """Idempotente: clics simultáneos del mismo video comparten un único trabajo."""
    row = await _authorized_media(g, uid, media)
    if row["kind"] != "video" or row["state"] == "ready":
        return _result(g, uid, row)
    if not active(g):
        raise web.HTTPServiceUnavailable(
            text="La preparación prioritaria no está disponible en este momento.")
    db = g.db()
    # Lock transaccional por archivo: bloquea ráfagas que intentarían saltarse
    # el límite y duplicar descargas del mismo archivo.
    async with db.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(784, 23)")
            existing = await connection.fetchrow(
                "SELECT state,next_attempt FROM wg_media_priority WHERE media=$1",
                row["id"])
            if not existing:
                global_count = await connection.fetchval(
                    "SELECT COUNT(*) FROM wg_media_priority")
                user_count = await connection.fetchval(
                    "SELECT COUNT(*) FROM wg_media_priority WHERE requester=$1",
                    uid)
                if int(global_count or 0) >= MAX_GLOBAL_PENDING:
                    raise web.HTTPTooManyRequests(
                        text="Hay muchos videos en preparación. Prueba nuevamente en un momento.")
                if int(user_count or 0) >= MAX_USER_PENDING:
                    raise web.HTTPTooManyRequests(
                        text="Ya tienes varios videos en preparación.")
                await connection.execute("""
                    INSERT INTO wg_media_priority(media,requester,requested_at)
                    VALUES($1,$2,$3) ON CONFLICT(media) DO NOTHING
                """, row["id"], uid, time.time())
    # Fetch the queue after commit: a worker may already have reserved it.
    return await status(g, uid, row["id"])


async def has_urgent(g, account_uid):
    """An active video request preempts *background* backups on that source."""
    if not active(g):
        return False
    return bool(await g.db().fetchval("""
       SELECT EXISTS(
         SELECT 1 FROM wg_media_priority q
         JOIN wg_media m ON m.id=q.media
         JOIN wg_folders f ON f.id=m.folder
         WHERE f.active AND m.state='preview'
           AND q.next_attempt <= $1
           AND (f.preferred_uid=$2 OR
              (f.preferred_uid IS NULL AND
                (f.source_uid=$2 OR f.source_uid IS NULL)))
       )
    """, time.time(), account_uid))


async def _defer(g, media, error, seconds):
    # FloodWait and busy locks must not cause a tight retry loop.
    await g.db().execute("""
      UPDATE wg_media_priority SET
        state='queued',attempts=attempts+1,next_attempt=$2,
        last_error=$3 WHERE media=$1
    """, media, time.time()+max(2, float(seconds)), str(error)[:90])


async def _process(g, media):
    """One bounded download, shared account lock, no second Telegram polling."""
    from descargas_app.account_chat import connected

    row = await g.db().fetchrow("""
      SELECT m.id,m.folder,m.message,m.kind,m.mime,m.size,m.filename,
             m.state,m.file_key,m.source_type,m.source_checks,
             f.id AS folder_id,f.peer,f.preferred_uid,f.source_uid,f.active
      FROM wg_media m JOIN wg_folders f ON f.id=m.folder
      WHERE m.id=$1
    """, media)
    if not row or not row["active"] or row["state"] == "ready":
        await g.db().execute("DELETE FROM wg_media_priority WHERE media=$1", media)
        return
    if row["state"] != "preview" or row["kind"] != "video":
        await g.db().execute("DELETE FROM wg_media_priority WHERE media=$1", media)
        return

    folder = dict(row)
    folder["id"] = row["folder_id"]
    uid = None
    try:
        if not active(g):
            raise sync.PauseRequested("priority_disabled")
        uid = await sync.choose_account(g, folder)
        if uid is None:
            raise sync.PauseRequested("source_unavailable")
        if not await sync.source_free(g, uid, folder["id"]):
            raise sync.PauseRequested("source_busy")
        service = g.s if uid > 0 else getattr(g.s, "datos", None)
        if service is None or (uid < 0 and not getattr(service, "ready", False)):
            raise sync.PauseRequested("source_unavailable")
        async with connected(service, abs(uid)) as client:
            guard = sync.PauseGuard(g, uid, folder["id"], priority=True)
            await guard()
            entity = await sync.find_dialog(
                g, client, folder["id"], uid, row["peer"])
            if entity is None:
                await sync.mark_discovery(g, folder["id"], uid, False, delay=600)
                raise sync.PauseRequested("source_has_no_access")
            message = await client.get_messages(entity, ids=row["message"])
            if not await sync.inspect_source(
                    g, folder["id"], dict(row), message):
                await _defer(g, media, "source_revalidation", 45)
                return
            await sync.backup_one(
                g, folder["id"], dict(row), message, client, guard)
        ready = await g.db().fetchval(
            "SELECT state='ready' FROM wg_media WHERE id=$1", media)
        if ready:
            await g.db().execute(
                "DELETE FROM wg_media_priority WHERE media=$1", media)
        else:
            await _defer(g, media, "not_ready", 20)
    except asyncio.CancelledError:
        # During shutdown, durable work becomes available to the next leader.
        await _defer(g, media, "interrupted", 5)
        raise
    except errors.FloodWaitError as exc:
        seconds = max(1, int(getattr(exc, "seconds", 60)))
        cooldown = max(600, seconds+120)
        if uid is not None:
            await g.db().execute("""
                INSERT INTO wg_account_cooldown(uid,available_at)
                VALUES($1,$2) ON CONFLICT(uid) DO UPDATE
                SET available_at=GREATEST(wg_account_cooldown.available_at,
                                         excluded.available_at)
            """, uid, time.time()+cooldown)
        await _defer(g, media, "FloodWaitError", cooldown)
        log.warning("Priority video Telegram cooldown=%ds", cooldown)
    except (sync.PauseRequested, ValueError) as exc:
        await _defer(g, media, type(exc).__name__, 8)
    except Exception as exc:
        count = await g.db().fetchval(
            "SELECT attempts FROM wg_media_priority WHERE media=$1", media)
        delay = min(600, 15 * 2 ** min(5, int(count or 0)))
        await _defer(g, media, type(exc).__name__, delay)
        log.warning("Priority video deferred (%s)", type(exc).__name__)


async def run(g):
    """At most two urgent videos; leader + durable queue survive restarts."""
    db = g.db()
    await db.execute(SCHEMA)
    parallel = max(1, min(2, int(os.getenv("GALERIAS_PRIORITY_PARALLEL", "2"))))
    running = {}
    async with db.acquire() as leader:
        while not g.closed:
            if await leader.fetchval("SELECT pg_try_advisory_lock(784,22)"):
                break
            await asyncio.sleep(3)
        if g.closed:
            return
        try:
            await db.execute("""
                UPDATE wg_media_priority SET state='queued',next_attempt=0
                WHERE state='running'
            """)
            log.warning("On-demand gallery video priority active; parallel=%d", parallel)
            while not g.closed:
                if not g.storage.ready or os.getenv("GALERIAS_SYNC_ENABLED","0")!="1":
                    await asyncio.sleep(5)
                    continue
                try:
                    # Bounded durable queue; don't accumulate abandoned work.
                    await db.execute("""
                        DELETE FROM wg_media_priority
                        WHERE requested_at<$1
                           OR media IN (SELECT id FROM wg_media
                                        WHERE state NOT IN ('preview','pending'))
                    """, time.time()-JOB_TTL_SECONDS)
                    available = parallel-len(running)
                    if available > 0:
                        due = await db.fetch("""
                            WITH next AS (
                              SELECT media FROM wg_media_priority
                              WHERE state='queued' AND next_attempt<=$1
                              ORDER BY requested_at DESC LIMIT $2
                              FOR UPDATE SKIP LOCKED
                            )
                            UPDATE wg_media_priority p SET state='running'
                            FROM next WHERE p.media=next.media
                            RETURNING p.media
                        """, time.time(), available)
                        for record in due:
                            media=int(record["media"])
                            task=asyncio.create_task(_process(g,media))
                            running[media]=task
                            task.add_done_callback(
                                lambda _task, mid=media: running.pop(mid, None))
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Priority supervisor retry (%s)", type(exc).__name__)
                    await asyncio.sleep(8)
        finally:
            for task in tuple(running.values()):
                task.cancel()
            if running:
                await asyncio.gather(*tuple(running.values()),
                                     return_exceptions=True)
            await leader.execute("SELECT pg_advisory_unlock(784,22)")
