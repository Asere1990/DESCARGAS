"""Copia masiva progresiva, con pausa/reanudación por carpeta e idempotencia en R2.

Solo CLOUDFLARE con GALERIAS_SYNC_ENABLED=1 ejecuta este motor.
Carriles por cuenta con concurrencia limitada y lock compartido con DESCARGAS.
No actualiza sesiones, no inicia bot polling y jamás elimina archivos R2 al ocultar carpetas.
"""
import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from telethon import errors, types
from PIL import Image, ImageOps
from .core import normal
from .media_types import classify

log=logging.getLogger("web-galerias-sync")
EXTRA="""
CREATE TABLE IF NOT EXISTS wg_discovery(
 folder BIGINT NOT NULL,uid BIGINT NOT NULL,
 tried_at DOUBLE PRECISION NOT NULL DEFAULT 0,works BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(folder,uid));
ALTER TABLE wg_discovery ADD COLUMN IF NOT EXISTS next_after DOUBLE PRECISION NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS wg_replies(
 folder BIGINT NOT NULL,parent_message BIGINT NOT NULL,reply_message BIGINT NOT NULL,
 words TEXT NOT NULL,PRIMARY KEY(folder,reply_message));
CREATE TABLE IF NOT EXISTS wg_source_peers(
 folder BIGINT NOT NULL,uid BIGINT NOT NULL,handle BYTEA NOT NULL,
 updated DOUBLE PRECISION NOT NULL,PRIMARY KEY(folder,uid));
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS source_total BIGINT NOT NULL DEFAULT 0;
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS available DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS error TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS wg_media_pending ON wg_media(folder,available,id)
 WHERE state='pending';
CREATE TABLE IF NOT EXISTS wg_account_cooldown(
 uid BIGINT PRIMARY KEY,available_at DOUBLE PRECISION NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS wg_media_preview ON wg_media(folder,available,id)
 WHERE state='preview';
CREATE INDEX IF NOT EXISTS wg_media_visible ON wg_media(folder,id DESC)
 WHERE state IN ('preview','ready');
"""

class PauseRequested(Exception):
    """Carpeta desactivada, cola principal ocupada o copia global detenida."""


def media_kind(m):
    """Mantener la firma anterior para los consumidores del sincronizador."""
    kind,mime,_=classify(m)
    return kind,mime


async def folder_active(g,folder):
    return bool(await g.db().fetchval(
        "SELECT active FROM wg_folders WHERE id=$1",folder))


async def checkpoint(g, folder=None, phase="esperando", error=""):
    data={"enabled":True,"storage":g.storage.ready,"updated":time.time(),
          "folder":folder,"phase":phase,"error":error}
    await g.db().execute("""INSERT INTO wg_config(key,value)
      VALUES('backup_status',$1::jsonb) ON CONFLICT(key)
      DO UPDATE SET value=excluded.value""",json.dumps(data))


async def source_free(g,uid,folder,*,now=None):
    if g.closed or os.getenv("GALERIAS_SYNC_ENABLED","0")!="1":
        return False
    if not await folder_active(g,folder):
        return False
    queued=await g.db().fetchval("""SELECT EXISTS(
      SELECT 1 FROM descarga_jobs
      WHERE uid=$1 AND state IN ('queued','preparing','sending'))""",uid)
    return not queued


class PauseGuard:
    """Comprueba frecuentemente el estado, sin consultar Postgres en cada chunk."""
    def __init__(self,g,uid,folder):
        self.g=g;self.uid=uid;self.folder=folder;self.last=0.0
        self.last_heartbeat=0.0

    async def __call__(self):
        now=time.monotonic()
        if now-self.last<1.5:
            return
        self.last=now
        if not await source_free(self.g,self.uid,self.folder):
            raise PauseRequested("Backup pausado sin modificar sesión Telegram")
        # Un video grande puede tardar minutos; mantener el estado visible real.
        if now-self.last_heartbeat>=30:
            await checkpoint(self.g,self.folder,"respaldando")
            self.last_heartbeat=now


async def find_dialog(g,client,folder,uid,peer):
    """Cache cifrada del peer por cuenta para evitar recorrer todos los chats en cada ciclo."""
    db=g.db()
    snapshot=await db.fetchval("""SELECT handle FROM wg_source_peers
      WHERE folder=$1 AND uid=$2""",folder,uid)
    if snapshot:
        try:
            data=g.s.repo.decode(snapshot)
            if data["kind"]=="channel":
                return types.InputPeerChannel(int(data["id"]),int(data["hash"]))
            if data["kind"]=="chat":
                return types.InputPeerChat(int(data["id"]))
        except Exception:
            await db.execute("DELETE FROM wg_source_peers WHERE folder=$1 AND uid=$2",
                             folder,uid)
    async for dialog in client.iter_dialogs():
        if int(dialog.id)!=int(peer):continue
        entity=dialog.entity
        if isinstance(entity,types.Channel) and getattr(entity,"access_hash",None):
            data={"kind":"channel","id":int(entity.id),"hash":int(entity.access_hash)}
        elif isinstance(entity,types.Chat):
            data={"kind":"chat","id":int(entity.id)}
        else:
            return None
        await db.execute("""INSERT INTO wg_source_peers(folder,uid,handle,updated)
          VALUES($1,$2,$3,$4) ON CONFLICT(folder,uid)
          DO UPDATE SET handle=excluded.handle,updated=excluded.updated""",
          folder,uid,g.s.repo.encode(data),time.time())
        return entity
    return None


async def choose_account(g,folder):
    """Priorizar la cuenta conocida y probar alternativas si deja de tener acceso."""
    now=time.time()
    row=await g.db().fetchrow("""SELECT a.uid FROM descarga_accounts a
      LEFT JOIN wg_discovery d ON d.folder=$1 AND d.uid=a.uid
      LEFT JOIN wg_account_cooldown c ON c.uid=a.uid
      WHERE a.session IS NOT NULL AND COALESCE(d.next_after,0)<=$2
        AND COALESCE(c.available_at,0)<=$2
        AND (a.uid=$3 OR d.works OR d.tried_at IS NULL OR d.tried_at<$2-900)
      ORDER BY CASE WHEN a.uid=$3 THEN 0 WHEN d.works THEN 1 ELSE 2 END,
               -- Nuevas carpetas sin origen: distribuir la exploración
               -- entre las cuentas elegibles en vez de elegir siempre la primera.
               CASE WHEN $3::bigint IS NULL AND d.tried_at IS NULL
                    THEN md5(a.uid::text || ':' || $1::text) ELSE '' END,
               COALESCE(d.tried_at,0),a.uid LIMIT 1""",
       folder["id"],now,folder["source_uid"])
    return row["uid"] if row else None


async def mark_discovery(g,folder,uid,works,delay=0):
    await g.db().execute("""INSERT INTO wg_discovery(
      folder,uid,tried_at,works,next_after)
      VALUES($1,$2,$3,$4,$5) ON CONFLICT(folder,uid)
      DO UPDATE SET tried_at=excluded.tried_at,
       works=excluded.works,next_after=excluded.next_after""",
       folder,uid,time.time(),works,time.time()+max(0,delay))


async def catalog_batch(g,folder,client,entity,source,limit=90):
    """Indexa con checkpoint duradero, incluso aunque no haya archivos nuevos."""
    if not await folder_active(g,folder):raise PauseRequested()
    rows=[]
    limit=max(10,min(120,int(limit)))
    if not source["complete"]:
        async for m in client.iter_messages(entity,
              offset_id=source["scan_cursor"] or 0,limit=limit):
            rows.append(m)
    else:
        async for m in client.iter_messages(entity,
              min_id=source["high_water"],reverse=True,limit=limit):
            rows.append(m)
    if not await folder_active(g,folder):raise PauseRequested()
    if not rows:
        if not source["complete"]:
            await g.db().execute(
              "UPDATE wg_folders SET complete=TRUE WHERE id=$1 AND active",
               folder)
        return 0
    for m in rows:
        await catalog(g,folder,m)
    if not source["complete"]:
        await g.db().execute("""UPDATE wg_folders SET scan_cursor=$2,
          high_water=GREATEST(high_water,$3),scan_count=scan_count+$4
          WHERE id=$1 AND active""",folder,min(x.id for x in rows),
          max(x.id for x in rows),len(rows))
    else:
        await g.db().execute("""UPDATE wg_folders SET
          high_water=GREATEST(high_water,$2),
          scan_count=scan_count+$3 WHERE id=$1 AND active""",
          folder,max(x.id for x in rows),len(rows))
    return len(rows)


async def preview_one(g,folder,item,message,client,guard):
    """Mostrar primero una miniatura: el original aún no está respaldado."""
    await guard()
    key=f"folders/{folder}/{message.id}/thumb.jpg"
    remote=await g.storage.object_size(key)
    saved=key if remote and remote>0 else None
    if saved is None:
        with tempfile.TemporaryDirectory(prefix="wg-preview-") as temporary:
            temporary=Path(temporary)
            thumb=temporary/"thumb.jpg"
            try:
                source=getattr(message,"photo",None) or getattr(message,"document",None)
                sizes=getattr(source,"sizes",None) or getattr(source,"thumbs",None) or []
                selected=next((i for i,size in enumerate(sizes)
                               if int(getattr(size,"w",0) or 0)>=320),None)
                index=selected if selected is not None else (len(sizes)-1 if sizes else 0)
                # Un documento video/GIF sin miniatura no debe descargar el
                # video completo en la fase prioritaria de vistas previas.
                if sizes or (item["kind"]=="photo"
                             and int(getattr(source,"size",0) or 0)<=8*1024*1024):
                    result=await client.download_media(message,
                        file=str(temporary/"preview"),thumb=index if sizes else None)
                else:
                    result=None
                await guard()
                if result and Path(result).is_file():
                    with Image.open(result) as photo:
                        photo=ImageOps.exif_transpose(photo)
                        photo.thumbnail((640,640))
                        if photo.mode!="RGB":photo=photo.convert("RGB")
                        photo.save(thumb,format="JPEG",quality=84,optimize=True)
            except (PauseRequested,asyncio.CancelledError,errors.FloodWaitError):
                raise
            except (OSError,ValueError,TypeError) as exc:
                # Algunos videos no tienen miniatura. Su ficha sigue visible.
                log.info("Gallery thumbnail not available (%s)",type(exc).__name__)
            if thumb.is_file():
                await guard()
                await g.storage.upload(thumb,key,"image/jpeg")
                actual=await g.storage.object_size(key)
                if actual!=thumb.stat().st_size:
                    raise ValueError("La miniatura R2 no coincide con la enviada.")
                saved=key
    await guard()
    await g.db().execute("""UPDATE wg_media SET state='preview',
      thumb_key=$3,error='' WHERE folder=$1 AND message=$2 AND state='pending'""",
      folder,message.id,saved)


async def media_retry(g,folder,message,exc):
    await g.db().execute("""UPDATE wg_media SET
      attempts=attempts+1,available=$3,error=$4
      WHERE folder=$1 AND message=$2 AND state IN ('pending','preview')""",
      folder,message,
      time.time()+min(3600,30*2**min(7,int(await g.db().fetchval(
         "SELECT COALESCE(attempts,0) FROM wg_media WHERE folder=$1 AND message=$2",
         folder,message) or 0))),
      type(exc).__name__)


async def inspect_source(g,folder,item,message):
    """No confundir un fallo temporal, un formato nuevo o una protección con un borrado.

    Devuelve True si el archivo puede procesarse ahora. Reintenta los ausentes
    hasta tres veces, con separación; nunca borra datos de R2.
    """
    expected=item["kind"]
    old_mime=item["mime"]
    state=item["state"]
    current=classify(message) if message else (None,None,None)
    kind,mime,source_type=current
    if message and (getattr(message,"noforwards",False)
                    or getattr(message,"ttl_period",None)
                    or getattr(getattr(message,"media",None),"ttl_seconds",None)):
        await g.db().execute("""UPDATE wg_media SET state='skipped',
           error='protected_or_temporary' WHERE folder=$1 AND message=$2
           AND state=$3""",folder,item["message"],state)
        return False
    if not kind:
        count=max(0,int(item["source_checks"] or 0))+1
        reason="message_absent" if not message else "media_removed"
        confirmed=count>=3
        next_state="missing" if confirmed else state
        next_error=reason+"_after_rechecks" if confirmed else reason+"_pending_recheck"
        # Al haber varios miles, nunca hacer las tres comprobaciones seguidas.
        wait=0 if confirmed else (300 if count==1 else 1800)
        await g.db().execute("""UPDATE wg_media SET state=$4,
          error=$5,source_checks=$6,available=$7
          WHERE folder=$1 AND message=$2 AND state=$3""",
          folder,item["message"],state,next_state,next_error,count,time.time()+wait)
        return False
    if kind!=expected or mime!=old_mime:
        await g.db().execute("""UPDATE wg_media SET kind=$4,mime=$5,
           source_type=$6,source_checks=0,available=0,state='pending',
           error='tipo_actualizado'
           WHERE folder=$1 AND message=$2 AND state=$3""",
           folder,item["message"],state,kind,mime,source_type)
        return False
    if item["source_type"]!=source_type or item["source_checks"]:
        await g.db().execute("""UPDATE wg_media SET source_type=$3,
           source_checks=0,error=CASE WHEN source_checks>0 THEN '' ELSE error END
           WHERE folder=$1 AND message=$2""",folder,item["message"],source_type)
    return True


async def sync_folder(g,source,*,max_files=None,scan_size=90,catalog_only=False,phase="combined"):
    """Un turno de lectura + copia, con lock exclusivo de cuenta ya existente."""
    from descargas_app.account_chat import connected
    folder=source["id"]
    if not await folder_active(g,folder):return
    uid=await choose_account(g,source)
    if uid is None:
        await g.db().execute("""UPDATE wg_folders SET
          error='Buscando otra cuenta disponible' WHERE id=$1 AND active""",folder)
        return
    if not await source_free(g,uid,folder):return
    try:
        async with connected(g.s,uid) as client:
            guard=PauseGuard(g,uid,folder)
            await guard()
            entity=await find_dialog(g,client,folder,uid,source["peer"])
            await guard()
            if entity is None:
                await mark_discovery(g,folder,uid,False,delay=600)
                await g.db().execute("""UPDATE wg_folders SET source_uid=NULL
                  WHERE id=$1 AND source_uid=$2""",folder,uid)
                return
            await mark_discovery(g,folder,uid,True)
            await g.db().execute("""UPDATE wg_folders SET
              source_uid=$2,last_check=$3,error='' WHERE id=$1 AND active""",
              folder,uid,time.time())

            if phase in ("combined","index","prime"):
                await checkpoint(g,folder,"catalogando")
                await catalog_batch(g,folder,client,entity,source,limit=scan_size)
            if catalog_only or phase=="index":
                await checkpoint(g,folder,"en_espera")
                return
            requested=max_files if max_files is not None else os.getenv("GALERIAS_SYNC_BATCH_FILES","5")
            batch=max(1,min(12,int(requested)))
            if phase in ("combined","preview","prime"):
                for _ in range(batch):
                    await guard()
                    item=await g.db().fetchrow("""SELECT message,kind,mime,size,filename,
                         attempts,source_checks,source_type,'pending'::text AS state
                       FROM wg_media WHERE folder=$1 AND state='pending'
                         AND available<=$2 ORDER BY id DESC LIMIT 1""",
                         folder,time.time())
                    if not item:break
                    m=await client.get_messages(entity,ids=item["message"])
                    await guard()
                    if not await inspect_source(g,folder,item,m):
                        continue
                    await checkpoint(g,folder,"preparando_vistas_previas")
                    try:
                        await preview_one(g,folder,item,m,client,guard)
                    except (PauseRequested,asyncio.CancelledError,errors.FloodWaitError):
                        raise
                    except Exception as exc:
                        log.warning("Gallery preview retry (%s)",type(exc).__name__)
                        await media_retry(g,folder,item["message"],exc)
            if phase in ("combined","archive"):
                for _ in range(batch):
                    await guard()
                    item=await g.db().fetchrow("""SELECT message,kind,mime,size,
                         attempts,source_checks,source_type,'preview'::text AS state
                       FROM wg_media WHERE folder=$1 AND state='preview'
                         AND available<=$2
                         ORDER BY CASE WHEN kind='photo' THEN 0 ELSE 1 END,
                                  id DESC LIMIT 1""",folder,time.time())
                    if not item:break
                    m=await client.get_messages(entity,ids=item["message"])
                    await guard()
                    if not await inspect_source(g,folder,item,m):
                        continue
                    await checkpoint(g,folder,"respaldando")
                    try:
                        await backup_one(g,folder,item,m,client,guard)
                    except (PauseRequested,asyncio.CancelledError,errors.FloodWaitError):
                        raise
                    except Exception as exc:
                        log.warning("Gallery file retry (%s)",type(exc).__name__)
                        await media_retry(g,folder,item["message"],exc)
            await checkpoint(g,folder,"en_espera")
    except (PauseRequested,asyncio.CancelledError):
        if g.closed:raise asyncio.CancelledError()
        return
    except errors.FloodWaitError as exc:
        seconds=max(1,int(getattr(exc,"seconds",60)))
        # Un FloodWait de 27 s no debe provocar otro intento al cabo de 30 s.
        # Enfriar tanto esta sesión como el descubrimiento global, sin
        # interferir con las descargas de DESCARGAS.
        per_account_cooldown=max(600,seconds+120)
        # Enfriar esta cuenta para todas sus carpetas, sin frenar otras cuentas.
        await g.db().execute("""INSERT INTO wg_account_cooldown(uid,available_at)
          VALUES($1,$2) ON CONFLICT(uid) DO UPDATE
          SET available_at=GREATEST(wg_account_cooldown.available_at,excluded.available_at)""",
          uid,time.time()+per_account_cooldown)
        await mark_discovery(g,folder,uid,True,delay=per_account_cooldown)
        await g.db().execute("""UPDATE wg_folders
          SET error='Telegram pidió esperar; respaldo reprogramado sin afectar cuentas'
          WHERE id=$1 AND active""",folder)
        log.warning("Gallery cooldown after rate limit: wait=%ds account_cooldown=%ds",
                    seconds,per_account_cooldown)
    except ValueError as exc:
        # Cuenta ocupada = ceder, pero credencial no válida = pasar a otra.
        if "Espera a que termine" not in str(exc):
            await mark_discovery(g,folder,uid,False,delay=900)
            await g.db().execute("""UPDATE wg_folders SET source_uid=NULL
              WHERE id=$1 AND source_uid=$2""",folder,uid)
    except (errors.RPCError,OSError,TimeoutError) as exc:
        log.warning("Gallery account deferred (%s)",type(exc).__name__)
        # Retirar un access_hash antiguo; al intentar de nuevo se buscará uno válido.
        await g.db().execute("DELETE FROM wg_source_peers WHERE folder=$1 AND uid=$2",
                             folder,uid)
        await mark_discovery(g,folder,uid,False,delay=600)
        await g.db().execute("""UPDATE wg_folders SET source_uid=NULL,error=$3
          WHERE id=$1 AND source_uid=$2""",folder,uid,type(exc).__name__)
    except Exception as exc:
        log.warning("Gallery retry (%s)",type(exc).__name__)
        await g.db().execute("""UPDATE wg_folders SET error=$2
          WHERE id=$1 AND active""",folder,type(exc).__name__)


async def catalog(g,folder,m):
    """Guardar solo multimedia y sus captions: nunca indexar conversaciones."""
    if (getattr(m,"noforwards",False) or getattr(m,"ttl_period",None)
        or getattr(getattr(m,"media",None),"ttl_seconds",None)):
        return
    kind,mime,source_type=classify(m)
    if not kind:
        return
    caption=str(getattr(m,"message","") or "")[:1500]
    doc=getattr(m,"document",None)
    size=int(getattr(doc,"size",0) or 0)
    filename="video.mp4" if kind=="video" else "foto.jpg"
    attrs=getattr(doc,"attributes",[]) or []
    for attr in attrs:
        if getattr(attr,"file_name",None):
            filename=str(attr.file_name)[:110]
            break
    if source_type=="gif" and mime=="image/gif" and filename=="video.mp4":
        filename="animacion.gif"
    if source_type=="photo_file" and filename=="foto.jpg":
        extension={"image/png":".png","image/webp":".webp",
                   "image/heic":".heic","image/avif":".avif"}.get(mime,".jpg")
        filename="foto"+extension
    reply=getattr(getattr(m,"reply_to",None),"reply_to_msg_id",None)
    grouped=str(m.grouped_id) if getattr(m,"grouped_id",None) else None
    await g.db().execute("""INSERT INTO wg_media(
      folder,message,kind,mime,filename,width,height,size,date,label,search_text,
      reply_to,grouped,created,source_type)
      VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
      ON CONFLICT(folder,message) DO UPDATE SET
        source_type=excluded.source_type,
        kind=excluded.kind,mime=excluded.mime,filename=excluded.filename,
        label=excluded.label,search_text=excluded.search_text
      WHERE wg_media.state IN ('pending','missing')""",
      folder,m.id,kind,mime,filename,0,0,size,
      m.date.timestamp() if m.date else time.time(),
      caption,normal(caption)[:4000],reply,grouped,time.time(),source_type)


async def backup_one(g,folder,item,message,client,guard):
    """Respaldo íntegro y verificable, sin modificar el formato original."""
    await guard()
    kind=item["kind"]
    mime=item["mime"] or ("image/jpeg" if kind=="photo" else "video/mp4")
    if kind=="video" and mime=="image/gif":
        from .gif_backup import backup_gif
        return await backup_gif(g,folder,item,message,client,guard)
    extensions={"image/jpeg":".jpg","image/png":".png","image/webp":".webp",
      "image/bmp":".bmp","image/heic":".heic","image/heif":".heif",
      "image/avif":".avif","video/quicktime":".mov",
      "video/webm":".webm","video/x-matroska":".mkv",
      "video/x-msvideo":".avi","video/x-m4v":".m4v","video/3gpp":".3gp"}
    ending=extensions.get(mime,".jpg" if kind=="photo" else ".mp4")
    key=f"folders/{folder}/{message.id}/original{ending}"
    thumb_key=f"folders/{folder}/{message.id}/thumb.jpg"

    original_size=await g.storage.object_size(key)
    thumb_size=await g.storage.object_size(thumb_key)
    known_size=int(item["size"] or 0)
    original_valid=original_size is not None and original_size>0 and (
        known_size==0 or original_size==known_size)
    if original_valid and thumb_size:
        await g.db().execute("""UPDATE wg_media SET state='ready',
          file_key=$3,thumb_key=$4,size=$5,error='',source_checks=0
          WHERE folder=$1 AND message=$2 AND state IN ('preview','pending')""",
          folder,message.id,key,thumb_key,original_size)
        return

    with tempfile.TemporaryDirectory(prefix="wg-") as tmp:
        tmp=Path(tmp)
        thumb=tmp/"thumb.jpg"
        if kind=="photo" and not original_valid:
            image_path=tmp/("original"+ending)
            async def progress(_,total):
                await guard()
            returned=await client.download_media(message,file=str(image_path),
                                                  progress_callback=progress)
            image_path=Path(returned) if returned else image_path
            if not image_path.is_file() or image_path.stat().st_size<=0:
                raise ValueError("Imagen incompleta")
            try:
                with Image.open(image_path) as photo:
                    photo=ImageOps.exif_transpose(photo)
                    photo.thumbnail((640,640))
                    if photo.mode!="RGB":photo=photo.convert("RGB")
                    photo.save(thumb,format="JPEG",quality=88,optimize=True)
            except (OSError,ValueError) as exc:
                # No bloquear respaldo de HEIC/AVIF si Pillow no trae decoder.
                log.info("Photo document thumbnail skipped (%s)",type(exc).__name__)
            await guard()
            await g.storage.upload(image_path,key,mime)
            original_size=image_path.stat().st_size
        elif kind=="video" and not original_valid:
            # Los videos se transmiten a R2 por partes sin cargar todo en RAM.
            parts=client.iter_download(message.media,request_size=512*1024)
            original_size=await g.storage.upload_stream(parts,key,mime,check=guard)

        if kind=="photo" and not thumb.is_file() and not thumb_size:
            image_path=tmp/("source"+ending)
            await guard()
            async def progress(_,total):
                await guard()
            returned=await client.download_media(message,file=str(image_path),
                                                  progress_callback=progress)
            image_path=Path(returned) if returned else image_path
            try:
                with Image.open(image_path) as photo:
                    photo=ImageOps.exif_transpose(photo)
                    photo.thumbnail((640,640))
                    if photo.mode!="RGB":photo=photo.convert("RGB")
                    photo.save(thumb,format="JPEG",quality=88,optimize=True)
            except (OSError,ValueError) as exc:
                log.info("Photo document thumbnail skipped (%s)",type(exc).__name__)
        if kind=="video" and not thumb_size:
            try:
                preview=await client.download_media(
                    message,file=str(tmp/"video_preview"),thumb=-1)
                if preview and Path(preview).is_file():
                    with Image.open(preview) as photo:
                        photo=ImageOps.exif_transpose(photo)
                        photo.thumbnail((640,640))
                        if photo.mode!="RGB":photo=photo.convert("RGB")
                        photo.save(thumb,format="JPEG",quality=88,optimize=True)
            except (PauseRequested,asyncio.CancelledError):
                raise
            except Exception:
                pass
        if thumb.is_file() and not thumb_size:
            await guard()
            await g.storage.upload(thumb,thumb_key,"image/jpeg")
            thumb_size=thumb.stat().st_size

    await guard()
    await g.db().execute("""UPDATE wg_media SET state='ready',file_key=$3,
      thumb_key=$4,size=$5,error='',source_checks=0
      WHERE folder=$1 AND message=$2 AND state IN ('preview','pending')""",
      folder,message.id,key,thumb_key if thumb_size else None,original_size or 0)


def sync_limits(total):
    """Turnos cortos para no monopolizar las demás galerías."""
    try:
        default=max(1,min(12,int(os.getenv("GALERIAS_SYNC_BATCH_FILES","5"))))
    except ValueError:
        default=5
    count=max(1,int(total))
    return min(default,max(1,24//count)), (90 if count<=5 else 60 if count<20 else 30)


async def fair_round(g,folders,*,catalog_only=False,phase="combined"):
    """Avanza todas las galerías en rondas, con cuentas distintas en paralelo.

    Una cuenta conserva su lock exclusivo para proteger las sesiones de
    DESCARGAS; el número de sesiones concurrentes es reducido y ajustable.
    """
    if not folders or g.closed:
        return
    files,scan=sync_limits(len(folders))
    if phase=="archive":
        files=1  # Un original por galería: videos grandes no bloquean toda la ronda.
    try:
        max_accounts=max(1,min(4,int(os.getenv("GALERIAS_SYNC_PARALLEL_ACCOUNTS","3"))))
    except ValueError:
        max_accounts=3
    try:
        spacing=max(0.0,min(5.0,float(os.getenv("GALERIAS_SYNC_BETWEEN_FOLDERS","0.5"))))
    except ValueError:
        spacing=0.5
    semaphore=asyncio.Semaphore(max_accounts)
    lanes={}
    for folder in folders:
        # Las galerías sin fuente conocida se agrupan por cuenta candidata.
        # Sigue existiendo un único carril por sesión autorizada.
        uid=folder["source_uid"] or await choose_account(g,folder)
        lanes.setdefault(uid or 0,[]).append(folder)

    async def lane(group):
        async with semaphore:
            for folder in group:
                if g.closed:
                    return
                await sync_folder(g,folder,max_files=files,scan_size=scan,
                                  catalog_only=catalog_only,phase=phase)
                if spacing:
                    await asyncio.sleep(spacing)

    await asyncio.gather(*(lane(group) for group in lanes.values()))


async def run(g):
    """Líder exclusivo; progreso equitativo y reanudable hasta 50 galerías."""
    db=g.db()
    await db.execute(EXTRA)
    async with db.acquire() as leader:
        while not g.closed:
            if await leader.fetchval("SELECT pg_try_advisory_lock(784,19)"):
                break
            await asyncio.sleep(5)
        try:
            log.warning("Gallery mass backup active; fair bounded parallel lanes")
            while not g.closed:
                try:
                    if os.getenv("GALERIAS_SYNC_ENABLED","0")!="1" or not g.storage.ready:
                        await asyncio.sleep(10)
                        continue
                    await checkpoint(g,phase="esperando_carpetas")
                    folders=await db.fetch("""SELECT * FROM wg_folders WHERE active
                      ORDER BY last_check ASC,id ASC""")
                    if not folders:
                        await asyncio.sleep(5)
                        continue
                    # Índice + miniatura en el mismo turno de cada galería:
                    # el cliente no espera a que terminen 50 índices.
                    await fair_round(g,folders,phase="prime")
                    # Archivar un número limitado por ronda evita que los videos
                    # grandes retrasen indefinidamente las otras 50 galerías.
                    if folders:
                        offset=getattr(g,"archive_offset",0)%len(folders)
                        selected=(folders[offset:]+folders[:offset])[:max(1,min(3,len(folders)))]
                        g.archive_offset=(offset+len(selected))%len(folders)
                        await fair_round(g,selected,phase="archive")
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Gallery supervisor retrying (%s)",type(exc).__name__)
                    await asyncio.sleep(20)
        finally:
            await leader.execute("SELECT pg_advisory_unlock(784,19)")
