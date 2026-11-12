"""Optional *isolated low-priority* local OCR for gallery thumbnails.

This module is not loaded during the user's search or first paint.
Enable only on a dedicated/appropriately provisioned CLOUDFLARE runtime with
GALERIAS_OCR_ENABLED=1 and optional rapidocr+onnxruntime dependencies installed.
The process is single-worker and runs at nice=15 to protect main requests.
"""
import asyncio
import concurrent.futures
import logging
import os
import time
from io import BytesIO

from . import media_text

log=logging.getLogger("gallery-ocr")
_ENGINE=None
_POOL=None


def recognize_bytes(payload):
    """Executed in an isolated lower-priority child process (no web tokens)."""
    global _ENGINE
    if _ENGINE is None:
        from rapidocr import RapidOCR
        _ENGINE=RapidOCR()
    result=_ENGINE(payload)
    if not result:
        return ""
    txts=getattr(result,"txts",None) or []
    scores=getattr(result,"scores",None) or []
    clean=[]
    for i,text in enumerate(txts):
        if not isinstance(text,str):
            continue
        if i<len(scores) and float(scores[i])<0.48:
            continue
        clean.append(text)
    return media_text._dedup_text(clean,limit=2500,per_part=250)


def low_priority_child():
    try:
        os.nice(15)
    except (AttributeError,OSError,PermissionError):
        pass
    # Don't make a single file monopolize every core/thread.
    os.environ["OMP_NUM_THREADS"]="1"
    os.environ["OPENBLAS_NUM_THREADS"]="1"


async def ocr_once(g):
    if os.getenv("GALERIAS_OCR_ENABLED","0")!="1":
        return False
    if not g.storage.ready:
        return False
    global _POOL
    if _POOL is None:
        try:
            import rapidocr  # noqa: F401  - optional, never required by DESCARGAS
            import onnxruntime  # noqa: F401
        except ImportError:
            log.warning("OCR waiting for optional RapidOCR/ONNX CPU dependencies")
            return False
        _POOL=concurrent.futures.ProcessPoolExecutor(
            max_workers=1,initializer=low_priority_child)
    db=g.db()
    now=time.time()
    # Don't block the source gallery while an R2 thumbnail is being backed up.
    row=await db.fetchrow("""
       WITH due AS (
          SELECT id FROM wg_media WHERE state IN ('preview','ready')
            AND thumb_key IS NOT NULL AND NOT ocr_checked
            AND ocr_next_attempt<=$1
          ORDER BY id DESC LIMIT 1 FOR UPDATE SKIP LOCKED
       )
       UPDATE wg_media m SET ocr_next_attempt=$1+180,
              ocr_attempts=ocr_attempts+1
       FROM due WHERE m.id=due.id
       RETURNING m.id,m.folder,m.message,m.thumb_key,m.ocr_attempts
    """,now)
    if not row:
        return False
    try:
        def thumbnail():
            client=g.storage._client()
            obj=client.get_object(Bucket=g.storage.bucket,
                                  Key=g.storage.safe_key(row["thumb_key"]))
            stream=obj["Body"]
            try:
                payload=stream.read(3000001)
            finally:
                stream.close()
            if len(payload)>3000000:
                raise ValueError("Thumbnail too large for OCR")
            from PIL import Image
            with Image.open(BytesIO(payload)) as image:
                if image.width*image.height>18000000:
                    raise ValueError("OCR image dimensions too large")
            return payload
        blob=await asyncio.to_thread(thumbnail)
        result=await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                _POOL,recognize_bytes,blob),timeout=48)
        await db.execute("""
          UPDATE wg_media
          SET ocr_text=$2,ocr_checked=TRUE,ocr_next_attempt=0
          WHERE id=$1 AND state IN ('preview','ready')
        """,row["id"],str(result)[:2500])
        await media_text.rebuild(g,int(row["folder"]),[int(row["message"])])
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("OCR deferred (%s)",type(exc).__name__)
        attempts=int(row["ocr_attempts"] or 1)
        await db.execute("""
          UPDATE wg_media SET
           ocr_next_attempt=$2,
           ocr_checked=$3
          WHERE id=$1
        """,row["id"],time.time()+min(7200,120*2**min(attempts,5)),
           attempts>=5)
        return False


async def run(g):
    """OCR never runs in API paths, never downloads a full 1 GB video."""
    if os.getenv("GALERIAS_OCR_ENABLED","0")!="1":
        return
    await asyncio.sleep(35)
    try:
        while not g.closed:
            try:
                await ocr_once(g)
                # A single OCR task every 30 seconds protects 1-core Render.
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("OCR worker retry (%s)",type(exc).__name__)
                await asyncio.sleep(90)
    finally:
        global _POOL
        if _POOL:
            _POOL.shutdown(wait=False,cancel_futures=True)
            _POOL=None
