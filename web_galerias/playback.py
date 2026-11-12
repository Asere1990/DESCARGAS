"""Reproducción a demanda: un video solicitado adelanta copias masivas, sin sesiones nuevas sin lock.

La cola reside únicamente en tablas wg_ del servicio CLOUDFLARE. Nunca modifica
sesiones, trabajos, cuentas ni límites de descarga de DESCARGAS o DATOS.
"""
import asyncio
import logging
import os
import time

from aiohttp import web
from telethon import errors

from .core import identifier
from .downloads import temporary_link

log = logging.getLogger("web-galerias-playback")


def _enabled(g):
    return (not g.closed and os.getenv("GALERIAS_SYNC_ENABLED", "0") == "1"
            and g.storage.ready)


async def media_status(g, uid, media):
    """Consultar estado del original sin crear una conexión a Telegram."""
    mid = identifier(media)
    row = await g.db().fetchrow("""
        SELECT m.id,m.folder,m.kind,m.mime,m.state,m.file_key,m.thumb_key,
               f.active
        FROM wg_media m JOIN wg_folders f ON f.id=m.folder WHERE m.id=$1
    """, mid)
    if not row or not row["active"] or row["state"] not in ("preview", "ready"):
        raise web.HTTPNotFound(text="Archivo no disponible.")
    if not await g.permitted(uid, row["folder"]):
        raise web.HTTPForbidden(text="Archivo no autorizado.")
    ready = row["state"] == "ready" and bool(row["file_key"])
    result = {
        "media": mid, "kind": row["kind"], "mime": row["mime"],
        "available": ready,
        "view": (f"/web-galerias/media/{mid}/view?t="
                 + temporary_link(g, uid, mid, "view")) if ready else None,
        "thumb": (f"/web-galerias/media/{mid}/thumb?t="
                  + temporary_link(g, uid, mid, "thumb")) if row["thumb_key"] else None,
        "status": "ready" if ready else "pending",
    }
    if not ready and row["kind"] == "video":
        job = await g.db().fetchrow("""
            SELECT state,retry_after FROM wg_playback_requests WHERE media=$1
        """, mid)
        if job:
            result["status"] = ("working" if job["state"] == "working" else
                                "waiting" if float(job["retry_after"]) > time.time() else "queued")
        elif not _enabled(g):
            result["status"] = "unavailable"
    return result


async def request_playback(g, uid, media, *, requested_by=None):
    """Sólo un visitante autorizado puede elevar un video visible en su galería."""
    result = await media_status(g, uid, media)
    if result["available"]:
        return result
    if result["kind"] != "video":
        raise web.HTTPBadRequest(text="La reproducción prioritaria solo admite videos.")
    if not _enabled(g):
        return {**result, "status": "unavailable"}
    mid = result["media"]
    actor = identifier(requested_by if requested_by is not None else uid)
    db = g.db()
    # Repetir un toque sobre el mismo archivo nunca multiplica trabajos.
    existing = await db.fetchval("SELECT 1 FROM wg_playback_requests WHERE media=$1", mid)
    if not existing:
        mine = await db.fetchval("""
            SELECT COUNT(*) FROM wg_playback_requests
            WHERE requested_by=$1 AND state IN ('queued','working')
              AND requested_at>$2
        """, actor, time.time() - 900)
        if int(mine or 0) >= 3:
            raise web.HTTPTooManyRequests(text="Ya se están preparando tres videos. Inténtalo más tarde.")
        total = await db.fetchval("""
            SELECT COUNT(*) FROM wg_playback_requests
            WHERE state IN ('queued','working') AND requested_at>$1
        """, time.time() - 900)
        if int(total or 0) >= 150:
            raise web.HTTPTooManyRequests(text="Hay muchas reproducciones en preparación. Inténtalo de nuevo.")
    now = time.time()
    await db.execute("""
        INSERT INTO wg_playback_requests(media,requested_by,requested_at,state,retry_after)
        VALUES($1,$2,$3,'queued',0)
        ON CONFLICT(media) DO UPDATE SET
          requested_at=excluded.requested_at,
          requested_by=excluded.requested_by
        -- Mantener la espera impuesta por Telegram y el trabajo en curso.
    """, mid, actor, now)
    result["status"] = "queued"
    return result


async def background_should_yield(g, uid, folder, current_message=None):
    """La copia normal cede a una solicitud del mismo origen; no interrumpe
    una copia si ya está descargando el propio video solicitado.
    """
    if not _enabled(g):
        return False
    return bool(await g.db().fetchval("""
        SELECT EXISTS(
          SELECT 1 FROM wg_playback_requests r
          JOIN wg_media m ON m.id=r.media
          JOIN wg_folders f ON f.id=m.folder
          WHERE f.active AND m.state='preview'
            AND r.requested_at>$2
            AND (r.state='working' OR r.retry_after<=$3)
            AND (f.id=$4 OR f.source_uid=$1 OR f.preferred_uid=$1
                 OR (f.source_uid IS NULL AND f.preferred_uid IS NULL))
            AND NOT (f.id=$4 AND m.message=$5)
        )
    """, uid, time.time() - 900, time.time(), folder,
        int(current_message or 0)))


async def _defer(g, media, seconds, *, failed=False):
    await g.db().execute("""
        UPDATE wg_playback_requests
        SET state='queued',retry_after=$2,attempts=attempts+$3
        WHERE media=$1
    """, media, time.time() + max(2, min(3600, seconds)), int(failed))


async def _serve_job(g, job):
    """Descarga un único mensaje (sin recorrer los otros 30.000 videos)."""
    from descargas_app.account_chat import connected
    from .sync import (PauseGuard, PauseRequested, backup_one, choose_account,
                       find_dialog, folder_active, inspect_source, mark_discovery,
                       source_free)
    mid = int(job["media"])
    row = await g.db().fetchrow("""
        SELECT m.id,m.message,m.kind,m.mime,m.size,m.filename,m.state,
               m.source_type,m.source_checks,m.folder,
               f.peer,f.source_uid,f.preferred_uid,f.active
        FROM wg_media m JOIN wg_folders f ON f.id=m.folder WHERE m.id=$1
    """, mid)
    if not row or not row["active"] or row["state"] != "preview" or row["kind"] != "video":
        await g.db().execute("DELETE FROM wg_playback_requests WHERE media=$1", mid)
        return
    folder = int(row["folder"])
    if not await folder_active(g, folder):
        await _defer(g, mid, 15)
        return
    uid = await choose_account(g, dict(row))
    if uid is None or not await source_free(g, uid, folder):
        await _defer(g, mid, 5)
        return
    source = g.s if uid > 0 else getattr(g.s, "datos", None)
    if source is None or (uid < 0 and not getattr(source, "ready", False)):
        await _defer(g, mid, 15)
        return
    try:
        # account_lock(wait=False) y control de descarga del sistema original;
        # la prioridad jamás toma una cuenta usada por DESCARGAS/DATOS.
        async with connected(source, abs(uid)) as client:
            guard = PauseGuard(g, uid, folder)
            await guard()
            entity = await find_dialog(g, client, folder, uid, row["peer"])
            if entity is None:
                await mark_discovery(g, folder, uid, False, delay=300)
                await _defer(g, mid, 30)
                return
            await mark_discovery(g, folder, uid, True)
            message = await client.get_messages(entity, ids=row["message"])
            await guard()
            if not await inspect_source(g, folder, row, message):
                refreshed = await g.db().fetchval(
                    "SELECT state FROM wg_media WHERE id=$1", mid)
                if refreshed not in ("preview", "pending"):
                    await g.db().execute(
                        "DELETE FROM wg_playback_requests WHERE media=$1", mid)
                else:
                    await _defer(g, mid, 15)
                return
            await backup_one(g, folder, row, message, client, guard)
        await g.db().execute("DELETE FROM wg_playback_requests WHERE media=$1", mid)
        log.info("Priority video completed media=%d folder=%d", mid, folder)
    except asyncio.CancelledError:
        raise
    except (PauseRequested, ValueError) as exc:
        # La cuenta está ocupada por otro trabajo; no cuenta como fallo.
        if isinstance(exc, ValueError) and "Espera a que termine" not in str(exc):
            log.warning("Priority video source unavailable (%s)", type(exc).__name__)
            await _defer(g, mid, 30, failed=True)
        else:
            await _defer(g, mid, 4)
    except errors.FloodWaitError as exc:
        seconds = max(1, int(getattr(exc, "seconds", 60)))
        await g.db().execute("""
            INSERT INTO wg_account_cooldown(uid,available_at) VALUES($1,$2)
            ON CONFLICT(uid) DO UPDATE SET available_at=
            GREATEST(wg_account_cooldown.available_at,excluded.available_at)
        """, uid, time.time() + max(600, seconds + 120))
        await _defer(g, mid, max(600, seconds + 120))
    except Exception as exc:
        log.warning("Priority video retry (%s)", type(exc).__name__)
        attempts = int(job["attempts"] or 0) + 1
        if attempts >= 5:
            await g.db().execute("DELETE FROM wg_playback_requests WHERE media=$1", mid)
            log.warning("Priority video stopped after 5 errors media=%d", mid)
        else:
            await _defer(g, mid, min(300, 15 * 2 ** min(4, attempts)), failed=True)


async def priority_worker(g):
    """Una cola duradera, reclamada atómicamente; ejecutada sólo bajo líder."""
    db = g.db()
    while not g.closed:
        try:
            if not _enabled(g):
                await asyncio.sleep(5)
                continue
            now = time.time()
            job = await db.fetchrow("""
                UPDATE wg_playback_requests r SET state='working'
                FROM (
                  SELECT media FROM wg_playback_requests
                  WHERE state='queued' AND retry_after<=$1
                    AND requested_at>$2
                  ORDER BY requested_at ASC
                  LIMIT 1 FOR UPDATE SKIP LOCKED
                ) q WHERE r.media=q.media
                RETURNING r.media,r.attempts
            """, now, now - 900)
            if job:
                await _serve_job(g, job)
            else:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Priority worker retry (%s)", type(exc).__name__)
            await asyncio.sleep(5)
