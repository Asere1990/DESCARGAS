"""Copia masiva progresiva, con pausa/reanudación por carpeta e idempotencia en R2.

Solo CLOUDFLARE con GALERIAS_SYNC_ENABLED=1 ejecuta este motor.
Una cuenta/una conexión a la vez, con el advisory lock de DESCARGAS.
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
"""

class PauseRequested(Exception):
    """Carpeta desactivada, cola principal ocupada o copia global detenida."""


def media_kind(m):
    if getattr(m,"photo",None):return "photo","image/jpeg"
    doc=getattr(m,"document",None)
    mime=str(getattr(doc,"mime_type","") or "")
    if mime.startswith("video/"):return "video",mime
    return None,None


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
      WHERE a.session IS NOT NULL AND COALESCE(d.next_after,0)<=$2
        AND (a.uid=$3 OR d.works OR d.tried_at IS NULL OR d.tried_at<$2-900)
      ORDER BY CASE WHEN a.uid=$3 THEN 0 WHEN d.works THEN 1 ELSE 2 END,
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


async def catalog_batch(g,folder,client,entity,source):
    """Indexa con checkpoint duradero, incluso aunque no haya archivos nuevos."""
    if not await folder_active(g,folder):raise PauseRequested()
    rows=[]
    if not source["complete"]:
        async for m in client.iter_messages(entity,
              offset_id=source["scan_cursor"] or 0,limit=90):
            rows.append(m)
    else:
        async for m in client.iter_messages(entity,
              min_id=source["high_water"],reverse=True,limit=90):
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


async def media_retry(g,folder,message,exc):
    await g.db().execute("""UPDATE wg_media SET
      attempts=attempts+1,available=$3,error=$4
      WHERE folder=$1 AND message=$2 AND state='pending'""",
      folder,message,
      time.time()+min(3600,30*2**min(7,int(await g.db().fetchval(
         "SELECT COALESCE(attempts,0) FROM wg_media WHERE folder=$1 AND message=$2",
         folder,message) or 0))),
      type(exc).__name__)


async def sync_folder(g,source):
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

            # Construir el índice y a la vez subir originales, en lugar de
            # completar la exploración antes de poner las primeras fotos en R2.
            await checkpoint(g,folder,"catalogando")
            await catalog_batch(g,folder,client,entity,source)
            batch=max(1,min(12,int(os.getenv("GALERIAS_SYNC_BATCH_FILES","5"))))
            for _ in range(batch):
                await guard()
                item=await g.db().fetchrow("""SELECT message,kind,mime,size
                   FROM wg_media WHERE folder=$1 AND state='pending'
                    AND available<=$2 ORDER BY id DESC LIMIT 1""",
                    folder,time.time())
                if not item:break
                m=await client.get_messages(entity,ids=item["message"])
                await guard()
                if not m or media_kind(m)[0]!=item["kind"] or getattr(m,"noforwards",False):
                    await g.db().execute("""UPDATE wg_media
                      SET state='missing',error='no_disponible_en_origen'
                      WHERE folder=$1 AND message=$2 AND state='pending'""",
                      folder,item["message"])
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
        g.sync_cooldown_until=max(getattr(g,"sync_cooldown_until",0),
                                  time.monotonic()+max(90,seconds+45))
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
    if getattr(m,"noforwards",False) or getattr(m,"ttl_period",None) or getattr(getattr(m,"media",None),"ttl_seconds",None):
        return
    db=g.db()
    kind,mime=media_kind(m)
    if not kind:
        reply=getattr(getattr(m,"reply_to",None),"reply_to_msg_id",None)
        content=normal(getattr(m,"message","") or "")
        if reply and content:
            await db.execute("""INSERT INTO wg_replies(folder,parent_message,reply_message,words)
              VALUES($1,$2,$3,$4) ON CONFLICT(folder,reply_message)
              DO UPDATE SET words=excluded.words""",folder,reply,m.id,content[:1000])
            # Only index the relation; comment text never exposed as chat.
            await db.execute("""UPDATE wg_media SET search_text=left(search_text||' '||$3,4000)
              WHERE folder=$1 AND message=$2 AND position($3 in search_text)=0""",
              folder,reply,content[:1000])
        return
    caption=str(getattr(m,"message","") or "")[:1500]
    doc=getattr(m,"document",None)
    size=int(getattr(doc,"size",0) or 0)
    filename="video.mp4" if kind=="video" else "foto.jpg"
    attrs=getattr(doc,"attributes",[]) or []
    for attr in attrs:
        if hasattr(attr,"file_name"):
            filename=str(attr.file_name)[:110]
    reply=getattr(getattr(m,"reply_to",None),"reply_to_msg_id",None)
    grouped=str(m.grouped_id) if getattr(m,"grouped_id",None) else None
    other=await db.fetchval("""SELECT string_agg(words,' ') FROM wg_replies
      WHERE folder=$1 AND parent_message=$2""",folder,m.id) or ""
    await db.execute("""INSERT INTO wg_media(
      folder,message,kind,mime,filename,width,height,size,date,label,search_text,
      reply_to,grouped,created)
      VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
      ON CONFLICT(folder,message) DO NOTHING""",
      folder,m.id,kind,mime,filename,0,0,size,
      m.date.timestamp() if m.date else time.time(),
      caption,normal(caption+" "+other)[:4000],reply,grouped,time.time())



async def backup_one(g,folder,item,message,client,guard):
    """Idempotente: guarda original + miniatura y después marca ready en DB."""
    await guard()
    kind=item["kind"]
    mime=item["mime"] or ("image/jpeg" if kind=="photo" else "video/mp4")
    ending=".jpg" if kind=="photo" else (".mov" if mime=="video/quicktime" else ".mp4")
    key=f"folders/{folder}/{message.id}/original{ending}"
    thumb_key=f"folders/{folder}/{message.id}/thumb.jpg"

    original_size=await g.storage.object_size(key)
    thumb_size=await g.storage.object_size(thumb_key)
    known_size=int(item["size"] or 0)
    original_valid=original_size is not None and original_size>0 and (
        known_size==0 or original_size==known_size)
    if original_valid and thumb_size:
        await g.db().execute("""UPDATE wg_media SET state='ready',
          file_key=$3,thumb_key=$4,size=$5,error=''
          WHERE folder=$1 AND message=$2 AND state='pending'""",
          folder,message.id,key,thumb_key,original_size)
        return

    # Las fotos se descargan a temporal para crear miniaturas de alta calidad.
    # Video: streaming multiparte de Telegram a R2, con memoria < 16 MiB.
    with tempfile.TemporaryDirectory(prefix="wg-") as tmp:
        tmp=Path(tmp)
        thumb=tmp/"thumb.jpg"
        if kind=="photo" and not original_valid:
            image_path=tmp/"original.jpg"
            async def progress(_, total):
                await guard()
            await client.download_media(message,file=str(image_path),
                                        progress_callback=progress)
            if not image_path.is_file() or image_path.stat().st_size==0:
                raise ValueError("Imagen incompleta")
            with Image.open(image_path) as photo:
                photo=ImageOps.exif_transpose(photo)
                photo.thumbnail((640,640))
                if photo.mode!="RGB":photo=photo.convert("RGB")
                photo.save(thumb,format="JPEG",quality=88,optimize=True)
            await guard()
            await g.storage.upload(image_path,key,mime)
            original_size=image_path.stat().st_size
        elif kind=="video" and not original_valid:
            # Sin archivo temporal gigantesco ni duplicar el original en memoria.
            parts=client.iter_download(message.media,request_size=512*1024)
            original_size=await g.storage.upload_stream(
                parts,key,mime,check=guard)

        # Miniatura de foto si ya existía el original R2 pero faltaba el thumbnail:
        # descargar la foto de Telegram solo en ese caso.
        if kind=="photo" and not thumb.is_file() and not thumb_size:
            image_path=tmp/"source.jpg"
            await guard()
            async def progress(_, total):
                await guard()
            await client.download_media(message,file=str(image_path),
                                        progress_callback=progress)
            with Image.open(image_path) as photo:
                photo=ImageOps.exif_transpose(photo)
                photo.thumbnail((640,640))
                if photo.mode!="RGB":photo=photo.convert("RGB")
                photo.save(thumb,format="JPEG",quality=88,optimize=True)
        if kind=="video" and not thumb_size:
            # Usar la mayor miniatura disponible en Telegram, sin bajar el video 2 veces.
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
    # Las copias ya terminadas permanecen en R2 aunque después se oculte la carpeta.
    await g.db().execute("""UPDATE wg_media SET state='ready',file_key=$3,
      thumb_key=$4,size=$5,error='' WHERE folder=$1 AND message=$2
      AND state='pending'""",folder,message.id,key,
      thumb_key if thumb_size else None,original_size or 0)


async def run(g):
    """Un solo sincronizador incluso en despliegues solapados; seguimiento reanudable."""
    db=g.db()
    await db.execute(EXTRA)
    async with db.acquire() as leader:
        while not g.closed:
            if await leader.fetchval("SELECT pg_try_advisory_lock(784,19)"):
                break
            await asyncio.sleep(5)
        try:
            log.warning("Gallery mass backup active; sources only selected folders")
            while not g.closed:
                try:
                    if os.getenv("GALERIAS_SYNC_ENABLED","0")!="1" or not g.storage.ready:
                        await asyncio.sleep(10)
                        continue
                    remaining=getattr(g,"sync_cooldown_until",0)-time.monotonic()
                    if remaining>0:
                        await checkpoint(g,phase="esperando Telegram")
                        await asyncio.sleep(min(30,max(1,remaining)))
                        continue
                    await checkpoint(g,phase="esperando_carpetas")
                    folders=await db.fetch("""SELECT * FROM wg_folders WHERE active
                      ORDER BY last_check ASC,id ASC""")
                    if not folders:
                        await asyncio.sleep(5)
                        continue
                    for folder in folders:
                        if g.closed:break
                        if await folder_active(g,folder["id"]):
                            await sync_folder(g,folder)
                        await asyncio.sleep(max(5,min(120,int(
                          os.getenv("GALERIAS_SYNC_DELAY_SECONDS","15")))))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Gallery supervisor retrying (%s)",type(exc).__name__)
                    await asyncio.sleep(20)
        finally:
            await leader.execute("SELECT pg_advisory_unlock(784,19)")
