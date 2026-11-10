"""Respaldo progresivo: una cuenta, un pequeño lote, sin modificar sesiones.
No se ejecuta hasta configurar R2 y GALERIAS_SYNC_ENABLED=1.
Respeta el bloqueo ya usado por DESCARGAS y salta cuentas ocupadas.
"""
import asyncio
import io
import logging
import os
import tempfile
import time
from pathlib import Path
from telethon import errors
from PIL import Image,ImageOps
from .core import normal

log=logging.getLogger("web-galerias-sync")
EXTRA="""
CREATE TABLE IF NOT EXISTS wg_discovery(
 folder BIGINT NOT NULL,uid BIGINT NOT NULL,
 tried_at DOUBLE PRECISION NOT NULL DEFAULT 0,works BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(folder,uid));
CREATE TABLE IF NOT EXISTS wg_replies(
 folder BIGINT NOT NULL,parent_message BIGINT NOT NULL,reply_message BIGINT NOT NULL,
 words TEXT NOT NULL, PRIMARY KEY(folder,reply_message));
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS source_total BIGINT NOT NULL DEFAULT 0;
"""
def media_kind(m):
    if getattr(m,"photo",None):return "photo","image/jpeg"
    doc=getattr(m,"document",None)
    mime=str(getattr(doc,"mime_type","") or "")
    if mime.startswith("video/"):return "video",mime
    return None,None

async def find_dialog(client,peer):
    # Un único dialog fetch por cuenta; sin join, enviar mensajes o cambios de permisos.
    async for dialog in client.iter_dialogs():
        if int(dialog.id)==int(peer):return dialog.entity
    return None

async def choose_account(g,folder):
    db=g.db();now=time.time()
    row=await db.fetchrow("""SELECT a.uid FROM descarga_accounts a
      LEFT JOIN wg_discovery d ON d.folder=$1 AND d.uid=a.uid
      WHERE a.session IS NOT NULL AND (a.uid=$3 OR d.works OR d.tried_at IS NULL OR d.tried_at<$2)
      ORDER BY CASE WHEN a.uid=$3 THEN 0 WHEN d.works THEN 1 ELSE 2 END,
               COALESCE(d.tried_at,0),a.uid LIMIT 1""",
      folder["id"],now-900,folder["source_uid"])
    return row["uid"] if row else None

async def sync_folder(g,folder):
    from descargas_app.account_chat import connected
    db=g.db()
    uid=await choose_account(g,folder)
    if uid is None:
        await db.execute("""UPDATE wg_folders SET error='Comprobando cuentas alternativas'
           WHERE id=$1""",folder["id"])
        return
    # No esperar: si DESCARGAS está usando esta cuenta, saltar sin interferir.
    pending=await db.fetchval("""SELECT 1 FROM descarga_jobs WHERE uid=$1
      AND state IN ('queued','preparing','sending') LIMIT 1""",uid)
    if pending:return
    try:
        async with connected(g.s,uid) as client:
            entity=await find_dialog(client,folder["peer"])
            if entity is None:
                await db.execute("""INSERT INTO wg_discovery(folder,uid,tried_at,works)
                  VALUES($1,$2,$3,FALSE) ON CONFLICT(folder,uid)
                  DO UPDATE SET tried_at=excluded.tried_at,works=FALSE""",
                  folder["id"],uid,time.time())
                await db.execute("UPDATE wg_folders SET source_uid=NULL WHERE id=$1 AND source_uid=$2",
                                 folder["id"],uid)
                return
            await db.execute("""INSERT INTO wg_discovery(folder,uid,tried_at,works)
                VALUES($1,$2,$3,TRUE) ON CONFLICT(folder,uid)
                DO UPDATE SET tried_at=excluded.tried_at,works=TRUE""",
                folder["id"],uid,time.time())
            await db.execute("""UPDATE wg_folders SET source_uid=$2,last_check=$3,error=''
                WHERE id=$1""",folder["id"],uid,time.time())
            # Respaldo prioritario de solo un archivo antes de catalogar más.
            item=await db.fetchrow("""SELECT message,kind,mime,size FROM wg_media
               WHERE folder=$1 AND state='pending' ORDER BY id DESC LIMIT 1""",folder["id"])
            if item:
                m=await client.get_messages(entity,ids=item["message"])
                if m and media_kind(m)[0]==item["kind"]:
                    await backup_one(g,folder["id"],item,m,client)
                    return
            if not folder["complete"]:
                rows=[]
                async for m in client.iter_messages(entity,offset_id=folder["scan_cursor"] or 0,limit=25):
                    rows.append(m)
                if not rows:
                    await db.execute("UPDATE wg_folders SET complete=TRUE WHERE id=$1",folder["id"])
                else:
                    for m in rows:await catalog(g,folder["id"],m)
                    top=max(x.id for x in rows)
                    low=min(x.id for x in rows)
                    await db.execute("""UPDATE wg_folders SET scan_cursor=$2,
                        high_water=GREATEST(high_water,$3),
                        scan_count=scan_count+$4 WHERE id=$1""",
                        folder["id"],low,top,len(rows))
            else:
                rows=[]
                async for m in client.iter_messages(
                    entity,min_id=folder["high_water"],reverse=True,limit=25):
                    rows.append(m)
                for m in rows:await catalog(g,folder["id"],m)
                if rows:
                    await db.execute("""UPDATE wg_folders SET
                      high_water=GREATEST(high_water,$2),scan_count=scan_count+$3
                      WHERE id=$1""",folder["id"],max(x.id for x in rows),len(rows))
    except asyncio.CancelledError:raise
    except ValueError as exc:
        # Cuenta ocupada: ceder; sesión revocada: buscar otra cuenta disponible.
        if "Espera a que termine" not in str(exc):
            await db.execute("""INSERT INTO wg_discovery(folder,uid,tried_at,works)
              VALUES($1,$2,$3,FALSE) ON CONFLICT(folder,uid)
              DO UPDATE SET tried_at=excluded.tried_at,works=FALSE""",
              folder["id"],uid,time.time())
            await db.execute("UPDATE wg_folders SET source_uid=NULL WHERE id=$1 AND source_uid=$2",
                             folder["id"],uid)
        return
    except (errors.RPCError,OSError,TimeoutError) as exc:
        log.warning("Gallery sync deferred (%s)",type(exc).__name__)
        await db.execute("UPDATE wg_folders SET error=$2 WHERE id=$1",
                         folder["id"],type(exc).__name__)
        await db.execute("""INSERT INTO wg_discovery(folder,uid,tried_at,works)
          VALUES($1,$2,$3,FALSE) ON CONFLICT(folder,uid)
          DO UPDATE SET tried_at=excluded.tried_at,works=FALSE""",
          folder["id"],uid,time.time())
    except Exception as exc:
        log.warning("Gallery background delayed (%s)",type(exc).__name__)

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

async def backup_one(g,folder,item,message,client):
    # Nada de imágenes borrosas: miniaturas 640px generadas a partir de la foto real.
    # Los videos conservan su archivo original y miniatura si Telegram la proporciona.
    if getattr(message,"noforwards",False):return
    kind=item["kind"];suffix=".jpg" if kind=="photo" else ".mp4"
    cap=int(os.getenv("GALERIAS_MAX_BACKUP_MB","128"))*1024*1024
    if item["size"] and item["size"]>cap:
        # No bloquear el índice por archivos superiores al límite de seguridad.
        await g.db().execute("UPDATE wg_media SET state='skipped' WHERE folder=$1 AND message=$2",
                             folder,item["message"])
        return
    with tempfile.TemporaryDirectory(prefix="wg-") as tmp:
        original=str(Path(tmp)/("original"+suffix))
        downloaded=await client.download_media(message,file=original)
        if not downloaded:return
        path=Path(downloaded)
        if not path.is_file():return
        if path.stat().st_size>cap:
            await g.db().execute("UPDATE wg_media SET state='skipped' WHERE folder=$1 AND message=$2",
                                 folder,message.id)
            return
        actual_size=path.stat().st_size
        thumb=Path(tmp)/"thumb.jpg"
        if kind=="photo":
            with Image.open(path) as image:
                image=ImageOps.exif_transpose(image)
                image.thumbnail((640,640))
                if image.mode!="RGB":image=image.convert("RGB")
                image.save(thumb,format="JPEG",quality=88,optimize=True)
        else:
            # Intentar utilizar miniatura nativa sin descargar otra vez el video.
            try:
                native=await client.download_media(message,file=str(Path(tmp)/"vthumb"),thumb=0)
                if native and Path(native).is_file():
                    with Image.open(native) as image:
                        image.thumbnail((640,640))
                        image.convert("RGB").save(thumb,format="JPEG",quality=85)
            except Exception:
                pass
            if not thumb.exists():
                try:
                    import imageio_ffmpeg
                    import subprocess
                    exe=imageio_ffmpeg.get_ffmpeg_exe()
                    subprocess.run([exe,"-hide_banner","-loglevel","error","-ss","1",
                      "-i",str(path),"-frames:v","1","-vf","scale=640:-2",
                      "-y",str(thumb)],timeout=25,check=True,
                      stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                except Exception:pass
        key=f"folders/{folder}/{message.id}/original{path.suffix or suffix}"
        thumb_key=f"folders/{folder}/{message.id}/thumb.jpg" if thumb.exists() else None
        await g.storage.upload(path,key,item["mime"] or ("image/jpeg" if kind=="photo" else "video/mp4"))
        if thumb_key:await g.storage.upload(thumb,thumb_key,"image/jpeg")
    await g.db().execute("""UPDATE wg_media SET state='ready',file_key=$3,
      thumb_key=$4,size=$5 WHERE folder=$1 AND message=$2 AND state='pending'""",
      folder,message.id,key,thumb_key,actual_size)

async def run(g):
    db=g.db()
    await db.execute(EXTRA)
    while not g.closed:
        try:
            # Control administrativo: puede detenerse en Render, sin efecto sobre DESCARGAS.
            if os.getenv("GALERIAS_SYNC_ENABLED","0")!="1" or not g.storage.ready:
                await asyncio.sleep(60);continue
            folders=await db.fetch("SELECT * FROM wg_folders WHERE active ORDER BY last_check ASC,id ASC")
            if not folders:
                await asyncio.sleep(30);continue
            for folder in folders:
                if g.closed:break
                await sync_folder(g,folder)
                # Pausa conservadora entre accesos a Telegram; los trabajadores tienen prioridad.
                await asyncio.sleep(max(15,int(os.getenv("GALERIAS_SYNC_DELAY_SECONDS","35"))))
        except asyncio.CancelledError:raise
        except Exception as exc:
            log.warning("Gallery scheduler retry (%s)",type(exc).__name__)
            await asyncio.sleep(30)
