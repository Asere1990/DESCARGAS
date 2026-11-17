"""Single-lane resumable bulk worker. Deliberately separate from normal downloads.

A file is first stored in the existing private administrator ALMACEN.
Only then is the bot allowed to copy it to a user's validated destination.
Uncertain network sends remain for administrator review; never blindly replay.
"""
import asyncio
import json
import logging
import tempfile
from pathlib import Path
import time
import uuid

from telethon import types,utils,errors,functions

from . import bulk_transfer as bulk
from .service import MAX_FILE,MAX_ALBUM,prepare_media_for_bot
from .transfer import prepare_album
from .moderation import ensure_allowed
from .protected import require_entity

LOG=logging.getLogger("descargas.bulk.worker")
PAGE=80
LEASE=900

class ReviewRequired(Exception):pass
class RetryablePreflight(Exception):pass

async def _claim(s):
    lease=uuid.uuid4().hex
    now=time.time()
    return await s.repo.pool.fetchrow(
        """WITH next AS (
             SELECT id FROM descarga_bulk_jobs
             WHERE bot_id=$1 AND
               (status IN ('scanning','publishing')
                 OR (status IN ('waiting_scan','waiting_send') AND pause_until<=$2)
                 OR (status IN ('review','error') AND run_no>0
                     AND updated>$2-7200
                     AND ((status='review' AND error='El envío requiere revisión.')
                          OR (status='error' AND error IN
                             ('El archivo descargado no coincide con el tamaño original; no se envió.',
                              'El archivo sigue incompleto tras reintentar la descarga; requiere revisión.',
                              'La lectura directa de Telegram tampoco completó el archivo; requiere revisión manual.')))
                     AND NOT EXISTS (
                         SELECT 1 FROM descarga_bulk_deliveries d
                         WHERE d.job_id=descarga_bulk_jobs.id
                           AND d.run_no=descarga_bulk_jobs.run_no
                           AND d.status IN ('preparing','archived','review'))))
               AND lease_until<$2
             ORDER BY updated,id FOR UPDATE SKIP LOCKED LIMIT 1)
           UPDATE descarga_bulk_jobs j SET
             status=CASE WHEN j.status='waiting_scan' THEN 'scanning'
                         WHEN j.status='waiting_send' THEN 'publishing'
                         WHEN j.status IN ('review','error') THEN 'publishing'
                         ELSE j.status END,
             error=CASE WHEN j.status='review'
                        THEN 'Recuperación segura automática en curso.'
                        WHEN j.status='error'
                        THEN 'Reanudación verificada tras archivo incompleto.'
                        ELSE j.error END,
             lease=$3,lease_until=$4,updated=$2
           FROM next WHERE j.id=next.id RETURNING j.*""",
        s.bot_id,now,lease,now+LEASE)

async def _state(s,job,status=None,error="",wait=0):
    # The exact lease prevents stale workers from restarting a paused job.
    query="""UPDATE descarga_bulk_jobs
             SET status=COALESCE($3,status),error=$4,
                 pause_until=$5,lease=NULL,lease_until=0,updated=$6
             WHERE id=$1 AND lease=$2"""
    await s.repo.pool.execute(query,job["id"],job["lease"],status,
                              str(error)[:180],time.time()+wait if wait else 0,time.time())

async def _release(s,job):
    await s.repo.pool.execute(
        """UPDATE descarga_bulk_jobs SET lease=NULL,lease_until=0,updated=$3
           WHERE id=$1 AND lease=$2""",job["id"],job["lease"],time.time())

async def _heartbeat(s,job):
    while True:
        await asyncio.sleep(30)
        updated=await s.repo.pool.execute(
            """UPDATE descarga_bulk_jobs SET lease_until=$3
               WHERE id=$1 AND lease=$2 AND status IN
               ('scanning','publishing','paused')""",
            job["id"],job["lease"],time.time()+LEASE)
        if updated!="UPDATE 1":return

async def _verify_account(s,job):
    row=await s.repo.account(int(job["uid"]))
    if not row or not row["session"] or int(row["generation"])!=int(job["generation"]):
        raise ValueError("La sesión vinculada cambió. Vuelve a conectar y crear una transferencia.")
    await ensure_allowed(s,int(job["uid"]),int(job["source_id"]))
    rules=await s.repo.policy(s.bot_id,int(job["uid"]),int(job["source_id"]))
    if rules["locked"]:
        raise ValueError("El origen está bloqueado para transferencias.")
    return row

def _source_peer(job):
    real,kind=utils.resolve_id(int(job["source_id"]))
    if kind is types.PeerChannel:
        h=int(job["source_hash"] or 0)
        if not h:raise ValueError("No se guardó el acceso a ese canal.")
        return types.InputPeerChannel(real,h)
    if kind is types.PeerChat:return types.InputPeerChat(real)
    raise ValueError("El origen no es un grupo o canal.")

async def _source_client(s,job):
    await _verify_account(s,job)
    client=await bulk._client_for(s,int(job["uid"]))
    me=await client.get_me()
    if not me or int(me.id)!=int(job["uid"]):
        await client.disconnect()
        raise ValueError("La sesión no corresponde al solicitante.")
    return client

async def scan_one(s,job):
    # Very short account lease per page: no long scans that block normal downloads.
    async with s.repo.account_lock(int(job["uid"]),wait=False):
        client=await _source_client(s,job)
        try:
            peer=_source_peer(job)
            try:
                messages=[m async for m in client.iter_messages(
                    peer,min_id=int(job["scan_cursor"]),reverse=True,limit=PAGE)]
            except errors.FloodWaitError:raise
            except errors.RPCError as exc:
                raise ValueError("No se puede leer el historial del grupo original.") from exc
        finally:await client.disconnect()
    if not messages:
        await s.repo.pool.execute(
            """UPDATE descarga_bulk_jobs SET status='ready',
                 updated=$3 WHERE id=$1 AND lease=$2 AND status='scanning'""",
            job["id"],job["lease"],time.time())
        return
    messages.sort(key=lambda m:int(m.id))
    current=bulk._counts(job["counts"])
    cursor=int(job["scan_cursor"])
    items=[]
    for m in messages:
        if int(m.id)<=cursor:continue
        cursor=int(m.id)
        kind=bulk.category(m)
        reply=int(getattr(m,"reply_to_msg_id",0) or 0)
        caption=str(getattr(m,"message","") or "")[:8000]
        if kind:
            current[kind]+=1
            items.append((job["id"],int(m.id),kind,current[kind],
                          str(getattr(m,"grouped_id",None) or ""),
                          reply,caption,int(m.sender_id) if m.sender_id else None))
        elif reply and caption:
            # Text replies are indexed separately from selected media ranges.
            items.append((job["id"],int(m.id),"reply",0,"",
                          reply,caption,int(m.sender_id) if m.sender_id else None))
    async with s.repo.pool.acquire() as conn,conn.transaction():
        old=await conn.fetchrow(
            """SELECT scan_cursor,status FROM descarga_bulk_jobs WHERE id=$1
               AND lease=$2 FOR UPDATE""",job["id"],job["lease"])
        if not old or old["status"]!="scanning" or int(old["scan_cursor"])!=int(job["scan_cursor"]):
            return
        if items:
            await conn.executemany(
                """INSERT INTO descarga_bulk_items
                   (job_id,message_id,category,ordinal,album,reply_to,caption,sender_id)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                   ON CONFLICT(job_id,message_id) DO NOTHING""",items)
        await conn.execute(
            """UPDATE descarga_bulk_jobs SET scan_cursor=$3,counts=$4::jsonb,
               updated=$5 WHERE id=$1 AND lease=$2""",
            job["id"],job["lease"],cursor,json.dumps(current),time.time())

def _range_query(ranges,first_param=3):
    parts=[];params=[]
    for i,kind in enumerate(bulk.KINDS):
        left=first_param+i*2;right=left+1
        parts.append(f"(i.category='{kind}' AND i.ordinal>${left} AND i.ordinal<=${right})")
        params.extend(map(int,ranges[kind]))
    return " OR ".join(parts),params

async def _candidate(s,job,ranges):
    predicates,values=_range_query(ranges)
    return await s.repo.pool.fetchrow(
        f"""SELECT i.* FROM descarga_bulk_items i
            WHERE i.job_id=$1 AND ({predicates})
            AND NOT EXISTS(
               SELECT 1 FROM descarga_bulk_deliveries d
               WHERE d.job_id=$1 AND d.run_no=$2
                 AND d.message_id=i.message_id)
            ORDER BY i.message_id LIMIT 1""",
        job["id"],job["run_no"],*values)

async def _batch(s,job,first,ranges):
    if not first["album"]:return [first]
    predicates,values=_range_query(ranges,4)
    rows=await s.repo.pool.fetch(
        f"""SELECT i.* FROM descarga_bulk_items i
            WHERE i.job_id=$1 AND i.album=$3 AND ({predicates})
             AND NOT EXISTS(
                 SELECT 1 FROM descarga_bulk_deliveries d
                 WHERE d.job_id=$1 AND d.run_no=$2
                   AND d.message_id=i.message_id)
            ORDER BY i.message_id LIMIT 10""",
        job["id"],job["run_no"],str(first["album"]),*values)
    return rows or [first]

async def _inflight(s,job):
    return await s.repo.pool.fetchval(
        """SELECT 1 FROM descarga_bulk_deliveries WHERE job_id=$1 AND run_no=$2
           AND status IN ('preparing','archived','review') LIMIT 1""",
        job["id"],job["run_no"])

# Only file-level preparation failures can be skipped. Access, capacity,
# policy, Telegram delivery uncertainty and database failures remain blockers.
SKIPPABLE_MEDIA_ERRORS=(
    "El archivo descargado no coincide con el tamaño original; no se envió.",
    "El archivo no se descargó completo.",
    "El archivo supera el límite de descarga.",
    "Archivo superior al límite permitido.",
    "Álbum superior al límite permitido.",
    "Algunos archivos del grupo original ya no están disponibles.",
    "El mensaje no contiene multimedia compatible.",
)

def _skippable_media_error(exc):
    return isinstance(exc,ValueError) and str(exc) in SKIPPABLE_MEDIA_ERRORS

async def _skip_media(s,job,items,error):
    # Claim only after proving that no delivery was ever prepared or sent.
    # A skipped row is excluded by _candidate so the next pass advances.
    now=time.time()
    await s.repo.pool.executemany(
        """INSERT INTO descarga_bulk_deliveries
           (job_id,run_no,message_id,category,status,error,updated)
           VALUES($1,$2,$3,$4,'skipped',$5,$6)
           ON CONFLICT(job_id,run_no,message_id) DO NOTHING""",
        [(job["id"],job["run_no"],int(item["message_id"]),
          item["category"],str(error)[:160],now) for item in items])
    await s.repo.pool.execute(
        """UPDATE descarga_bulk_jobs SET error='',updated=$3
           WHERE id=$1 AND lease=$2 AND status='publishing'""",
        job["id"],job["lease"],now)
    LOG.warning("Bulk skipped an undeliverable media item (preflight only)")


async def _storage_peer(s):
    if not s.almacen:raise ValueError("No disponible.")
    try:return await s.bot.get_input_entity(s.almacen)
    except ValueError:
        cid,kind=utils.resolve_id(s.almacen)
        if kind is types.PeerChannel:
            result=await s.bot(functions.channels.GetChannelsRequest(
                [types.InputChannel(cid,0)]))
            return utils.get_input_peer(result.chats[0])
        if kind is types.PeerChat:return types.InputPeerChat(cid)
        raise ValueError("Archivo privado no disponible.")

async def _claim_entries(s,job,items):
    # Claim a whole album atomically so an overlapping worker cannot resend it.
    now=time.time()
    async with s.repo.pool.acquire() as conn,conn.transaction():
        for item in items:
            await conn.execute(
                """INSERT INTO descarga_bulk_deliveries
                   (job_id,run_no,message_id,category,status,updated)
                   VALUES($1,$2,$3,$4,'preparing',$5)
                   ON CONFLICT DO NOTHING""",
                job["id"],job["run_no"],int(item["message_id"]),item["category"],now)

async def _checkpoint_archive(s,job,items,archived):
    async with s.repo.pool.acquire() as conn,conn.transaction():
        await conn.executemany(
            """UPDATE descarga_bulk_deliveries SET status='archived',
               archive_id=$4,updated=$5
               WHERE job_id=$1 AND run_no=$2 AND message_id=$3
                 AND status='preparing'""",
            [(job["id"],job["run_no"],int(item["message_id"]),int(message.id),time.time())
             for item,message in zip(items,archived)])

async def _checkpoint_published(s,job,items,archived,posted):
    now=time.time()
    async with s.repo.pool.acquire() as conn,conn.transaction():
        await conn.executemany(
            """UPDATE descarga_bulk_deliveries SET status='published',
               destination_id=$4,updated=$5
               WHERE job_id=$1 AND run_no=$2 AND message_id=$3
                 AND status='archived'""",
            [(job["id"],job["run_no"],int(item["message_id"]),int(mid),now)
             for item,mid in zip(items,posted)])
        await conn.executemany(
            """INSERT INTO descarga_bulk_audit
               (job_id,run_no,message_id,actor,source_id,target_id,
                archive_id,destination_id,original_author,caption,original_reply,created)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               ON CONFLICT DO NOTHING""",
            [(job["id"],job["run_no"],int(item["message_id"]),
              int(job["uid"]),int(job["source_id"]),int(job["target_id"]),
              int(msg.id),int(dest),item["sender_id"],str(item["caption"] or "")[:8000],
              int(item["reply_to"]),now)
             for item,msg,dest in zip(items,archived,posted)])
        await conn.execute(
            """UPDATE descarga_bulk_jobs SET run_done=run_done+$3,error='',updated=$4
               WHERE id=$1 AND run_no=$2 AND status IN ('publishing','paused')""",
            job["id"],job["run_no"],len(posted),now)

async def _mark_review(s,job,items,error):
    await s.repo.pool.execute(
        """UPDATE descarga_bulk_deliveries SET status='review',error=$4,updated=$5
           WHERE job_id=$1 AND run_no=$2 AND message_id=ANY($3::bigint[])
             AND status IN ('preparing','archived')""",
        job["id"],job["run_no"],[int(r["message_id"]) for r in items],
        str(error)[:160],time.time())

async def _archive_note(s,job,items,archived):
    # This information is never returned in a regular user's Mini App.
    first=items[0]
    text=(f"CONTROL DE TRANSFERENCIA\n"
          f"Solicitante: {job['uid']}\n"
          f"Origen: {job['source_title']} ({job['source_id']})\n"
          f"Destino: {job['target_title']} ({job['target_id']})\n"
          f"Mensajes originales: {', '.join(str(r['message_id']) for r in items)}\n"
          f"Autores: {', '.join(str(r['sender_id'] or '-') for r in items)}")
    try:
        await s.api("sendMessage",{"chat_id":s.almacen,"text":text,
           "reply_parameters":{"message_id":int(archived[0].id)},
           "disable_notification":True})
    except Exception as exc:
        LOG.warning("Bulk private control note deferred (%s)",type(exc).__name__)

async def _copy_to_target(s,job,items,archived):
    mids=[int(m.id) for m in archived]
    if len(mids)!=len(items):
        raise ReviewRequired("Álbum incompleto en el archivo privado.")
    payload={"chat_id":int(job["target_id"]),"from_chat_id":s.almacen,
             "disable_notification":True}
    if len(mids)==1:
        original=int(items[0]["reply_to"] or 0)
        if original:
            parent=await s.repo.pool.fetchval(
                """SELECT destination_id FROM descarga_bulk_deliveries
                   WHERE job_id=$1 AND run_no=$2 AND message_id=$3
                     AND status='published'""",
                job["id"],job["run_no"],original)
            if parent:payload["reply_parameters"]={"message_id":int(parent)}
        payload["message_id"]=mids[0]
        result=await s.api("copyMessage",payload)
        return [int(result["message_id"])]
    payload["message_ids"]=mids
    result=await s.api("copyMessages",payload)
    return [int(x["message_id"]) for x in (result or [])]

async def send_media(s,job,items):
    ids=[int(item["message_id"]) for item in items]
    await s.repo.pace("bulk-upload",2.8*max(1,len(items)))
    await bulk.check_destination(s,int(job["uid"]),int(job["target_id"]))
    async with s.repo.account_lock(int(job["uid"]),wait=False):
        client=await _source_client(s,job)
        try:
            peer=_source_peer(job)
            original=await client.get_messages(peer,ids=ids)
            if not isinstance(original,list):original=[original]
            present={int(m.id):m for m in original if m and bulk.category(m)}
            if len(present)!=len(items):
                raise ValueError("Algunos archivos del grupo original ya no están disponibles.")
            ordered=[present[mid] for mid in ids]
            if any((m.file.size or 0)>MAX_FILE for m in ordered):
                raise ValueError("Archivo superior al límite permitido.")
            if sum(m.file.size or 0 for m in ordered)>MAX_ALBUM:
                raise ValueError("Álbum superior al límite permitido.")
            with tempfile.TemporaryDirectory(prefix="descargas-bulk-") as temp:
                async def prepare(path,item):
                    return await prepare_media_for_bot(s.bot,client,path,item)
                media=await prepare_album(client,ordered,temp,prepare,MAX_FILE,MAX_ALBUM)
                # Claim immediately before Telegram posting, not before
                # acquiring the user's session. A busy account is retryable.
                # Resolving the archive peer can fail transiently and has no
                # publishing side effect. Only claim after it is resolved.
                archive_peer=await _storage_peer(s)
                await _claim_entries(s,job,items)
                # The archive is the durable first stop. A failed archive
                # never causes a file to be sent to the user destination.
                try:
                    saved=await s.bot.send_file(archive_peer,
                        media if len(media)>1 else media[0],
                        caption=[m.message or "" for m in ordered] if len(media)>1
                        else ordered[0].message or "",
                        formatting_entities=[m.entities or [] for m in ordered]
                        if len(media)>1 else ordered[0].entities or [],
                        parse_mode=None)
                except errors.FloodWaitError:
                    # Explicit Telegram rejection of the one batch request:
                    # no archive was accepted, so its unconfirmed claims may
                    # be retried after the exact documented cooldown.
                    await s.repo.pool.execute(
                        """DELETE FROM descarga_bulk_deliveries
                           WHERE job_id=$1 AND run_no=$2
                             AND message_id=ANY($3::bigint[])
                             AND status='preparing' AND archive_id IS NULL""",
                        job["id"],job["run_no"],ids)
                    raise
        finally:await client.disconnect()
    saved=saved if isinstance(saved,list) else [saved]
    if len(saved)!=len(items):
        raise ReviewRequired("No se confirmó el archivo completo.")
    await _checkpoint_archive(s,job,items,saved)
    await _archive_note(s,job,items,saved)
    # A manual pause affects only the NEXT batch; this confirmed album completes.
    posted=await _copy_to_target(s,job,items,saved)
    if len(posted)!=len(items):
        raise ReviewRequired("Telegram no confirmó la copia del álbum.")
    await _checkpoint_published(s,job,items,saved,posted)

async def _reply_candidate(s,job):
    # Replicate text replies only when their original parent was delivered.
    return await s.repo.pool.fetchrow(
        """SELECT i.*,
             p.destination_id AS parent_dest
           FROM descarga_bulk_items i
           JOIN descarga_bulk_deliveries p ON
             p.job_id=i.job_id AND p.run_no=$2
             AND p.message_id=i.reply_to AND p.status='published'
           WHERE i.job_id=$1 AND i.category='reply'
             AND NOT EXISTS(
               SELECT 1 FROM descarga_bulk_deliveries d
               WHERE d.job_id=$1 AND d.run_no=$2
                 AND d.message_id=i.message_id)
           ORDER BY i.message_id LIMIT 1""",
        job["id"],job["run_no"])

async def _publish_reply(s,job,item):
    # Text replies are not included in photo/video range counts.
    await _claim_entries(s,job,[item])
    note=str(item["caption"] or "")
    if not note:return
    try:
        archive_parent=await s.repo.pool.fetchval(
            """SELECT archive_id FROM descarga_bulk_deliveries
               WHERE job_id=$1 AND run_no=$2 AND message_id=$3
                 AND status='published'""",
            job["id"],job["run_no"],int(item["reply_to"]))
        archival={"chat_id":s.almacen,"text":note[:4096],
                  "disable_notification":True}
        if archive_parent:
            archival["reply_parameters"]={"message_id":int(archive_parent)}
        archive=await s.api("sendMessage",archival)
        mid=int(archive["message_id"])
        await s.repo.pool.execute(
            """UPDATE descarga_bulk_deliveries SET status='archived',
               archive_id=$4,updated=$5
               WHERE job_id=$1 AND run_no=$2 AND message_id=$3
                 AND status='preparing'""",
            job["id"],job["run_no"],int(item["message_id"]),mid,time.time())
        dest=await s.api("copyMessage",{"chat_id":int(job["target_id"]),
            "from_chat_id":s.almacen,"message_id":mid,
            "reply_parameters":{"message_id":int(item["parent_dest"])},
            "disable_notification":True})
        did=int(dest["message_id"])
        await s.repo.pool.execute(
            """UPDATE descarga_bulk_deliveries SET status='published',
               destination_id=$4,updated=$5
               WHERE job_id=$1 AND run_no=$2 AND message_id=$3
                 AND status='archived'""",
            job["id"],job["run_no"],int(item["message_id"]),did,time.time())
        await s.repo.pool.execute(
            """INSERT INTO descarga_bulk_audit
               (job_id,run_no,message_id,actor,source_id,target_id,
                archive_id,destination_id,original_author,caption,original_reply,created)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               ON CONFLICT DO NOTHING""",
            job["id"],job["run_no"],int(item["message_id"]),
            int(job["uid"]),int(job["source_id"]),int(job["target_id"]),mid,did,
            item["sender_id"],note,int(item["reply_to"]),time.time())
    except asyncio.CancelledError:raise
    except Exception as exc:
        await _mark_review(s,job,[item],type(exc).__name__)
        raise ReviewRequired("Respuesta pendiente de revisión.") from exc

async def _send_or_skip(s,job,rows):
    try:
        await send_media(s,job,rows)
    except (asyncio.CancelledError,errors.FloodWaitError):
        raise
    except ValueError as exc:
        if "transferencia en curso" in str(exc).lower():
            raise
        if await _inflight(s,job):
            # An upload may already be at the private archive or destination.
            # Never skip or retry any such uncertain delivery.
            await _mark_review(s,job,rows,type(exc).__name__)
            raise ReviewRequired("El envío requiere revisión.") from exc
        if not _skippable_media_error(exc):
            raise
        if len(rows)>1:
            # An album with one bad file must not discard its other items.
            # Reconstruct members individually only after proving no send.
            for single in rows:
                await _send_or_skip(s,job,[single])
        else:
            await _skip_media(s,job,rows,exc)
        return
    except Exception as exc:
        if not await _inflight(s,job):
            raise RetryablePreflight(type(exc).__name__) from exc
        await _mark_review(s,job,rows,type(exc).__name__)
        raise ReviewRequired("El envío requiere revisión.") from exc


async def publish_one(s,job):
    run=await s.repo.pool.fetchrow(
        "SELECT ranges FROM descarga_bulk_runs WHERE job_id=$1 AND run_no=$2",
        job["id"],job["run_no"])
    if not run:
        raise ValueError("No hay un rango activo.")
    raw=run["ranges"];ranges=json.loads(raw) if isinstance(raw,str) else dict(raw)
    if await _inflight(s,job):
        raise ReviewRequired("Hay un envío anterior sin confirmar.")
    item=await _candidate(s,job,ranges)
    if item:
        # Current range is checked before claiming, not after an ambiguous send.
        rows=await _batch(s,job,item,ranges) if item["album"] else [item]
        # Before each file/album, keep ordinary block and membership checks.
        await _verify_account(s,job)
        if not s.almacen:raise ValueError("Esta función está temporalmente deshabilitada.")
        await _send_or_skip(s,job,rows)
        return
    reply=await _reply_candidate(s,job)
    if reply:
        await _publish_reply(s,job,reply)
        return
    # Recompute even if user paused after the last send.
    await s.repo.pool.execute(
        """UPDATE descarga_bulk_jobs SET status='finished',error='',updated=$3
           WHERE id=$1 AND lease=$2 AND status='publishing'""",
        job["id"],job["lease"],time.time())
    await s.repo.pool.execute(
        """UPDATE descarga_bulk_runs SET status='finished',finished=$3
           WHERE job_id=$1 AND run_no=$2""",
        job["id"],job["run_no"],time.time())

async def run(s):
    # No busy loop, no account-login operations, no interference with old queues.
    LOG.warning("DESCARGAS isolated bulk worker started")
    while not s.stopping:
        claimed=None;heartbeat=None
        try:
            claimed=await _claim(s)
            if not claimed:
                await asyncio.sleep(3);continue
            heartbeat=asyncio.create_task(_heartbeat(s,claimed))
            if claimed["status"]=="scanning":
                await scan_one(s,claimed)
            elif claimed["status"]=="publishing":
                await publish_one(s,claimed)
        except asyncio.CancelledError:raise
        except errors.FloodWaitError as exc:
            # Explicit Telegram rate limit, with no speculative replay of sends.
            try:
                await _state(s,claimed,
                    "waiting_scan" if claimed["status"]=="scanning" else "waiting_send",
                    "Telegram pide esperar.",int(exc.seconds)+3)
            except Exception:
                LOG.warning("Could not persist bulk wait")
        except RetryablePreflight as exc:
            # The failed operation took place before claiming any delivery:
            # back off and retry automatically, with a fixed upper bound.
            previous=str(claimed["error"] or "")
            attempt=1
            if previous.startswith("Intento de preparación "):
                try:attempt=int(previous.split(" ",3)[3].split("/",1)[0])+1
                except (ValueError,IndexError):pass
            LOG.warning("Bulk safe preflight failed attempt=%s type=%s",attempt,str(exc))
            try:
                if attempt>=5:
                    await _state(s,claimed,"error",
                                 f"No se pudo preparar el archivo tras 5 intentos ({exc}).")
                else:
                    await _state(s,claimed,"waiting_send",
                                 f"Intento de preparación {attempt}/5: {exc}",
                                 wait=min(180,20*attempt))
            except Exception:LOG.warning("Could not persist bulk preflight wait")
        except ReviewRequired as exc:
            repeated=(str(claimed["error"] or "")==
                      "Recuperación segura automática en curso.")
            if repeated:
                LOG.warning("Bulk safe recovery ended in a confirmed review state")
            try:
                await _state(s,claimed,"review",
                    "Reintento seguro agotado; hay un envío pendiente de revisión."
                    if repeated else str(exc))
            except Exception:LOG.warning("Could not persist bulk review")
        except ValueError as exc:
            if "transferencia en curso" in str(exc).lower():
                # The normal downloader owns the linked session; back off,
                # never label the user job broken for this temporary conflict.
                await asyncio.sleep(4)
                continue
            reason=str(exc)
            if (reason=="El archivo descargado no coincide con el tamaño original; no se envió."
                    and str(claimed["error"] or "")==
                        "Reanudación verificada tras archivo incompleto."):
                reason="La lectura directa de Telegram tampoco completó el archivo; requiere revisión manual."
            if "archivo" in reason.lower() and "incomplet" in reason.lower():
                LOG.warning("Bulk media size validation failed after bounded retry")
            try:await _state(s,claimed,"error",reason)
            except Exception:LOG.warning("Could not persist bulk validation error")
        except Exception as exc:
            LOG.warning("Bulk worker step deferred (%s)",type(exc).__name__)
            try:await _state(s,claimed,"error","No se pudo confirmar la operación.")
            except Exception:LOG.warning("Bulk worker state unavailable")
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat,return_exceptions=True)
            if claimed:
                try:await _release(s,claimed)
                except Exception:LOG.warning("Bulk worker lease release delayed")
        await asyncio.sleep(0.1)
