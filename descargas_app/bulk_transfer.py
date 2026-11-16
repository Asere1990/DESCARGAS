"""Isolated user-initiated group transfers for DESCARGAS Mini App.

No mutation of the ordinary download queue, linked account sessions,
export cursors, or existing storage. Admin review is separate and server-side.
"""
import asyncio
import json
import logging
import time
import uuid

from telethon import types, utils
from .moderation import ensure_allowed
from .creator_access import police
from .protected import sources as protected_sources

LOG=logging.getLogger("descargas.bulk")
KINDS=("photos","videos","gifs","photo_files","video_files")
MAX_DIALOGS=1500
MAX_DESTINATIONS=150
SCHEMA="""
CREATE TABLE IF NOT EXISTS descarga_bulk_chats(
 bot_id BIGINT NOT NULL,chat_id BIGINT NOT NULL,title TEXT NOT NULL,
 chat_type TEXT NOT NULL,active BOOLEAN NOT NULL DEFAULT TRUE,
 seen_at DOUBLE PRECISION NOT NULL,PRIMARY KEY(bot_id,chat_id));
CREATE TABLE IF NOT EXISTS descarga_bulk_jobs(
 id UUID PRIMARY KEY,bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,
 generation BIGINT NOT NULL,source_id BIGINT NOT NULL,
 source_hash BIGINT NOT NULL DEFAULT 0,source_title TEXT NOT NULL,
 target_id BIGINT NOT NULL,target_title TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'scanning',
 scan_cursor BIGINT NOT NULL DEFAULT 0,
 counts JSONB NOT NULL DEFAULT '{}'::jsonb,
 run_no INTEGER NOT NULL DEFAULT 0,run_total BIGINT NOT NULL DEFAULT 0,
 run_done BIGINT NOT NULL DEFAULT 0,
 lease TEXT,lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
 pause_until DOUBLE PRECISION NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '',
 created DOUBLE PRECISION NOT NULL,updated DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS descarga_bulk_jobs_owner ON descarga_bulk_jobs(bot_id,uid,created DESC);
CREATE INDEX IF NOT EXISTS descarga_bulk_jobs_ready ON descarga_bulk_jobs(bot_id,status,lease_until);
CREATE TABLE IF NOT EXISTS descarga_bulk_items(
 job_id UUID NOT NULL REFERENCES descarga_bulk_jobs(id),
 message_id BIGINT NOT NULL,category TEXT NOT NULL,
 ordinal BIGINT NOT NULL DEFAULT 0,
 album TEXT NOT NULL DEFAULT '',reply_to BIGINT NOT NULL DEFAULT 0,
 caption TEXT NOT NULL DEFAULT '',sender_id BIGINT,
 PRIMARY KEY(job_id,message_id));
CREATE INDEX IF NOT EXISTS descarga_bulk_items_range
 ON descarga_bulk_items(job_id,category,ordinal);
CREATE INDEX IF NOT EXISTS descarga_bulk_items_album
 ON descarga_bulk_items(job_id,album);
CREATE TABLE IF NOT EXISTS descarga_bulk_runs(
 job_id UUID NOT NULL REFERENCES descarga_bulk_jobs(id),
 run_no INTEGER NOT NULL,ranges JSONB NOT NULL,
 selected BIGINT NOT NULL,started DOUBLE PRECISION NOT NULL,
 finished DOUBLE PRECISION,status TEXT NOT NULL DEFAULT 'active',
 PRIMARY KEY(job_id,run_no));
CREATE TABLE IF NOT EXISTS descarga_bulk_deliveries(
 job_id UUID NOT NULL,run_no INTEGER NOT NULL,
 message_id BIGINT NOT NULL,category TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',archive_id BIGINT,
 destination_id BIGINT,error TEXT NOT NULL DEFAULT '',
 updated DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(job_id,run_no,message_id),
 FOREIGN KEY(job_id,run_no) REFERENCES descarga_bulk_runs(job_id,run_no));
CREATE INDEX IF NOT EXISTS descarga_bulk_deliveries_status
 ON descarga_bulk_deliveries(job_id,run_no,status);
CREATE TABLE IF NOT EXISTS descarga_bulk_audit(
 job_id UUID NOT NULL,run_no INTEGER NOT NULL,message_id BIGINT NOT NULL,
 actor BIGINT NOT NULL,source_id BIGINT NOT NULL,target_id BIGINT NOT NULL,
 archive_id BIGINT NOT NULL,destination_id BIGINT,
 original_author BIGINT,caption TEXT NOT NULL DEFAULT '',
 original_reply BIGINT NOT NULL DEFAULT 0,
 created DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(job_id,run_no,message_id));
"""

def _int(value):
    if type(value) is not int or not 0<value<2**63:
        raise ValueError("Identificador no válido.")
    return value

def _chat(value):
    if type(value) is not int or not -(2**63)<value<0:
        raise ValueError("Selecciona un grupo o canal válido.")
    return value

def _counts(raw):
    if isinstance(raw,str):raw=json.loads(raw)
    return {kind:int((raw or {}).get(kind,0)) for kind in KINDS}

def category(message):
    if getattr(message,"photo",None):return "photos"
    if getattr(message,"gif",None):return "gifs"
    if getattr(message,"video",None):return "videos"
    document=getattr(message,"document",None)
    mime=str(getattr(document,"mime_type","") or "").lower()
    if document and mime.startswith("image/"):return "photo_files"
    if document and mime.startswith("video/"):return "video_files"
    return ""

def _role(value):
    return (value or {}).get("status") in ("administrator","creator")

async def observe_chat(service,update):
    """Persist only bot-visible membership updates. No message content stored."""
    if not service.bot_id or not getattr(service.repo,"pool",None):return
    event=update.get("my_chat_member")
    message=update.get("message") or update.get("channel_post")
    info=(event or {}).get("chat") if event else (message or {}).get("chat")
    if not isinstance(info,dict) or info.get("type") not in ("group","supergroup","channel"):
        return
    chat_id=info.get("id")
    if type(chat_id) is not int or chat_id>=0:return
    current=(event or {}).get("new_chat_member") or {}
    active=current.get("status") not in ("left","kicked") if event else True
    title=str(info.get("title") or "Grupo sin nombre")[:160]
    await service.repo.pool.execute(
        """INSERT INTO descarga_bulk_chats(bot_id,chat_id,title,chat_type,active,seen_at)
           VALUES($1,$2,$3,$4,$5,$6)
           ON CONFLICT(bot_id,chat_id) DO UPDATE SET
             title=EXCLUDED.title,chat_type=EXCLUDED.chat_type,
             active=EXCLUDED.active,seen_at=EXCLUDED.seen_at""",
        service.bot_id,chat_id,title,info["type"],active,time.time())

async def _my_account(service,uid):
    row=await service.repo.account(uid)
    if not row or not row["session"]:
        raise ValueError("Conecta primero tu cuenta de Telegram.")
    return row

async def _client_for(service,uid):
    session=await service.get_session(uid)
    if not session:raise ValueError("Conecta primero tu cuenta de Telegram.")
    client=service.client(session)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ValueError("La cuenta vinculada perdió su sesión.")
    return client

async def origins(service,uid):
    _int(uid);await _my_account(service,uid)
    async with service.repo.account_lock(uid,wait=False):
        client=await _client_for(service,uid)
        try:
            me=await client.get_me()
            if not me or int(me.id)!=uid:raise ValueError("La cuenta vinculada no corresponde a este usuario.")
            blocked=protected_sources()
            result=[]
            async for dialog in client.iter_dialogs(limit=MAX_DIALOGS):
                ent=dialog.entity
                if not isinstance(ent,(types.Channel,types.Chat)):continue
                ident=int(dialog.id)
                if ident>=0 or ident in blocked:continue
                result.append({"id":ident,"title":str(dialog.name or "Sin nombre")[:160],
                               "kind":"canal" if getattr(ent,"broadcast",False) else "grupo"})
            return {"groups":sorted(result,key=lambda x:x["title"].casefold()),
                    "limited":len(result)>=MAX_DIALOGS}
        finally:await client.disconnect()

async def check_destination(service,uid,dest):
    _chat(dest)
    bot=await service.api("getChatMember",{"chat_id":dest,"user_id":service.bot_id})
    if not _role(bot):
        raise ValueError("Agrega el bot como administrador del grupo destino.")
    person=await service.api("getChatMember",{"chat_id":dest,"user_id":uid})
    if not _role(person):
        raise ValueError("Debes ser administrador del grupo destino.")
    return True

async def destinations(service,uid):
    _int(uid);await _my_account(service,uid)
    rows=await service.repo.pool.fetch(
        """SELECT chat_id,title,chat_type FROM descarga_bulk_chats
           WHERE bot_id=$1 AND active=TRUE ORDER BY title LIMIT $2""",
        service.bot_id,MAX_DESTINATIONS)
    groups=[]
    for row in rows:
        try:
            await check_destination(service,uid,int(row["chat_id"]))
        except Exception:
            continue
        groups.append({"id":int(row["chat_id"]),"title":row["title"],
                       "kind":"canal" if row["chat_type"]=="channel" else "grupo"})
    return {"groups":groups}

async def _source_dialog(service,uid,source):
    """Fetch source entity using only the user's own linked session."""
    async with service.repo.account_lock(uid,wait=False):
        client=await _client_for(service,uid)
        try:
            me=await client.get_me()
            if not me or int(me.id)!=uid:
                raise ValueError("La cuenta vinculada no corresponde a este usuario.")
            async for dialog in client.iter_dialogs(limit=MAX_DIALOGS):
                if int(dialog.id)==source:
                    ent=dialog.entity
                    if not isinstance(ent,(types.Channel,types.Chat)):
                        break
                    if getattr(ent,"noforwards",False):
                        raise ValueError("Este grupo no permite redistribuir contenido.")
                    return {"title":str(dialog.name or "Grupo")[:160],
                            "hash":int(getattr(ent,"access_hash",0) or 0),
                            "type":"channel" if isinstance(ent,types.Channel) else "chat"}
        finally:await client.disconnect()
    raise ValueError("El grupo origen no pertenece a tu cuenta vinculada.")

async def create(service,uid,data):
    _int(uid)
    source=_chat(data.get("source"))
    dest=_chat(data.get("destination"))
    if source==dest:raise ValueError("Selecciona grupos diferentes.")
    await ensure_allowed(service,uid,source)
    rules=await service.repo.policy(service.bot_id,uid,source)
    if rules["locked"]:
        raise ValueError("No puedes transferir contenido de ese origen.")
    account=await _my_account(service,uid)
    if not service.almacen:
        # Never start an unsupervised bulk transfer.
        raise ValueError("Esta función no está disponible temporalmente.")
    try:
        keeper=await service.api("getChatMember",{
            "chat_id":service.almacen,"user_id":service.bot_id})
        if not _role(keeper):
            raise ValueError("Esta función no está disponible temporalmente.")
    except Exception:
        raise ValueError("Esta función no está disponible temporalmente.") from None
    await check_destination(service,uid,dest)
    source_data=await _source_dialog(service,uid,source)
    target=await service.repo.pool.fetchrow(
        """SELECT title FROM descarga_bulk_chats
           WHERE bot_id=$1 AND chat_id=$2 AND active=TRUE""",
        service.bot_id,dest)
    if not target:raise ValueError("El bot todavía no ha registrado ese destino. Envía un mensaje allí.")
    prior=await service.repo.pool.fetchval(
        """SELECT 1 FROM descarga_bulk_jobs
           WHERE bot_id=$1 AND uid=$2 AND status IN ('scanning','ready','publishing','paused','review')
           LIMIT 1""",service.bot_id,uid)
    if prior:raise ValueError("Desconecta primero tu transferencia anterior.")
    job_id=uuid.uuid4();now=time.time()
    await service.repo.pool.execute(
        """INSERT INTO descarga_bulk_jobs
           (id,bot_id,uid,generation,source_id,source_hash,source_title,
            target_id,target_title,created,updated)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$10)""",
        job_id,service.bot_id,uid,int(account["generation"]),source,
        source_data["hash"],source_data["title"],dest,str(target["title"]),now)
    return {"id":str(job_id),"status":"scanning"}

async def _owner(service,uid,ident):
    try:job_id=uuid.UUID(str(ident))
    except (ValueError,AttributeError):raise ValueError("Transferencia no válida.") from None
    row=await service.repo.pool.fetchrow(
        "SELECT * FROM descarga_bulk_jobs WHERE id=$1 AND bot_id=$2 AND uid=$3",
        job_id,service.bot_id,uid)
    if not row or row["status"]=="detached":
        raise ValueError("Transferencia no disponible.")
    return row

async def current(service,uid):
    _int(uid)
    row=await service.repo.pool.fetchrow(
        """SELECT * FROM descarga_bulk_jobs
           WHERE bot_id=$1 AND uid=$2 AND status<>'detached'
           ORDER BY created DESC LIMIT 1""",service.bot_id,uid)
    if not row:return {"job":None}
    counts=_counts(row["counts"])
    ranges={}
    if int(row["run_no"])>0:
        run=await service.repo.pool.fetchrow(
            "SELECT ranges FROM descarga_bulk_runs WHERE job_id=$1 AND run_no=$2",
            row["id"],row["run_no"])
        if run:
            value=run["ranges"]
            ranges=json.loads(value) if isinstance(value,str) else dict(value)
    return {"job":{"id":str(row["id"]),"source":row["source_title"],"destination":row["target_title"],
                   "status":row["status"],"counts":counts,"scan_count":sum(counts.values()),
                   "run_no":int(row["run_no"]),"selected":int(row["run_total"]),
                   "published":int(row["run_done"]),"ranges":ranges,
                   "error":str(row["error"] or "")[:180]}}

def valid_ranges(counts,raw):
    if not isinstance(raw,dict) or set(raw)!=set(KINDS):
        raise ValueError("Selecciona los rangos de todas las categorías.")
    result={}
    for kind in KINDS:
        item=raw[kind]
        if not isinstance(item,list) or len(item)!=2 or any(type(v)!=int for v in item):
            raise ValueError("Rango de archivos no válido.")
        start,end=item;limit=counts[kind]
        if not 0<=start<=end<=limit:
            raise ValueError("Rango fuera de los archivos disponibles.")
        result[kind]=[start,end]
    if not sum(end-start for start,end in result.values()):
        raise ValueError("Selecciona por lo menos un archivo.")
    return result

async def begin(service,uid,data):
    row=await _owner(service,uid,data.get("id"))
    if row["status"] not in ("ready","paused","finished","error"):
        raise ValueError("Espera el escaneo o pausa la transferencia actual.")
    if row["lease"] and float(row["lease_until"])>time.time():
        raise ValueError("El envío anterior todavía está terminando.")
    if float(row["pause_until"])>time.time():
        raise ValueError("Telegram exige esperar antes de continuar.")
    await ensure_allowed(service,uid,int(row["source_id"]))
    await check_destination(service,uid,int(row["target_id"]))
    if not await service.repo.pool.fetchval(
        """SELECT 1 FROM descarga_bulk_chats
           WHERE bot_id=$1 AND chat_id=$2 AND active=TRUE""",
        service.bot_id,int(row["target_id"])):
        raise ValueError("El bot ya no está conectado al destino.")
    unresolved=await service.repo.pool.fetchval(
        """SELECT 1 FROM descarga_bulk_deliveries
           WHERE job_id=$1 AND run_no=$2
             AND status IN ('preparing','archived','review') LIMIT 1""",
        row["id"],row["run_no"])
    if unresolved:
        raise ValueError("Hay archivos pendientes de revisión de una entrega anterior.")
    counts=_counts(row["counts"]);ranges=valid_ranges(counts,data.get("ranges"))
    selected=sum(end-start for start,end in ranges.values())
    # Advisory transaction serializes two concurrent taps. No old run is deleted.
    async with service.repo.pool.acquire() as conn,conn.transaction():
        locked=await conn.fetchrow(
            "SELECT status,run_no,lease,lease_until FROM descarga_bulk_jobs WHERE id=$1 FOR UPDATE",
            row["id"])
        if locked["status"] not in ("ready","paused","finished","error") or (
            locked["lease"] and float(locked["lease_until"])>time.time()):
            raise ValueError("La transferencia cambió. Actualiza el panel.")
        run_no=int(locked["run_no"])+1
        await conn.execute(
            """INSERT INTO descarga_bulk_runs(job_id,run_no,ranges,selected,started)
               VALUES($1,$2,$3::jsonb,$4,$5)""",
            row["id"],run_no,json.dumps(ranges),selected,time.time())
        await conn.execute(
            """UPDATE descarga_bulk_jobs SET status='publishing',run_no=$2,
               run_total=$3,run_done=0,error='',lease=NULL,lease_until=0,updated=$4
               WHERE id=$1""",row["id"],run_no,selected,time.time())
    return {"ok":True,"run_no":run_no,"selected":selected}


async def pause(service,uid,data):
    row=await _owner(service,uid,data.get("id"))
    await service.repo.pool.execute(
        """UPDATE descarga_bulk_jobs SET
             status=CASE WHEN status IN ('scanning','waiting_scan')
                         THEN 'paused_scan' ELSE 'paused' END,
             pause_until=0,updated=$2
           WHERE id=$1 AND status IN ('scanning','waiting_scan',
                                     'publishing','waiting_send')""",
        row["id"],time.time())
    return {"ok":True}


async def resume(service,uid,data):
    row=await _owner(service,uid,data.get("id"))
    if row["status"] not in ("paused","paused_scan"):
        raise ValueError("Esta transferencia no está pausada.")
    if row["lease"] and float(row["lease_until"])>time.time():
        raise ValueError("La transferencia anterior todavía está terminando.")
    if float(row["pause_until"])>time.time():
        raise ValueError("Telegram exige esperar antes de reanudar.")
    await ensure_allowed(service,uid,int(row["source_id"]))
    if row["status"]=="paused":
        await check_destination(service,uid,int(row["target_id"]))
    new="scanning" if row["status"]=="paused_scan" else "publishing"
    updated=await service.repo.pool.fetchval(
        """UPDATE descarga_bulk_jobs SET status=$3,error='',updated=$4
           WHERE id=$1 AND bot_id=$2 AND status=$5
             AND (lease IS NULL OR lease_until<=$4)
           RETURNING 1""",
        row["id"],service.bot_id,new,time.time(),row["status"])
    if not updated:raise ValueError("Actualiza el progreso antes de reanudar.")
    return {"ok":True,"status":new}


async def disconnect(service,uid,data):
    row=await _owner(service,uid,data.get("id"))
    await service.repo.pool.execute(
        """UPDATE descarga_bulk_jobs SET status='detached',updated=$2
           WHERE id=$1 AND uid=$3 AND bot_id=$4""",
        row["id"],time.time(),uid,service.bot_id)
    return {"ok":True}

async def admin(service,uid,data):
    if not police(uid):raise PermissionError("Administración no autorizada.")
    op=str(data.get("operation") or "")
    if op=="status":
        rows=await service.repo.pool.fetch(
            """SELECT j.id,j.uid,j.source_title,j.target_title,
               j.status,j.run_done,j.run_total,j.error,j.created,
               (SELECT count(*) FROM descarga_bulk_audit a WHERE a.job_id=j.id) AS audited
               FROM descarga_bulk_jobs j WHERE j.bot_id=$1
               ORDER BY j.created DESC LIMIT 80""",service.bot_id)
        return {"jobs":[{"id":str(r["id"]),"uid":int(r["uid"]),
                         "source":r["source_title"],"destination":r["target_title"],
                         "status":r["status"],"published":int(r["run_done"]),
                         "total":int(r["run_total"]),"audited":int(r["audited"]),
                         "error":str(r["error"] or "")[:180]} for r in rows]}
    if op=="details":
        ident=str(data.get("id") or "")
        try:job_id=uuid.UUID(ident)
        except ValueError:raise ValueError("Trabajo no válido.") from None
        row=await service.repo.pool.fetchrow(
            "SELECT id,uid,source_title,target_title FROM descarga_bulk_jobs WHERE bot_id=$1 AND id=$2",
            service.bot_id,job_id)
        if not row:raise ValueError("Trabajo no encontrado.")
        records=await service.repo.pool.fetch(
            """SELECT run_no,message_id,source_id,target_id,archive_id,
                      destination_id,original_author,caption,original_reply
               FROM descarga_bulk_audit WHERE job_id=$1
               ORDER BY created DESC LIMIT 70""",job_id)
        return {"uid":int(row["uid"]),"source":row["source_title"],
                "destination":row["target_title"],
                "archive_chat_id":int(service.almacen or 0),
                "audit":[{"run":int(r["run_no"]),"original_id":int(r["message_id"]),
                          "archive_id":int(r["archive_id"]),
                          "destination_id":int(r["destination_id"] or 0),
                          "author":int(r["original_author"] or 0),
                          "reply_to":int(r["original_reply"]),
                          "caption":str(r["caption"] or "")[:320]} for r in records]}
    if op in ("stop","ban"):
        target=_int(data.get("uid"))
        if op=="ban":
            await service.repo.set_blocks(service.bot_id,[target],True,uid)
        await service.repo.pool.execute(
            """UPDATE descarga_bulk_jobs SET status='detached',updated=$3
               WHERE bot_id=$1 AND uid=$2 AND status<>'detached'""",
            service.bot_id,target,time.time())
        return {"ok":True}
    raise ValueError("Operación administrativa no válida.")

async def dispatch(service,uid,operation,data):
    if not hasattr(service,"repo") or not service.repo.pool or not service.bot_id:
        raise ValueError("Esta función necesita DESCARGAS principal.")
    if operation=="bulk-origins":return await origins(service,uid)
    if operation=="bulk-destinations":return await destinations(service,uid)
    if operation=="bulk-create":return await create(service,uid,data)
    if operation=="bulk-status":return await current(service,uid)
    if operation=="bulk-begin":return await begin(service,uid,data)
    if operation=="bulk-pause":return await pause(service,uid,data)
    if operation=="bulk-resume":return await resume(service,uid,data)
    if operation=="bulk-disconnect":return await disconnect(service,uid,data)
    if operation=="bulk-admin":return await admin(service,uid,data)
    raise ValueError("Operación no disponible.")
