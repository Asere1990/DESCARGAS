"""Second, optional Telegram -> R2 gallery feed. No linked user sessions or original sync changes.

TOKEN_WEB is exclusively consumed here. One Group ID maps to one public folder;
assignments are atomic and server-side unique, and existing R2 media persists.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
import uuid
from typing import Any

import aiohttp
import asyncpg
from aiohttp import web

LOG = logging.getLogger("web-galerias-bot-feed")
MAX_FILE_BYTES = 20_000_000 - 1
MAX_THUMB_BYTES = 2 * 1024 * 1024
LOCK_ID = 721_804_332_198
SCHEMA = """
CREATE TABLE IF NOT EXISTS wg_bot_feed_routes(
  folder BIGINT PRIMARY KEY REFERENCES wg_folders(id) ON DELETE CASCADE,
  group_id BIGINT NOT NULL UNIQUE,
  revision UUID NOT NULL,
  linked_at DOUBLE PRECISION NOT NULL,
  updated DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS wg_bot_feed_state(
  bot_id BIGINT PRIMARY KEY,
  cursor BIGINT NOT NULL DEFAULT 0
);
CREATE SEQUENCE IF NOT EXISTS wg_bot_feed_message_seq START WITH 1;
CREATE TABLE IF NOT EXISTS wg_bot_feed_items(
  group_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  folder BIGINT NOT NULL REFERENCES wg_folders(id),
  revision UUID NOT NULL,
  message_date DOUBLE PRECISION NOT NULL,
  file_id TEXT NOT NULL,
  thumb_file_id TEXT,
  file_size BIGINT NOT NULL DEFAULT 0,
  caption TEXT NOT NULL DEFAULT '',
  grouped TEXT,
  kind TEXT NOT NULL CHECK(kind IN ('photo','video')),
  source_type TEXT NOT NULL,
  mime TEXT NOT NULL,
  filename TEXT NOT NULL,
  width INTEGER NOT NULL DEFAULT 0,
  height INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','processing','ready','skipped','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
  media_id BIGINT,
  error TEXT NOT NULL DEFAULT '',
  created DOUBLE PRECISION NOT NULL,
  updated DOUBLE PRECISION NOT NULL,
  PRIMARY KEY(group_id,message_id)
);
CREATE INDEX IF NOT EXISTS wg_bot_feed_due
 ON wg_bot_feed_items(status,next_attempt,created)
 WHERE status IN ('pending','processing');
CREATE INDEX IF NOT EXISTS wg_bot_feed_folder
 ON wg_bot_feed_items(folder,created DESC);
"""

class BotAPIError(RuntimeError):
    def __init__(self, method, status, code):
        super().__init__(f"Telegram {method} status {status} ({code})")
        self.status = int(status)

def token():
    return os.getenv("TOKEN_WEB", "").strip()

async def schema(g):
    if getattr(g, "_bot_feed_schema_ready", False):
        return
    lock = getattr(g, "_bot_feed_schema_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        g._bot_feed_schema_lock = lock
    async with lock:
        if not getattr(g, "_bot_feed_schema_ready", False):
            await g.db().execute(SCHEMA)
            g._bot_feed_schema_ready = True

def group_id(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        val = int(value)
    except (ValueError, TypeError, OverflowError):
        raise web.HTTPBadRequest(text="Escribe el Group ID negativo de Telegram.") from None
    if not -(2**63) < val < -1:
        raise web.HTTPBadRequest(text="Group ID no válido.")
    return val

async def telegram(session, bot_token, method, data=None):
    if not bot_token:
        raise RuntimeError("TOKEN_WEB no configurado.")
    url = "https://api.telegram.org/bot" + bot_token + "/" + method
    async with session.post(url, json=data or {}) as response:
        body = await response.json(content_type=None)
        if response.status != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            raise BotAPIError(method, response.status, (body or {}).get("error_code", 0))
        return body.get("result")

def media_from_update(update):
    """Extract all newly posted photos/videos, including forwarded messages and albums."""
    msg = update.get("message") or update.get("channel_post")
    if not isinstance(msg, dict):
        return None
    chat = msg.get("chat") or {}
    if chat.get("type") not in ("group", "supergroup", "channel"):
        return None
    try:
        gid, mid = int(chat["id"]), int(msg["message_id"])
        posted = float(msg["date"])
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    if gid >= 0 or mid <= 0:
        return None
    kind = None
    source_type = None
    original = None
    filename = ""
    mime = ""
    thumb = None
    if msg.get("photo"):
        sizes = [p for p in msg["photo"] if isinstance(p, dict) and p.get("file_id")]
        if not sizes:
            return None
        original = max(sizes, key=lambda p: (int(p.get("file_size") or 0), int(p.get("width") or 0)))
        thumb = min(sizes, key=lambda p: int(p.get("file_size") or 0))
        kind, source_type, mime, filename = "photo", "photo", "image/jpeg", "photo.jpg"
    elif isinstance(msg.get("video"), dict):
        original = msg["video"]
        thumb = original.get("thumbnail") or original.get("thumb")
        kind, source_type = "video", "video"
        mime, filename = "video/mp4", str(original.get("file_name") or "video.mp4")
    elif isinstance(msg.get("animation"), dict):
        original = msg["animation"]
        thumb = original.get("thumbnail") or original.get("thumb")
        kind, source_type = "video", "gif"
        mime, filename = "video/mp4", str(original.get("file_name") or "animation.mp4")
    elif isinstance(msg.get("document"), dict):
        original = msg["document"]
        mime = str(original.get("mime_type") or "").lower()
        if mime in ("image/jpeg","image/png","image/webp","image/gif"):
            kind, source_type = "photo", "photo_file"
        elif mime in ("video/mp4","video/quicktime","video/webm"):
            kind, source_type = "video", "video_file"
        else:
            return None
        thumb = original.get("thumbnail") or original.get("thumb")
        filename = str(original.get("file_name") or "archivo")
    else:
        return None

    file_id = original.get("file_id")
    if not isinstance(file_id, str) or not file_id or len(file_id) > 512:
        return None
    caption = str(msg.get("caption") or "")[:4000]
    file_size = max(0, int(original.get("file_size") or 0))
    grouped = str(msg.get("media_group_id") or "")[:100]
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:100].strip("._")
    if not filename:
        filename = "foto.jpg" if kind == "photo" else "video.mp4"
    return dict(
        group_id=gid, message_id=mid, message_date=posted, file_id=file_id,
        thumb_file_id=thumb.get("file_id") if isinstance(thumb, dict) else None,
        file_size=file_size, caption=caption, grouped=grouped or None,
        kind=kind, source_type=source_type, mime=mime,
        filename=filename, width=int(original.get("width") or 0),
        height=int(original.get("height") or 0)
    )

async def verify_membership(session, bot_token, gid):
    bot = await telegram(session, bot_token, "getMe")
    info = await telegram(session, bot_token, "getChat", {"chat_id": gid})
    if info.get("type") not in ("group","supergroup","channel"):
        raise web.HTTPBadRequest(text="El ID no corresponde a un grupo o canal.")
    member = await telegram(session, bot_token, "getChatMember", {
        "chat_id": gid, "user_id": int(bot["id"])
    })
    status = member.get("status")
    if status not in ("creator","administrator","member"):
        raise web.HTTPBadRequest(text="Añade primero el bot al grupo de Telegram.")
    if info.get("type") == "channel" and status not in ("creator","administrator"):
        raise web.HTTPBadRequest(text="El bot debe ser administrador del canal.")
    if status == "member" and not bot.get("can_read_all_group_messages", False):
        raise web.HTTPBadRequest(text=(
            "Haz al bot administrador del grupo o desactiva su privacidad en BotFather."
        ))
    return int(bot["id"])

async def route_operation(g, actor, op, data):
    await schema(g)
    try:
        folder = int(data.get("folder"))
    except (ValueError, TypeError, OverflowError):
        raise web.HTTPBadRequest(text="Carpeta incorrecta.") from None
    if folder <= 0:
        raise web.HTTPBadRequest(text="Carpeta incorrecta.")
    existing = await g.db().fetchval("""
      SELECT id FROM wg_folders
      WHERE id=$1 AND active
        AND NOT EXISTS(SELECT 1 FROM wg_folder_sources WHERE source_folder=$1)
    """, folder)
    if not existing:
        raise web.HTTPNotFound(text="Carpeta principal no encontrada.")
    if op == "admin-bot-feed-remove":
        deleted = await g.db().fetchval("""
          DELETE FROM wg_bot_feed_routes WHERE folder=$1 RETURNING group_id
        """, folder)
        if deleted is not None:
            await g.log(actor, "admin-bot-feed-remove")
        return {"ok": True, "removed": deleted is not None, "saved_media_preserved": True}
    if op != "admin-bot-feed-set":
        raise web.HTTPBadRequest(text="Acción desconocida.")
    gid = group_id(data.get("group_id"))
    if not token():
        raise web.HTTPServiceUnavailable(text="Configura TOKEN_WEB en CLOUDFLARE primero.")
    # A traditional Telegram source may already belong to another folder.
    # A new Group ID must not break the one-group/one-folder rule even when
    # that group is supplied via the existing, untouchable reader route.
    legacy_owner = await g.db().fetchval("""
      SELECT COALESCE(link.parent_folder, f.id)
      FROM wg_folders f
      LEFT JOIN wg_folder_sources link ON link.source_folder=f.id
      WHERE f.peer=$1 AND f.active LIMIT 1
    """, gid)
    if legacy_owner is not None and int(legacy_owner) != folder:
        raise web.HTTPConflict(text=(
            "Ese Group ID ya alimenta otra carpeta por la vía tradicional."
        ))
    current = await g.db().fetchrow(
        "SELECT group_id FROM wg_bot_feed_routes WHERE folder=$1", folder)
    if current and int(current["group_id"]) == gid:
        return {"ok": True, "folder": folder, "group_id": gid, "unchanged": True}

    timeout = aiohttp.ClientTimeout(total=12)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await verify_membership(session, token(), gid)
    except (aiohttp.ClientError, asyncio.TimeoutError, BotAPIError) as exc:
        LOG.warning("Unable to verify bot membership (%s)", type(exc).__name__)
        raise web.HTTPBadRequest(text=(
            "El bot no pudo confirmar acceso al grupo; revisa el ID y sus permisos."
        )) from None

    now = time.time()
    try:
        await g.db().execute("""
          INSERT INTO wg_bot_feed_routes(folder,group_id,revision,linked_at,updated)
          VALUES($1,$2,$3,$4,$4)
          ON CONFLICT(folder) DO UPDATE SET
            group_id=EXCLUDED.group_id,revision=EXCLUDED.revision,
            linked_at=EXCLUDED.linked_at,updated=EXCLUDED.updated
        """, folder, gid, uuid.uuid4(), now)
    except asyncpg.UniqueViolationError:
        raise web.HTTPConflict(text=(
            "Ese Group ID ya está vinculado a otra carpeta de Web Galerías."
        )) from None
    await g.log(actor, "admin-bot-feed-set")
    return {"ok": True, "folder": folder, "group_id": gid, "saved_media_preserved": True}

async def routes(g):
    await schema(g)
    rows = await g.db().fetch("""
      SELECT r.folder,r.group_id,
        count(i.message_id) FILTER (WHERE i.status='ready')::int AS ready,
        count(i.message_id) FILTER (WHERE i.status IN ('pending','processing'))::int AS queued,
        count(i.message_id) FILTER (WHERE i.status='skipped')::int AS skipped,
        count(i.message_id) FILTER (WHERE i.status='failed')::int AS failed
      FROM wg_bot_feed_routes r
      LEFT JOIN wg_bot_feed_items i ON i.group_id=r.group_id
      GROUP BY r.folder,r.group_id ORDER BY r.folder
    """)
    return [dict(folder=int(r["folder"]),group_id=int(r["group_id"]),
                 ready=int(r["ready"]), queued=int(r["queued"]),
                 skipped=int(r["skipped"]),failed=int(r["failed"])) for r in rows]

def storage_keys(item):
    gid = abs(int(item["group_id"]))
    mid = int(item["message_id"])
    mime = item["mime"]
    ext = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/gif": ".gif", "video/mp4": ".mp4",
        "video/quicktime": ".mov", "video/webm": ".webm",
    }.get(mime, ".mp4" if item["kind"] == "video" else ".jpg")
    prefix = f"botfeed/{gid}/{mid}"
    return prefix + "/original" + ext, prefix + "/thumb.jpg"

def _file_url(bot_token, file_path):
    if not isinstance(file_path, str) or len(file_path) > 450:
        raise ValueError("Ruta de Telegram inválida.")
    if file_path.startswith("/") or ".." in file_path.split("/") or not re.fullmatch(
        r"[A-Za-z0-9_./-]+", file_path
    ):
        raise ValueError("Ruta de Telegram inválida.")
    return "https://api.telegram.org/file/bot" + bot_token + "/" + file_path

async def file_info(session, bot_token, file_id):
    info = await telegram(session, bot_token, "getFile", {"file_id": file_id})
    size = int(info.get("file_size") or 0)
    if size < 1 or size > MAX_FILE_BYTES:
        raise ValueError("Archivo excede el límite de descarga de Telegram de 20 MB.")
    path = info.get("file_path")
    return _file_url(bot_token, path), size

async def file_chunks(session, url, *, limit):
    count = 0
    async with session.get(url) as response:
        if response.status != 200:
            raise BotAPIError("getFile-content", response.status, response.status)
        async for chunk in response.content.iter_chunked(256 * 1024):
            count += len(chunk)
            if count > limit:
                raise ValueError("Archivo Telegram mayor que el límite permitido.")
            yield chunk

async def image_preview(session, bot_token, file_id):
    if not file_id:
        return None
    try:
        url, size = await file_info(session, bot_token, file_id)
        if size > MAX_THUMB_BYTES:
            return None
        parts = []
        total = 0
        async for chunk in file_chunks(session, url, limit=MAX_THUMB_BYTES):
            total += len(chunk)
            parts.append(chunk)
        if total != size:
            return None
        return await asyncio.to_thread(_jpeg_thumb, b"".join(parts))
    except (aiohttp.ClientError, BotAPIError, ValueError, OSError) as exc:
        LOG.info("Telegram thumbnail unavailable (%s)", type(exc).__name__)
        return None

def _jpeg_thumb(raw):
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as im:
        im.thumbnail((440, 440))
        image = im.convert("RGB")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=80, optimize=True)
        return output.getvalue()

def _fallback_thumb():
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (320, 320), (28, 33, 42))
    draw = ImageDraw.Draw(image)
    draw.polygon(((123, 92), (123, 228), (233, 160)), fill=(235, 238, 245))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=78)
    return output.getvalue()

async def route_current(g, item):
    return bool(await g.db().fetchval("""
      SELECT 1 FROM wg_bot_feed_routes r JOIN wg_folders f ON f.id=r.folder
      WHERE r.folder=$1 AND r.group_id=$2 AND r.revision=$3 AND f.active
    """, item["folder"], item["group_id"], item["revision"]))

async def mark_skipped(g, item, reason):
    await g.db().execute("""
      UPDATE wg_bot_feed_items SET status='skipped',error=$3,updated=$4
      WHERE group_id=$1 AND message_id=$2 AND status='processing'
    """, item["group_id"], item["message_id"], reason[:150], time.time())

async def process_item(g, session, bot_token, item):
    if not await route_current(g, item):
        await mark_skipped(g, item, "Group ID desconectado o reasignado")
        return
    if int(item["file_size"]) > MAX_FILE_BYTES:
        await mark_skipped(g, item, "Archivo superior a 20 MB")
        return
    original_key, thumb_key = storage_keys(item)
    url, telegram_size = await file_info(session, bot_token, item["file_id"])
    if int(item["file_size"]) > 0 and telegram_size != int(item["file_size"]):
        raise ValueError("Tamaño de archivo inconsistente.")
    stored = await g.storage.object_size(original_key)
    original_photo = None
    if stored != telegram_size:
        if item["kind"] == "photo" and telegram_size <= 4 * 1024 * 1024:
            # Compressed Telegram photos need only ONE getFile download and
            # one PUT to R2. Derive the actual thumbnail from the same bytes.
            parts = []
            downloaded = 0
            async for chunk in file_chunks(session, url, limit=MAX_FILE_BYTES):
                downloaded += len(chunk)
                parts.append(chunk)
            if downloaded != telegram_size:
                raise ValueError("Foto Telegram incompleta.")
            if not await route_current(g, item):
                raise ValueError("Group ID retirado durante la descarga.")
            original_photo = b"".join(parts)
            await asyncio.to_thread(
                g.storage._client().put_object,
                Bucket=g.storage.bucket, Key=original_key,
                Body=original_photo, ContentType=item["mime"],
                CacheControl="private, max-age=3600"
            )
            saved = await g.storage.object_size(original_key)
            if saved != telegram_size:
                raise ValueError("Cloudflare R2 no confirmó el archivo original.")
        else:
            # Even large 20MB clips use bounded streaming and backpressure.
            # Throttle route checks to avoid ~80 SQL queries per upload.
            checked_at = 0.0
            async def still_current():
                nonlocal checked_at
                now = time.monotonic()
                if now - checked_at < 3:
                    return
                checked_at = now
                if not await route_current(g, item):
                    raise ValueError("Group ID retirado durante la descarga.")
            await g.storage.upload_stream(
                file_chunks(session, url, limit=MAX_FILE_BYTES), original_key,
                item["mime"], check=still_current, expected_size=telegram_size
            )
    if not await g.storage.exists(thumb_key):
        raw = None
        if original_photo is not None:
            try:
                raw = await asyncio.to_thread(_jpeg_thumb, original_photo)
            except (OSError, ValueError):
                pass
        if not raw:
            raw = await image_preview(session, bot_token, item["thumb_file_id"])
        if not raw:
            raw = await asyncio.to_thread(_fallback_thumb)
        await asyncio.to_thread(
            g.storage._client().put_object, Bucket=g.storage.bucket,
            Key=thumb_key, Body=raw, ContentType="image/jpeg",
            CacheControl="private, max-age=3600"
        )
    from .core import normal
    label = str(item["caption"] or "")
    name = str(item["filename"])
    search = normal((label + " " + name).strip())
    now = time.time()
    async with g.db().acquire() as conn:
        async with conn.transaction():
            route = await conn.fetchval("""
              SELECT 1 FROM wg_bot_feed_routes r JOIN wg_folders f ON f.id=r.folder
              WHERE r.folder=$1 AND r.group_id=$2 AND r.revision=$3 AND f.active
              FOR SHARE OF r
            """, item["folder"], item["group_id"], item["revision"])
            if not route:
                await conn.execute("""
                  UPDATE wg_bot_feed_items SET status='skipped',
                    error='Group ID reasignado',updated=$3
                  WHERE group_id=$1 AND message_id=$2 AND status='processing'
                """, item["group_id"], item["message_id"], now)
                return
            recorded = await conn.fetchrow("""
              SELECT status, media_id FROM wg_bot_feed_items
              WHERE group_id=$1 AND message_id=$2 FOR UPDATE
            """, item["group_id"], item["message_id"])
            if not recorded or recorded["status"] != "processing":
                return
            # Dedicated negative range. Does not collide with Telegram messages
            # or manual R2 uploads. No change to old folders or their cursors.
            mid = await conn.fetchval("SELECT nextval('wg_bot_feed_message_seq')")
            unique_message = -(2**62) + int(mid)
            media_id = await conn.fetchval("""
              INSERT INTO wg_media(
                folder,message,kind,mime,filename,width,height,size,date,
                label,search_text,reply_to,grouped,thumb_key,file_key,state,
                created,source_type)
              VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,
                $10,$11,NULL,$12,$13,$14,'ready',$15,$16)
              RETURNING id
            """,
                item["folder"], unique_message, item["kind"], item["mime"],
                name, item["width"], item["height"], telegram_size,
                float(item["message_date"]), label, search,
                (f"bot:{abs(item['group_id'])}:{item['grouped']}"
                 if item["grouped"] else None),
                thumb_key, original_key, now, item["source_type"])
            await conn.execute("""
              UPDATE wg_bot_feed_items SET status='ready',media_id=$3,
                error='',updated=$4 WHERE group_id=$1 AND message_id=$2
                AND status='processing'
            """, item["group_id"], item["message_id"], media_id, now)
    LOG.info("Bot feed media ready in gallery folder %s", item["folder"])

async def claim_item(g):
    return await g.db().fetchrow("""
      WITH candidate AS (
        SELECT group_id,message_id FROM wg_bot_feed_items
        WHERE attempts<8 AND (
          (status='pending' AND next_attempt<=$1)
          OR (status='processing' AND updated<$1-900))
        ORDER BY created,group_id,message_id
        LIMIT 1 FOR UPDATE SKIP LOCKED
      )
      UPDATE wg_bot_feed_items i
      SET status='processing',attempts=attempts+1,updated=$1
      FROM candidate c WHERE i.group_id=c.group_id AND i.message_id=c.message_id
      RETURNING i.*
    """, time.time())

async def fail_item(g, item, error):
    attempts = int(item["attempts"])
    delay = min(300, 5 * 2 ** min(attempts, 6))
    await g.db().execute("""
      UPDATE wg_bot_feed_items
      SET status=$3,error=$4,next_attempt=$5,updated=$6
      WHERE group_id=$1 AND message_id=$2 AND status='processing'
    """, item["group_id"],item["message_id"],
       "failed" if attempts >= 8 else "pending",
       str(error)[:140], time.time() + delay, time.time())

async def consumer(g, session, bot_token):
    while not g.closed:
        try:
            item = await claim_item(g)
            if not item:
                await asyncio.sleep(2)
                continue
            try:
                await process_item(g, session, bot_token, item)
            except asyncio.CancelledError:
                raise
            except ValueError as exc:
                # Unsupported/oversize files are explicitly recorded, not retried
                # forever. Transient errors retain the inbox for safe reprocessing.
                if "límite" in str(exc) or "20 MB" in str(exc):
                    await mark_skipped(g, item, "Archivo incompatible o superior a 20 MB")
                else:
                    await fail_item(g, item, type(exc).__name__)
            except (aiohttp.ClientError, asyncio.TimeoutError, BotAPIError) as exc:
                await fail_item(g, item, type(exc).__name__)
            except Exception as exc:
                LOG.warning("Bot feed item deferred (%s)", type(exc).__name__)
                await fail_item(g, item, type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Bot feed queue unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(5)

async def polling(g, session, bot_token, bot_id):
    await g.db().execute("""
      INSERT INTO wg_bot_feed_state(bot_id,cursor) VALUES($1,0)
      ON CONFLICT DO NOTHING
    """, bot_id)
    cursor = int(await g.db().fetchval(
        "SELECT cursor FROM wg_bot_feed_state WHERE bot_id=$1", bot_id))
    while not g.closed:
        try:
            updates = await telegram(session, bot_token, "getUpdates", {
                "offset": cursor + 1, "limit": 100, "timeout": 20,
                "allowed_updates": ["message", "channel_post"]
            })
            if not updates:
                continue
            latest = max(int(e.get("update_id") or 0) for e in updates)
            candidates = [media_from_update(e) for e in updates]
            candidates = [x for x in candidates if x is not None]
            gids = sorted(set(int(x["group_id"]) for x in candidates))
            now = time.time()
            async with g.db().acquire() as conn:
                async with conn.transaction():
                    mapping = {}
                    if gids:
                        rows = await conn.fetch("""
                          SELECT group_id,folder,revision,linked_at
                          FROM wg_bot_feed_routes WHERE group_id=ANY($1::bigint[])
                        """, gids)
                        mapping = {int(r["group_id"]): r for r in rows}
                    for item in candidates:
                        route = mapping.get(item["group_id"])
                        if not route or item["message_date"] + 1 < route["linked_at"]:
                            continue
                        await conn.execute("""
                          INSERT INTO wg_bot_feed_items(
                            group_id,message_id,folder,revision,message_date,
                            file_id,thumb_file_id,file_size,caption,grouped,
                            kind,source_type,mime,filename,width,height,
                            created,updated)
                          VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                                 $11,$12,$13,$14,$15,$16,$17,$17)
                          ON CONFLICT(group_id,message_id) DO NOTHING
                        """,
                            item["group_id"],item["message_id"],
                            route["folder"],route["revision"],
                            item["message_date"],item["file_id"],
                            item["thumb_file_id"],item["file_size"],
                            item["caption"],item["grouped"],item["kind"],
                            item["source_type"],item["mime"],item["filename"],
                            item["width"],item["height"],now)
                    await conn.execute("""
                      UPDATE wg_bot_feed_state
                      SET cursor=GREATEST(cursor,$2) WHERE bot_id=$1
                    """, bot_id, latest)
            cursor = max(cursor, latest)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never log token-bearing Telegram URLs.
            LOG.warning("Bot feed update receiver deferred (%s)", type(exc).__name__)
            await asyncio.sleep(5)

async def run(g):
    """Standalone optional bot consumer. Never starts/changes traditional sync."""
    bot_token = token()
    if not bot_token:
        return
    while not g.closed:
        try:
            await schema(g)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(
                total=55, connect=10, sock_read=40
            )) as session:
                me = await telegram(session, bot_token, "getMe")
                bot_id = int(me["id"])
                hook = await telegram(session, bot_token, "getWebhookInfo")
                if hook.get("url"):
                    LOG.warning("TOKEN_WEB has an existing webhook; cannot poll safely")
                    return
                async with g.db().acquire() as lock:
                    owned = await lock.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_ID)
                    if not owned:
                        await asyncio.sleep(6)
                        continue
                    try:
                        LOG.info("Dedicated TOKEN_WEB feed receiver ready")
                        receiver = asyncio.create_task(polling(g, session, bot_token, bot_id))
                        worker = asyncio.create_task(consumer(g, session, bot_token))
                        try:
                            done, pending = await asyncio.wait(
                                (receiver, worker),
                                return_when=asyncio.FIRST_EXCEPTION)
                            for task in done:
                                task.result()
                        finally:
                            receiver.cancel()
                            worker.cancel()
                            await asyncio.gather(receiver, worker, return_exceptions=True)
                    finally:
                        await lock.fetchval("SELECT pg_advisory_unlock($1)", LOCK_ID)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("TOKEN_WEB feed startup deferred (%s)", type(exc).__name__)
        await asyncio.sleep(10)
