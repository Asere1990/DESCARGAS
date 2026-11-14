"""Administrator-only, direct browser -> Cloudflare R2 uploads.

Independent of Telethon, DESCARGAS, DATOS and the gallery sync loop.  An
upload is never exposed to customers until its original AND thumbnail have
been confirmed in R2 and the existing wg_media index is updated.
"""
import asyncio
import logging
import math
import os
import time
import uuid
from pathlib import PurePath

from aiohttp import web
from botocore.exceptions import ClientError

LOG = logging.getLogger("web-galerias-manual-upload")
PART_SIZE = 16 * 1024 * 1024
MAX_SIZE = 2 * 1024 * 1024 * 1024
MAX_THUMB = 2 * 1024 * 1024
TYPES = {
    ".jpg": ("image/jpeg", "photo"),
    ".jpeg": ("image/jpeg", "photo"),
    ".png": ("image/png", "photo"),
    ".webp": ("image/webp", "photo"),
    ".heic": ("image/heic", "photo"),
    ".heif": ("image/heif", "photo"),
    ".avif": ("image/avif", "photo"),
    ".mp4": ("video/mp4", "video"),
    ".mov": ("video/quicktime", "video"),
    ".webm": ("video/webm", "video"),
}
SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS wg_manual_message_seq START WITH 1;
CREATE TABLE IF NOT EXISTS wg_manual_uploads(
  id UUID PRIMARY KEY,
  folder BIGINT NOT NULL REFERENCES wg_folders(id),
  actor BIGINT NOT NULL,
  filename TEXT NOT NULL,
  mime TEXT NOT NULL,
  kind TEXT NOT NULL,
  bytes BIGINT NOT NULL,
  object_key TEXT NOT NULL UNIQUE,
  thumb_key TEXT NOT NULL UNIQUE,
  multipart_id TEXT,
  status TEXT NOT NULL DEFAULT 'uploading',
  media_id BIGINT,
  width INTEGER NOT NULL DEFAULT 0,
  height INTEGER NOT NULL DEFAULT 0,
  created DOUBLE PRECISION NOT NULL,
  updated DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS wg_manual_uploads_actor
  ON wg_manual_uploads(actor,status,created);
"""

async def schema(g):
    if getattr(g, "_manual_upload_schema_ready", False):
        return
    lock = getattr(g, "_manual_upload_schema_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        g._manual_upload_schema_lock = lock
    async with lock:
        if not getattr(g, "_manual_upload_schema_ready", False):
            await g.db().execute(SCHEMA)
            g._manual_upload_schema_ready = True


def parse_filename(value):
    if not isinstance(value, str):
        raise web.HTTPBadRequest(text="Nombre de archivo inválido.")
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or len(name) > 180 or any(ord(char) < 32 for char in name):
        raise web.HTTPBadRequest(text="Nombre de archivo inválido.")
    ext = os.path.splitext(name)[1].lower()
    if ext not in TYPES:
        raise web.HTTPBadRequest(text="Formato no admitido: utiliza JPG, PNG, WEBP, MP4, MOV o WEBM.")
    mime, kind = TYPES[ext]
    return name, ext, mime, kind


def positive(value, field):
    try:
        number = int(value)
        if number <= 0:
            raise ValueError()
        return number
    except (ValueError, TypeError, OverflowError):
        raise web.HTTPBadRequest(text=f"{field} incorrecto.") from None


def upload_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise web.HTTPBadRequest(text="Identificador de subida incorrecto.") from None


async def row_for(g, uid, data):
    row = await g.db().fetchrow(
        "SELECT * FROM wg_manual_uploads WHERE id=$1 AND actor=$2",
        upload_uuid(data.get("upload")), uid)
    if not row:
        raise web.HTTPNotFound(text="Esta subida no pertenece a tu sesión.")
    return row


def presign(storage, method, key, **kwargs):
    params = {"Bucket": storage.bucket, "Key": key, **kwargs}
    return storage._client().generate_presigned_url(
        method, Params=params, ExpiresIn=900, HttpMethod="PUT")


async def r2_head(storage, key):
    from botocore.exceptions import ClientError
    try:
        return await asyncio.to_thread(
            storage._client().head_object, Bucket=storage.bucket, Key=key)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) in (
                "404", "NoSuchKey", "NotFound"):
            return None
        raise


def trusted_origins():
    configured = os.environ.get("GALERIAS_MANUAL_UPLOAD_ORIGINS", "")
    origins = ["https://cloudflare-t2gf.onrender.com"]
    for value in configured.split(","):
        origin = value.strip().rstrip("/")
        if origin.startswith("https://") and len(origin) < 200 and "/" not in origin[8:]:
            origins.append(origin)
    return list(dict.fromkeys(origins))


async def cors_ready(g):
    """Append an exact-origin PUT rule without replacing existing R2 rules.

    Only invoked by the administrator while opening the uploader. Never make
    the R2 bucket public, nor delete another application's CORS policy.
    """
    if getattr(g, "_manual_upload_cors_ready", False):
        return True
    client = g.storage._client()
    bucket = g.storage.bucket
    try:
        result = await asyncio.to_thread(client.get_bucket_cors, Bucket=bucket)
        rules = list(result.get("CORSRules") or [])
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) in (
                "NoSuchCORSConfiguration", "404", "NoSuchKey"):
            rules = []
        else:
            LOG.warning("R2 CORS inspection failed: %s", type(exc).__name__)
            return False
    origins = trusted_origins()
    changed = False
    for origin in origins:
        configured = any(
            (origin in rule.get("AllowedOrigins", []) or
             "*" in rule.get("AllowedOrigins", []))
            and "PUT" in rule.get("AllowedMethods", [])
            and ("*" in rule.get("AllowedHeaders", []) or
                 any(h.lower() == "content-type"
                     for h in rule.get("AllowedHeaders", [])))
            for rule in rules)
        if not configured:
            changed = True
            rules.append({
                "AllowedOrigins": [origin],
                "AllowedMethods": ["PUT"],
                "AllowedHeaders": ["Content-Type"],
                "ExposeHeaders": ["ETag"],
                "MaxAgeSeconds": 3600,
            })
    if not changed:
        g._manual_upload_cors_ready = True
        return True
    try:
        await asyncio.to_thread(
            client.put_bucket_cors, Bucket=bucket,
            CORSConfiguration={"CORSRules": rules})
        g._manual_upload_cors_ready = True
        return True
    except Exception as exc:
        LOG.warning("Direct uploads require R2 CORS configuration: %s",
                    type(exc).__name__)
        return False


async def info(g):
    if not g.storage.ready:
        return {"ready": False, "reason": "Cloudflare R2 no está configurado."}
    cors = await cors_ready(g)
    return {
        "ready": bool(cors),
        "reason": "" if cors else (
            "La clave R2 no pudo habilitar CORS. Hay que permitir PUT desde "
            "https://cloudflare-t2gf.onrender.com en el bucket privado."),
        "max_bytes": MAX_SIZE,
        "part_size": PART_SIZE,
        "types": sorted(TYPES),
    }


async def begin(g, uid, data):
    if not g.storage.ready:
        raise web.HTTPServiceUnavailable(text="Cloudflare R2 no está disponible.")
    if not await cors_ready(g):
        raise web.HTTPServiceUnavailable(
            text="Falta habilitar permisos CORS para subida directa a R2.")
    folder = positive(data.get("folder"), "Galería")
    folder_row = await g.db().fetchrow("""
        SELECT f.id FROM wg_folders f
        WHERE f.id=$1 AND f.active
          AND NOT EXISTS(SELECT 1 FROM wg_folder_sources s
                         WHERE s.source_folder=f.id)
    """, folder)
    if not folder_row:
        raise web.HTTPNotFound(text="Galería principal no encontrada.")
    name, ext, mime, kind = parse_filename(data.get("filename"))
    size = positive(data.get("size"), "Tamaño")
    if size > MAX_SIZE:
        raise web.HTTPBadRequest(text="Máximo 2 GiB por archivo.")
    active = await g.db().fetchval("""
        SELECT COUNT(*) FROM wg_manual_uploads
        WHERE actor=$1 AND status IN ('uploading','uploaded')
    """, uid)
    if active >= 50:
        raise web.HTTPTooManyRequests(
            text="Hay demasiadas subidas pendientes; reanuda las anteriores.")
    try:
        width = max(0, min(100000, int(data.get("width") or 0)))
        height = max(0, min(100000, int(data.get("height") or 0)))
    except (ValueError, TypeError, OverflowError):
        raise web.HTTPBadRequest(text="Dimensiones incorrectas.") from None
    ident = uuid.uuid4()
    key = f"manual/{folder}/{ident.hex}/original{ext}"
    thumb = f"manual/{folder}/{ident.hex}/thumb.jpg"
    multi = size > PART_SIZE
    multipart_id = None
    if multi:
        started = await asyncio.to_thread(
            g.storage._client().create_multipart_upload,
            Bucket=g.storage.bucket, Key=key, ContentType=mime,
            CacheControl="private, max-age=3600")
        multipart_id = started["UploadId"]
    try:
        await g.db().execute("""
            INSERT INTO wg_manual_uploads(
              id,folder,actor,filename,mime,kind,bytes,object_key,
              thumb_key,multipart_id,status,width,height,created,updated)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'uploading',$11,$12,$13,$13)
        """, ident, folder, uid, name, mime, kind, size, key, thumb,
             multipart_id, width, height, time.time())
    except BaseException:
        if multipart_id:
            try:
                await asyncio.to_thread(g.storage._client().abort_multipart_upload,
                    Bucket=g.storage.bucket, Key=key, UploadId=multipart_id)
            except Exception:
                pass
        raise
    return await describe(g, dict(id=ident, object_key=key, thumb_key=thumb,
                                  multipart_id=multipart_id, mime=mime,
                                  bytes=size, status="uploading", media_id=None))


async def list_parts(g, row):
    if not row["multipart_id"] or row["status"] != "uploading":
        return []
    try:
        result = await asyncio.to_thread(
            g.storage._client().list_parts,
            Bucket=g.storage.bucket,
            Key=row["object_key"], UploadId=row["multipart_id"])
        return result.get("Parts") or []
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) in (
                "NoSuchUpload", "404"):
            return []
        raise


async def describe(g, row, include_parts=False):
    result = {
        "upload": str(row["id"]),
        "multipart": bool(row["multipart_id"]),
        "part_size": PART_SIZE,
        "status": row["status"],
        "media": row.get("media_id") if isinstance(row, dict) else row["media_id"],
        "thumb_url": presign(g.storage, "put_object", row["thumb_key"],
                             ContentType="image/jpeg") if row["status"] == "uploading" else None,
    }
    if row["status"] == "uploading" and not row["multipart_id"]:
        result["url"] = presign(
            g.storage, "put_object", row["object_key"], ContentType=row["mime"])
    if include_parts and row["multipart_id"]:
        result["parts"] = [
            {"number": part["PartNumber"], "size": part["Size"]}
            for part in await list_parts(g, row)]
    return result


async def part_url(g, uid, data):
    row = await row_for(g, uid, data)
    if row["status"] != "uploading" or not row["multipart_id"]:
        raise web.HTTPConflict(text="Esta subida no admite partes nuevas.")
    part = positive(data.get("part"), "Parte")
    if part > math.ceil(row["bytes"] / PART_SIZE):
        raise web.HTTPBadRequest(text="Parte fuera del archivo.")
    return {"url": presign(g.storage, "upload_part", row["object_key"],
                           UploadId=row["multipart_id"], PartNumber=part)}


async def finish(g, uid, data):
    row = await row_for(g, uid, data)
    if row["status"] == "ready":
        return {"ok": True, "media": row["media_id"], "already": True}
    if row["status"] not in ("uploading", "uploaded"):
        raise web.HTTPConflict(text="La subida no está activa.")

    thumb = await r2_head(g.storage, row["thumb_key"])
    if not thumb or not 0 < int(thumb["ContentLength"]) <= MAX_THUMB:
        raise web.HTTPConflict(
            text="No se confirmó la miniatura. Vuelve a seleccionar el mismo archivo.")

    # If an earlier request completed R2 but lost its DB response, recover
    # from the verified HEAD instead of calling a consumed multipart ID.
    previously_completed = await r2_head(g.storage, row["object_key"])
    if (row["status"] == "uploading" and row["multipart_id"]
            and not (previously_completed and
                     int(previously_completed["ContentLength"]) == row["bytes"])):
        parts = await list_parts(g, row)
        expected_parts = math.ceil(row["bytes"] / PART_SIZE)
        if len(parts) != expected_parts or sum(int(p["Size"]) for p in parts) != row["bytes"]:
            raise web.HTTPConflict(text="Faltan partes del archivo; reanuda la subida.")
        if any(p["PartNumber"] != i + 1 or
               int(p["Size"]) != (PART_SIZE if i < expected_parts - 1
                                  else row["bytes"] - PART_SIZE * i)
               for i, p in enumerate(parts)):
            raise web.HTTPConflict(text="El archivo tiene partes incompletas.")
        await asyncio.to_thread(
            g.storage._client().complete_multipart_upload,
            Bucket=g.storage.bucket, Key=row["object_key"],
            UploadId=row["multipart_id"],
            MultipartUpload={"Parts": [
                {"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in parts]})

    original = await r2_head(g.storage, row["object_key"])
    if not original or int(original["ContentLength"]) != row["bytes"]:
        raise web.HTTPConflict(text="Cloudflare no confirmó el tamaño del archivo.")
    if row["status"] == "uploading":
        await g.db().execute("""
            UPDATE wg_manual_uploads SET status='uploaded',updated=$2
            WHERE id=$1 AND status='uploading'
        """, row["id"], time.time())
    async with g.db().acquire() as conn:
        async with conn.transaction():
            saved = await conn.fetchrow(
                "SELECT * FROM wg_manual_uploads WHERE id=$1 AND actor=$2 FOR UPDATE",
                row["id"], uid)
            if saved["media_id"]:
                return {"ok": True, "media": saved["media_id"], "already": True}
            sequence = await conn.fetchval("SELECT nextval('wg_manual_message_seq')")
            # Telegram message IDs are positive. Manual originals use a private
            # negative sequence, so Telegram indexing and cursor scans remain untouched.
            media = await conn.fetchval("""
                INSERT INTO wg_media (
                    folder,message,kind,mime,filename,width,height,size,date,
                    label,search_text,thumb_key,file_key,state,created,source_type)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'',$10,$11,$12,'ready',$9,$13)
                RETURNING id
            """, saved["folder"], -sequence, saved["kind"], saved["mime"],
                 saved["filename"], saved["width"], saved["height"],
                 saved["bytes"], time.time(), saved["filename"],
                 saved["thumb_key"], saved["object_key"],
                 "photo_file" if saved["kind"] == "photo" else "video_file")
            await conn.execute("""
                UPDATE wg_manual_uploads SET media_id=$2,status='ready',updated=$3
                WHERE id=$1
            """, saved["id"], media, time.time())
            return {"ok": True, "media": media}


async def abort(g, uid, data):
    row = await row_for(g, uid, data)
    if row["status"] == "ready":
        raise web.HTTPConflict(text="El archivo ya está publicado; utiliza la moderación.")
    if row["status"] == "aborted":
        return {"ok": True}
    if row["status"] == "uploading" and row["multipart_id"]:
        try:
            await asyncio.to_thread(
                g.storage._client().abort_multipart_upload,
                Bucket=g.storage.bucket, Key=row["object_key"],
                UploadId=row["multipart_id"])
        except ClientError:
            pass
    # Keys are unique to this draft, never shared with another gallery item.
    for key in (row["object_key"], row["thumb_key"]):
        try:
            await asyncio.to_thread(
                g.storage._client().delete_object,
                Bucket=g.storage.bucket, Key=key)
        except Exception:
            LOG.warning("Draft R2 cleanup retry needed")
    await g.db().execute("""
        UPDATE wg_manual_uploads SET status='aborted',updated=$2
        WHERE id=$1 AND status IN ('uploading','uploaded')
    """, row["id"], time.time())
    return {"ok": True}


async def operation(g, uid, op, data):
    if op == "admin-manual-config":
        await schema(g)
        return await info(g)
    await schema(g)
    if op == "admin-manual-start":
        return await begin(g, uid, data)
    if op == "admin-manual-status":
        row = await row_for(g, uid, data)
        return await describe(g, row, include_parts=True)
    if op == "admin-manual-part-url":
        return await part_url(g, uid, data)
    if op == "admin-manual-complete":
        return await finish(g, uid, data)
    if op == "admin-manual-abort":
        return await abort(g, uid, data)
    raise web.HTTPNotFound()
