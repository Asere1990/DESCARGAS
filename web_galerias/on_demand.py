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
-- Real bytes delivered from Telegram, never guessed timer percentages.
ALTER TABLE wg_media_priority ADD COLUMN IF NOT EXISTS received_bytes BIGINT NOT NULL DEFAULT 0;
ALTER TABLE wg_media_priority ADD COLUMN IF NOT EXISTS expected_bytes BIGINT NOT NULL DEFAULT 0;
ALTER TABLE wg_media_priority ADD COLUMN IF NOT EXISTS progress_at DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE wg_media_priority ADD COLUMN IF NOT EXISTS started_at DOUBLE PRECISION NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS wg_media_priority_requester ON wg_media_priority(requester);
CREATE INDEX IF NOT EXISTS wg_media_priority_fifo
  ON wg_media_priority(requested_at ASC) WHERE state='queued';
CREATE INDEX IF NOT EXISTS wg_media_priority_due
  ON wg_media_priority(next_attempt,requested_at DESC)
  WHERE state='queued';
"""

# The queue holds 250+ distinct requests; it does NOT start 250 Telegram
# transfers at once. Actual transfers are separately bounded per service.
MAX_GLOBAL_PENDING = 500
MAX_USER_PENDING = 4
JOB_TTL_SECONDS = 48 * 3600
DEFAULT_PARALLEL = 4
MAX_PARALLEL = 6


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
        "progress_percent": 100 if available else None,
        "received_bytes": 0,
        "expected_bytes": 0,
        "queue_position": None,
        "queue_total": None,
    }
    if not available and priority:
        priority = dict(priority)
        wait = max(0, int(float(priority["next_attempt"] or 0) - time.time()))
        result["priority_state"] = (
            "waiting" if wait else str(priority["state"]))
        result["retry_after"] = wait
        received = max(0, int(priority.get("received_bytes") or 0))
        expected = max(0, int(priority.get("expected_bytes") or 0))
        result["received_bytes"] = received
        result["expected_bytes"] = expected
        if priority["state"] == "running" and expected > 0:
            # 100% is reserved for the verified, published R2 object.
            result["progress_percent"] = min(99, received * 100 // expected)
    return result


async def status(g, uid, media):
    row = await _authorized_media(g, uid, media)
    job = None
    if row["state"] != "ready" and active(g):
        job = await g.db().fetchrow(
            """SELECT state,next_attempt,requested_at,received_bytes,
                      expected_bytes,progress_at
               FROM wg_media_priority WHERE media=$1""", row["id"])
    result = _result(g, uid, row, job)
    requested_at = dict(job).get("requested_at") if job else None
    if job and job["state"] == "queued" and requested_at is not None:
        # A position is approximate when other users have multiple requests.
        stats = await g.db().fetchrow("""
            SELECT COUNT(*) FILTER(
                     WHERE state='queued' AND requested_at<=$1)::int AS position,
                   COUNT(*) FILTER(WHERE state='queued')::int AS total
            FROM wg_media_priority
        """, requested_at)
        if stats and "position" in dict(stats):
            result["queue_position"] = int(stats["position"] or 0)
            result["queue_total"] = int(stats["total"] or 0)
    return result


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
        last_error=$3,received_bytes=0,progress_at=0,started_at=0
      WHERE media=$1
    """, media, time.time()+max(2, float(seconds)), str(error)[:90])


async def report_playback_failure(g, uid, media):
    """One diagnosed playback failure, never an extra Telegram connection.

    Pending originals use the existing queue. For a file marked ready, inspect
    its R2 size (HEAD only). Requeue it only if integrity is demonstrably bad.
    A device-specific decode or transient buffering error cannot invalidate a
    valid original or force another 1-GB Telegram download.
    """
    row=await _authorized_media(g,uid,media)
    if row["kind"]!="video":
        raise web.HTTPBadRequest(text="Solo videos.")
    if row["state"]=="preview":
        result=await request(g,uid,media)
        return {"ok":True,"requeued":not result.get("available",False)}
    original=row.get("file_key")
    if not original:
        return {"ok":True,"requeued":False}
    try:
        size=await g.storage.object_size(original)
    except Exception:
        # An R2 outage is not proof the original is corrupt.
        log.warning("Video playback check pending (%s)",row["id"])
        return {"ok":True,"requeued":False}
    indexed=int(row.get("size") or 0)
    damaged=(size is None or size<=4096 or (indexed>0 and size!=indexed))
    if not damaged:
        log.info("Video playback client error: R2 verified media=%s bytes=%s",
                 row["id"],size)
        return {"ok":True,"requeued":False}
    # Only provably invalid copies are sent back through the existing
    # priority worker; its source selection, locks and parallelism are intact.
    changed=await g.db().fetchval("""
      UPDATE wg_media SET state='preview',error='playback_integrity_failed',
        available=0 WHERE id=$1 AND state='ready' AND kind='video'
      RETURNING id
    """,row["id"])
    if changed:
        log.warning("Video original requeued after failed integrity media=%s",row["id"])
    result=await request(g,uid,media)
    return {"ok":True,"requeued":not result.get("available",False)}


async def _select_source(g, folder):
    """Prefer the known source, then another previously verified free account.

    All accounts still use the original account_lock (wait=False), including
    DATOS. Administratively pinned accounts are never silently changed.
    """
    first = await sync.choose_account(g, folder)
    candidates = [first] if first is not None else []
    if folder.get("preferred_uid") is None:
        verified = await g.db().fetch("""
            SELECT d.uid FROM wg_discovery d
            LEFT JOIN wg_account_cooldown c ON c.uid=d.uid
            WHERE d.folder=$1 AND d.works
              AND COALESCE(c.available_at,0)<=$2
            ORDER BY d.tried_at DESC LIMIT 16
        """, folder["id"], time.time())
        candidates.extend(int(entry["uid"]) for entry in verified)
    seen = set()
    reserved = getattr(g, "priority_accounts_in_use", None)
    lock = getattr(g, "priority_source_lock", None)
    for uid in candidates:
        if uid is None or uid in seen:
            continue
        seen.add(uid)
        source = g.s if uid > 0 else getattr(g.s, "datos", None)
        if source is None or (uid < 0 and not getattr(source, "ready", False)):
            continue
        if uid < 0:
            # DESCARGAS and DATOS can contain the same Telegram UID.
            # Their independent DB locks would not protect each other:
            # never open that real account through both systems at once.
            duplicate = await g.db().fetchval("""
                SELECT EXISTS(SELECT 1 FROM descarga_accounts
                              WHERE uid=$1 AND session IS NOT NULL)
            """, -uid)
            if duplicate:
                continue
        if reserved is not None and uid in reserved:
            continue
        if not await sync.source_free(g, uid, folder["id"]):
            continue
        if lock is not None and reserved is not None:
            async with lock:
                if uid in reserved:
                    continue
                reserved.add(uid)
        return uid
    return None


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
        if media in (132224,132225):
            log.warning("Video recovery skipped id=%s state=%s",
                        media,dict(row).get("state") if row else "missing")
        await g.db().execute("DELETE FROM wg_media_priority WHERE media=$1", media)
        return
    if row["state"] != "preview" or row["kind"] != "video":
        await g.db().execute("DELETE FROM wg_media_priority WHERE media=$1", media)
        return

    folder = dict(row)
    folder["id"] = row["folder"]
    uid = None
    try:
        if not active(g):
            raise sync.PauseRequested("priority_disabled")
        uid = await _select_source(g, folder)
        if uid is None:
            if media in (132224,132225):
                log.warning("Video recovery id=%s source_busy_or_unavailable",media)
            raise sync.PauseRequested("source_busy_or_unavailable")
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
            # Real bytes from Telethon; never a simulated clock-based counter.
            document = getattr(message, "document", None)
            expected = max(0, int(getattr(document, "size", 0) or
                                  row["size"] or 0))
            if media in (132224,132225):
                log.warning("Video recovery source id=%s telegram_bytes=%s indexed_bytes=%s media_type=%s",
                            media,int(getattr(document,"size",0) or 0),
                            row["size"],type(getattr(message,"media",None)).__name__)
            await g.db().execute("""
                UPDATE wg_media_priority
                SET expected_bytes=$2,received_bytes=0,progress_at=$3
                WHERE media=$1 AND state='running'
            """, media, expected, time.time())
            last_report = 0.0
            last_received = 0

            async def transfer_progress(received):
                nonlocal last_report, last_received
                amount = max(0, int(received or 0))
                moment = time.monotonic()
                if moment-last_report < 0.85 and amount-last_received < 4*1024*1024:
                    return
                last_report = moment
                last_received = amount
                await g.db().execute("""
                    UPDATE wg_media_priority SET
                      received_bytes=$2,expected_bytes=$3,progress_at=$4
                    WHERE media=$1 AND state='running'
                """, media, amount, expected, time.time())

            await sync.backup_one(
                g, folder["id"], dict(row), message, client, guard,
                progress=transfer_progress)
        ready = await g.db().fetchval(
            "SELECT state='ready' FROM wg_media WHERE id=$1", media)
        if ready:
            if media in (132224,132225):
                size=await g.db().fetchval("SELECT size FROM wg_media WHERE id=$1",media)
                log.warning("Video recovery complete id=%s final_bytes=%s",media,size)
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
    except sync.PauseRequested as exc:
        if media in (132224,132225):
            log.warning("Video recovery deferred id=%s reason=%s",
                        media,str(exc)[:75])
        await _defer(g, media, type(exc).__name__, 8)
    except ValueError as exc:
        if media in (132224,132225):
            log.warning("Video recovery invalid id=%s reason=%s",
                        media,str(exc)[:90])
        # Invalid/short Telegram data is not a busy account. Exponential
        # backoff avoids repeated 1-KiB retries and unnecessary API load.
        attempts=await g.db().fetchval(
            "SELECT attempts FROM wg_media_priority WHERE media=$1",media)
        await _defer(g, media, "invalid_or_incomplete_original",
                     min(600,60*2**min(4,int(attempts or 0))))
    except Exception as exc:
        count = await g.db().fetchval(
            "SELECT attempts FROM wg_media_priority WHERE media=$1", media)
        delay = min(600, 15 * 2 ** min(5, int(count or 0)))
        await _defer(g, media, type(exc).__name__, delay)
        log.warning("Priority video deferred (%s)", type(exc).__name__)
    finally:
        reserved = getattr(g, "priority_accounts_in_use", None)
        if reserved is not None and uid is not None:
            reserved.discard(uid)


async def run(g):
    """Bounded streams, durable 500-slot queue, fair across active sources."""
    db = g.db()
    await db.execute(SCHEMA)
    try:
        parallel = max(1, min(MAX_PARALLEL,
            int(os.getenv("GALERIAS_PRIORITY_PARALLEL", str(DEFAULT_PARALLEL)))))
    except (ValueError, TypeError):
        parallel = DEFAULT_PARALLEL
    running = {}
    # Reservations avoid waking several transfers on the same Telegram
    # account at once. The original PostgreSQL lock remains authoritative.
    g.priority_accounts_in_use = set()
    g.priority_source_lock = asyncio.Lock()
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
            g.priority_leader_ready.set()
            log.warning(
                "On-demand gallery video priority active; parallel=%d capacity=%d",
                parallel, MAX_GLOBAL_PENDING)
            last_queue_report = 0.0
            while not g.closed:
                if not g.storage.ready or os.getenv("GALERIAS_SYNC_ENABLED","0")!="1":
                    await asyncio.sleep(5)
                    continue
                try:
                    # Bounded durable queue; don't accumulate abandoned work.
                    await db.execute("""
                        DELETE FROM wg_media_priority
                        WHERE (requested_at<$1 AND state='queued')
                           OR media IN (SELECT id FROM wg_media
                                        WHERE state NOT IN ('preview','pending'))
                    """, time.time()-JOB_TTL_SECONDS)
                    available = parallel-len(running)
                    if available > 0:
                        # Respect a fixed account, or at most the number of
                        # previously verified sources for an unpinned folder.
                        # A burst of 250 clicks from one folder with one source
                        # therefore creates just ONE active Telegram transfer.
                        # First requests from different users are scheduled
                        # before any user's second/third/fourth request.
                        due = await db.fetch("""
                            WITH eligible AS (
                              SELECT q.media,q.requested_at,m.folder,
                                CASE WHEN f.preferred_uid IS NOT NULL THEN 1
                                ELSE LEAST($3::int,GREATEST(1,
                                  (SELECT COUNT(*)::int FROM wg_discovery d
                                   WHERE d.folder=m.folder AND d.works))) END
                                  AS folder_capacity,
                                ROW_NUMBER() OVER(
                                  PARTITION BY m.folder
                                  ORDER BY q.requested_at ASC,q.media) AS folder_rank,
                                ROW_NUMBER() OVER(
                                  PARTITION BY q.requester
                                  ORDER BY q.requested_at ASC,q.media) AS user_round
                              FROM wg_media_priority q
                              JOIN wg_media m ON m.id=q.media
                              JOIN wg_folders f ON f.id=m.folder
                              WHERE q.state='queued' AND q.next_attempt<=$1
                                AND m.state='preview' AND m.kind='video'
                                AND f.active
                            ), next AS (
                              SELECT e.media FROM eligible e
                              WHERE e.folder_rank <= GREATEST(0,e.folder_capacity-
                                (SELECT COUNT(*)::int FROM wg_media_priority r
                                 JOIN wg_media mr ON mr.id=r.media
                                 WHERE r.state='running' AND mr.folder=e.folder))
                              ORDER BY e.user_round,e.requested_at ASC
                              LIMIT $2
                            )
                            UPDATE wg_media_priority p SET
                              state='running',received_bytes=0,
                              progress_at=0,started_at=$1
                            FROM next WHERE p.media=next.media AND p.state='queued'
                            RETURNING p.media
                        """, time.time(), available, parallel)
                        for record in due:
                            media=int(record["media"])
                            task=asyncio.create_task(_process(g,media))
                            running[media]=task
                            task.add_done_callback(
                                lambda _task, mid=media: running.pop(mid, None))
                    if time.monotonic()-last_queue_report>=120:
                        snapshot=await db.fetchrow("""
                            SELECT COUNT(*) FILTER(WHERE state='queued')::int AS queued,
                                   COUNT(*) FILTER(WHERE state='running')::int AS running
                            FROM wg_media_priority
                        """)
                        log.warning(
                            "On-demand video capacity: queued=%d running=%d slots=%d max_queue=%d",
                            int(snapshot["queued"] or 0),int(snapshot["running"] or 0),
                            parallel,MAX_GLOBAL_PENDING)
                        last_queue_report=time.monotonic()
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Priority supervisor retry (%s)", type(exc).__name__)
                    await asyncio.sleep(8)
        finally:
            g.priority_leader_ready.clear()
            for task in tuple(running.values()):
                task.cancel()
            if running:
                await asyncio.gather(*tuple(running.values()),
                                     return_exceptions=True)
            await leader.execute("SELECT pg_advisory_unlock(784,22)")
