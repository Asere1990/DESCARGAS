"""Portadas privadas y optimizadas de las galerías originales.

Estas imágenes son personalización visual; no modifican ningún original
de Telegram ni el respaldo de Cloudflare R2.
"""
import asyncio
import io
from aiohttp import web
from PIL import Image, ImageOps, UnidentifiedImageError

COVER_SCHEMA="""
CREATE TABLE IF NOT EXISTS wg_folder_covers(
 folder BIGINT PRIMARY KEY REFERENCES wg_folders(id) ON DELETE CASCADE,
 mime TEXT NOT NULL DEFAULT 'image/jpeg',
 body BYTEA NOT NULL,
 updated DOUBLE PRECISION NOT NULL);
"""

ALLOWED_MIME={"image/jpeg","image/png","image/webp"}
MAX_UPLOAD=5*1024*1024
MAX_PIXELS=16_000_000
MAX_EDGE=1200


def optimized_cover(blob,mime):
    """Produce JPEG no animado y reducido; limita archivos y píxeles dañinos."""
    if not isinstance(blob,(bytes,bytearray)) or not 0<len(blob)<=MAX_UPLOAD:
        raise web.HTTPBadRequest(text="La portada debe ocupar hasta 5 MB.")
    if mime not in ALLOWED_MIME:
        raise web.HTTPBadRequest(text="Usa una foto JPEG, PNG o WebP.")
    try:
        with Image.open(io.BytesIO(blob)) as raw:
            if raw.format not in {"JPEG","PNG","WEBP"}:
                raise web.HTTPBadRequest(text="El contenido no coincide con una imagen admitida.")
            real={"JPEG":"image/jpeg","PNG":"image/png","WEBP":"image/webp"}[raw.format]
            if real!=mime:
                raise web.HTTPBadRequest(text="El tipo de archivo no coincide con la imagen.")
            if raw.width<1 or raw.height<1 or raw.width*raw.height>MAX_PIXELS:
                raise web.HTTPBadRequest(text="La imagen supera el tamaño permitido.")
            raw.seek(0)
            raw.load()
            photo=ImageOps.exif_transpose(raw)
            photo.thumbnail((MAX_EDGE,MAX_EDGE))
            if photo.mode in ("RGBA","LA") or "transparency" in photo.info:
                background=Image.new("RGBA",photo.size,(255,255,255,255))
                background.alpha_composite(photo.convert("RGBA"))
                final=background.convert("RGB")
            else:
                final=photo.convert("RGB")
            stream=io.BytesIO()
            final.save(stream,format="JPEG",quality=85,optimize=True,progressive=True)
            return stream.getvalue()
    except web.HTTPException:
        raise
    except (UnidentifiedImageError,OSError,ValueError,TypeError,Image.DecompressionBombError):
        raise web.HTTPBadRequest(text="No se pudo procesar la portada.") from None


def temporary_cover(g,uid,folder):
    from .downloads import temporary_link
    return ("/web-galerias/cover/"+str(folder)+"?t="+
            temporary_link(g,uid,folder,"cover"))


async def serve(g,request):
    from .downloads import validate_link
    from .core import identifier
    p=validate_link(g,request.query.get("t"),"cover")
    uid=identifier(p["uid"])
    folder=identifier(request.match_info["folder"])
    if folder!=p["item"] or not await g.permitted(uid,folder):
        raise web.HTTPForbidden(text="Portada no autorizada.")
    row=await g.db().fetchrow(
        "SELECT mime,body FROM wg_folder_covers WHERE folder=$1",folder)
    if not row:
        raise web.HTTPNotFound(text="Portada no encontrada.")
    return web.Response(body=bytes(row["body"]),content_type=row["mime"],
        headers={"Cache-Control":"private, max-age=120",
                 "X-Content-Type-Options":"nosniff",
                 "Referrer-Policy":"no-referrer"})
