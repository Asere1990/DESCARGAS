"""Private and bounded promotional video pipeline for the Gallery landing page.

Large smartphone .MOV (including HEVC) is transcoded to web-compatible MP4,
stored in private R2, and streamed through same-origin with HTTP Range.
Never stores video blobs in Postgres; existing DB intro asset remains fallback.
"""
import asyncio
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from aiohttp import web

MAX_INTRO_UPLOAD=80*1024*1024
MAX_INTRO_OUTPUT=80*1024*1024
ALLOWED_VIDEO={"video/mp4":".mp4","video/quicktime":".mov","video/webm":".webm"}
UPLOAD_LOCK=asyncio.Lock()
# Preserve enough memory for the gallery API, Telegram sync and R2 while
# FFmpeg runs. On small 512 MiB instances, reject a risky conversion rather
# than bringing the entire Mini App down with an OOM/502.
MIN_VIDEO_HEADROOM=640*1024*1024

def available_memory_bytes():
    """Available cgroup memory for this service, or None if not measurable."""
    try:
        root=Path("/sys/fs/cgroup")
        maximum=(root/"memory.max").read_text().strip()
        current=int((root/"memory.current").read_text().strip())
        if maximum=="max":
            return None
        limit=int(maximum)
        return max(0,limit-current) if limit>0 else None
    except (FileNotFoundError,PermissionError,OSError,ValueError):
        return None

def require_video_memory():
    available=available_memory_bytes()
    if available is not None and available<MIN_VIDEO_HEADROOM:
        raise web.HTTPServiceUnavailable(
            text="El servidor está ocupado y necesita reservar memoria para "
                 "las galerías. Vuelve a intentar subir el video cuando baje la carga.")

def transcode_intro(source,output):
    from imageio_ffmpeg import get_ffmpeg_exe
    # One decoder, filter and encoder thread: no unbounded FFmpeg auto threads
    # competing with the backup service in a one-CPU Render instance.
    args=[get_ffmpeg_exe(),"-nostdin","-hide_banner","-loglevel","error","-y",
          "-threads","1","-filter_threads","1",
          "-i",str(source),"-map","0:v:0","-map","0:a:0?",
          "-vf","scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2",
          "-c:v","libx264","-threads","1","-preset","veryfast","-crf","25",
          "-pix_fmt","yuv420p","-c:a","aac","-b:a","96k",
          "-movflags","+faststart","-max_muxing_queue_size","256",str(output)]
    try:
        subprocess.run(args,check=True,timeout=180,stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE)
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired,OSError):
        raise web.HTTPUnprocessableEntity(
            text="No se pudo convertir el video. Usa MP4, MOV o WebM con video válido.") from None
    if not output.is_file() or not 0<output.stat().st_size<=MAX_INTRO_OUTPUT:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_INTRO_OUTPUT,actual_size=output.stat().st_size if output.exists() else 0)
    # MP4's ftyp box is required for reliable iPhone/Android playback.
    with output.open("rb") as video:
        if b"ftyp" not in video.read(64):
            raise web.HTTPUnprocessableEntity(text="No se generó un MP4 compatible.")


def extract_poster(source,output):
    """Small first-frame JPEG, generated once, never re-encoded per visitor."""
    from imageio_ffmpeg import get_ffmpeg_exe
    from PIL import Image,ImageOps
    args=[get_ffmpeg_exe(),"-nostdin","-hide_banner","-loglevel","error","-y",
          "-threads","1","-filter_threads","1","-i",str(source),
          "-an","-frames:v","1",
          "-vf","scale=960:960:force_original_aspect_ratio=decrease:force_divisible_by=2",
          "-q:v","5",str(output)]
    try:
        subprocess.run(args,check=True,timeout=35,stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE)
        with Image.open(output) as raw:
            photo=ImageOps.exif_transpose(raw)
            photo.thumbnail((960,960))
            if photo.mode!="RGB":photo=photo.convert("RGB")
            photo.save(output,format="JPEG",quality=76,optimize=True)
        if not output.is_file() or not 0<output.stat().st_size<=1024*1024:
            raise ValueError("JPEG poster out of range")
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired,OSError,ValueError):
        raise web.HTTPUnprocessableEntity(
            text="No se pudo preparar el primer fotograma del video.") from None


def fetch_video_to_disk(storage,key,path,limit):
    """Bounded streaming from private R2 for one-time legacy poster generation."""
    obj=storage._client().get_object(Bucket=storage.bucket,Key=storage.safe_key(key))
    stream=obj["Body"]
    total=0
    try:
        with path.open("wb") as target:
            for block in stream.iter_chunks(chunk_size=256*1024):
                if block:
                    total+=len(block)
                    if total>limit:raise ValueError("Video exceeds poster limit")
                    target.write(block)
        if not total:raise ValueError("Empty video in R2")
    finally:
        stream.close()


async def backfill_poster(g):
    """One-time migration for an already uploaded promotional video.

    Run off the request path so the Mini App never waits for FFmpeg or R2.
    A failed attempt leaves the last video and all existing metadata intact.
    """
    import logging
    log=logging.getLogger("web-galerias")
    try:
        await asyncio.sleep(3)
        if g.closed or not g.storage.ready:return
        raw=await g.db().fetchval("SELECT value FROM wg_config WHERE key='intro_promo'")
        if not raw:return
        metadata=json.loads(raw) if isinstance(raw,str) else dict(raw)
        if metadata.get("poster_key") or metadata.get("mime")!="video/mp4":
            return
        key=g.storage.safe_key(metadata.get("key",""))
        if not key.startswith("web-galerias/presentation/"):return
        if UPLOAD_LOCK.locked():return
        require_video_memory()
        async with UPLOAD_LOCK:
            with tempfile.TemporaryDirectory(prefix="wg-poster-backfill-") as dirname:
                original=Path(dirname)/"original.mp4"
                poster=Path(dirname)/"first-frame.jpg"
                size=metadata.get("size")
                if type(size) is not int or not 0<size<=MAX_INTRO_OUTPUT:
                    return
                await asyncio.to_thread(fetch_video_to_disk,g.storage,key,original,MAX_INTRO_OUTPUT)
                await asyncio.to_thread(extract_poster,original,poster)
                poster_key=key[:-4]+".jpg"
                await g.storage.upload(poster,poster_key,"image/jpeg")
                poster_size=poster.stat().st_size
                if await g.storage.object_size(poster_key)!=poster_size:
                    raise ValueError("Poster not verified in R2")
                metadata["poster_key"]=poster_key
                metadata["poster_size"]=poster_size
                await g.db().execute("""UPDATE wg_config SET value=$2::jsonb
                    WHERE key='intro_promo' AND value->>'key'=$1
                      AND NOT (value ? 'poster_key')""",
                    key,json.dumps(metadata))
                log.info("Promo poster cached from current video")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Existing promo poster pending (%s)",type(exc).__name__)


async def poster_asset(g,request):
    """Same-origin lightweight image preloaded before Telegram initialization."""
    from botocore.exceptions import ClientError
    raw=await g.db().fetchval("SELECT value FROM wg_config WHERE key='design'")
    design=json.loads(raw) if isinstance(raw,str) else (raw or {})
    if design.get("intro_media","video")=="photo":
        asset=await g.db().fetchrow(
            "SELECT mime,body FROM wg_assets WHERE key='intro-photo'")
        if not asset:raise web.HTTPNotFound(text="Foto de portada no disponible.")
        return web.Response(body=bytes(asset["body"]),content_type=asset["mime"],
            headers={"Cache-Control":"public, max-age=30, stale-while-revalidate=120",
                     "X-Content-Type-Options":"nosniff"})
    raw=await g.db().fetchval("SELECT value FROM wg_config WHERE key='intro_promo'")
    if not raw:raise web.HTTPNotFound(text="Video de portada no configurado.")
    metadata=json.loads(raw) if isinstance(raw,str) else dict(raw)
    key=metadata.get("poster_key")
    size=metadata.get("poster_size")
    if not isinstance(key,str) or not key.startswith("web-galerias/presentation/"):
        raise web.HTTPNotFound(text="Imagen inicial todavía no preparada.")
    if type(size) is not int or not 0<size<=1024*1024:
        raise web.HTTPNotFound()
    key=g.storage.safe_key(key)
    try:
        obj=await asyncio.to_thread(
            lambda:g.storage._client().get_object(Bucket=g.storage.bucket,Key=key))
        stream=obj["Body"]
        try:
            content=await asyncio.to_thread(stream.read,1024*1024+1)
        finally:
            await asyncio.to_thread(stream.close)
    except ClientError:
        raise web.HTTPNotFound(text="Fotograma inicial no disponible.") from None
    if len(content)!=size or len(content)>1024*1024:
        raise web.HTTPServiceUnavailable(text="Fotograma de portada incompleto.")
    return web.Response(body=content,content_type="image/jpeg",
        headers={"Cache-Control":"public, max-age=30, stale-while-revalidate=120",
                 "X-Content-Type-Options":"nosniff"})


async def upload(g,request):
    from .permissions import is_admin
    uid=await g.auth(request)
    if not is_admin(uid):raise web.HTTPForbidden(text="Solo administradores.")
    if not g.storage.ready:raise web.HTTPServiceUnavailable(text="Cloudflare R2 no está disponible.")
    mime=request.headers.get("X-Intro-Mime","").lower().strip()
    if mime not in ALLOWED_VIDEO:
        raise web.HTTPBadRequest(text="El video debe ser MP4, MOV o WebM.")
    length=request.content_length
    if length is not None and (length<=0 or length>MAX_INTRO_UPLOAD):
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_INTRO_UPLOAD,actual_size=length)
    if UPLOAD_LOCK.locked():
        raise web.HTTPConflict(text="Ya se está procesando otro video de presentación.")
    require_video_memory()
    async with UPLOAD_LOCK:
        with tempfile.TemporaryDirectory(prefix="wg-intro-") as temp:
            folder=Path(temp)
            source=folder/("original"+ALLOWED_VIDEO[mime])
            output=folder/"presentation.mp4"
            total=0
            with source.open("wb") as file:
                async for chunk in request.content.iter_chunked(256*1024):
                    total+=len(chunk)
                    if total>MAX_INTRO_UPLOAD:
                        raise web.HTTPRequestEntityTooLarge(
                            max_size=MAX_INTRO_UPLOAD,actual_size=total)
                    file.write(chunk)
            if total<64:
                raise web.HTTPBadRequest(text="El video está vacío o incompleto.")
            with source.open("rb") as file:
                magic=file.read(64)
            if mime=="video/webm":
                good=magic.startswith(b"\x1a\x45\xdf\xa3")
            else:
                good=b"ftyp" in magic
            if not good:raise web.HTTPBadRequest(text="Formato del video incorrecto.")
            require_video_memory()
            await asyncio.to_thread(transcode_intro,source,output)
            poster=folder/"first-frame.jpg"
            await asyncio.to_thread(extract_poster,output,poster)
            stem="web-galerias/presentation/"+secrets.token_hex(16)
            key=stem+".mp4"
            poster_key=stem+".jpg"
            await g.storage.upload(output,key,"video/mp4")
            size=output.stat().st_size
            checked=await g.storage.object_size(key)
            if checked!=size:
                raise web.HTTPServiceUnavailable(
                    text="No se pudo verificar el video guardado en Cloudflare.")
            # Upload the first frame independently. The visible poster must be
            # verified before publishing the new video to users.
            await g.storage.upload(poster,poster_key,"image/jpeg")
            poster_size=poster.stat().st_size
            if await g.storage.object_size(poster_key)!=poster_size:
                raise web.HTTPServiceUnavailable(
                    text="No se pudo verificar el fotograma inicial en Cloudflare.")
            # Publish only after the video AND poster are fully verified.
            metadata={"key":key,"mime":"video/mp4","size":size,
                      "poster_key":poster_key,"poster_size":poster_size,
                      "updated":time.time()}
            await g.db().execute("""INSERT INTO wg_config(key,value)
              VALUES('intro_promo',$1::jsonb)
              ON CONFLICT(key) DO UPDATE SET value=excluded.value""",json.dumps(metadata))
            await g.log(uid,"admin-intro-video")
    return {"ok":True,"complete":True,"video":"mp4",
            "size":size,"message":"Video convertido, guardado y verificado."}


async def stream(g,request,metadata):
    """Serve the R2 MP4 as a same-origin resource with byte-range support.

    The browser remains on Web Galerías, and iPhone can request seeking ranges.
    No direct Cloudflare URL is exposed in the viewer.
    """
    from botocore.exceptions import ClientError
    if not isinstance(metadata,dict) or metadata.get("mime")!="video/mp4":
        raise web.HTTPNotFound(text="Video publicitario no encontrado.")
    key=g.storage.safe_key(metadata.get("key",""))
    if not key.startswith("web-galerias/presentation/"):
        raise web.HTTPNotFound()
    full_size=metadata.get("size")
    if type(full_size) is not int or full_size<=0:
        raise web.HTTPServiceUnavailable(text="Metadatos del video incompletos.")
    byte_range=request.headers.get("Range","")
    opts={"Bucket":g.storage.bucket,"Key":key}
    if byte_range:
        # Single, bounded HTTP range; reject multi-ranges and malformed values.
        match=re.fullmatch(r"bytes=(\d*)-(\d*)",byte_range)
        if not match or (not match.group(1) and not match.group(2)):
            raise web.HTTPRequestRangeNotSatisfiable()
        start=int(match.group(1)) if match.group(1) else None
        end=int(match.group(2)) if match.group(2) else None
        if start is not None and (start>=full_size or (end is not None and end<start)):
            raise web.HTTPRequestRangeNotSatisfiable()
        if start is None and (end is None or end<=0):
            raise web.HTTPRequestRangeNotSatisfiable()
        opts["Range"]=byte_range
    try:
        obj=await asyncio.to_thread(lambda:g.storage._client().get_object(**opts))
    except ClientError as exc:
        reason=str(exc.response.get("Error",{}).get("Code",""))
        if reason in ("404","NoSuchKey","NotFound"):
            raise web.HTTPNotFound(text="Video todavía no disponible.") from None
        if reason in ("InvalidRange","416"):
            raise web.HTTPRequestRangeNotSatisfiable() from None
        raise web.HTTPServiceUnavailable(text="El video no está disponible temporalmente.") from None
    body=obj["Body"]
    headers={"Accept-Ranges":"bytes","Cache-Control":"public, max-age=300",
             "X-Content-Type-Options":"nosniff"}
    if obj.get("ContentLength") is not None:
        headers["Content-Length"]=str(obj["ContentLength"])
    if obj.get("ContentRange"):
        headers["Content-Range"]=obj["ContentRange"]
    status=206 if obj.get("ContentRange") else 200
    response=web.StreamResponse(status=status,headers=headers)
    response.content_type="video/mp4"
    try:
        await response.prepare(request)
        while True:
            chunk=await asyncio.to_thread(body.read,256*1024)
            if not chunk:break
            await response.write(chunk)
        await response.write_eof()
    except (ConnectionResetError,BrokenPipeError):
        pass
    finally:
        await asyncio.to_thread(body.close)
    return response
