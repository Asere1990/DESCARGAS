"""Web Galerías broadcast notices: durable, admin-only, low-priority fanout.

A notice is available to all users registered at its publication time, even
if their Telegram DM is blocked. Telegram delivery is best effort and rate-
limited; it never participates in gallery loading or R2 backup scheduling.
"""
from __future__ import annotations
import asyncio
import html
import json
import logging
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web
from .core import identifier
from .downloads import temporary_link, validate_link
from .permissions import is_admin

log = logging.getLogger("gallery-notices")
SCHEMA = """
CREATE TABLE IF NOT EXISTS wg_notices(
 id BIGSERIAL PRIMARY KEY,
 actor BIGINT NOT NULL,
 message TEXT NOT NULL DEFAULT '',
 media_key TEXT,
 media_kind TEXT,
 mime TEXT,
 filename TEXT,
 media_size BIGINT NOT NULL DEFAULT 0,
 created DOUBLE PRECISION NOT NULL,
 audience INTEGER NOT NULL DEFAULT 0,
 published BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS wg_notices_newest ON wg_notices(id DESC) WHERE published;
CREATE TABLE IF NOT EXISTS wg_notice_uploads(
 token TEXT PRIMARY KEY,
 actor BIGINT NOT NULL,
 media_key TEXT NOT NULL UNIQUE,
 media_kind TEXT NOT NULL,
 mime TEXT NOT NULL,
 filename TEXT NOT NULL,
 size BIGINT NOT NULL,
 created DOUBLE PRECISION NOT NULL,
 used BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS wg_notice_reads(
 notice BIGINT NOT NULL REFERENCES wg_notices(id) ON DELETE CASCADE,
 uid BIGINT NOT NULL,
 read_at DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(uid,notice)
);
CREATE INDEX IF NOT EXISTS wg_notice_reads_notice ON wg_notice_reads(notice);
CREATE TABLE IF NOT EXISTS wg_notice_deliveries(
 notice BIGINT NOT NULL REFERENCES wg_notices(id) ON DELETE CASCADE,
 uid BIGINT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
 delivered_at DOUBLE PRECISION,
 last_error TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(notice,uid)
);
CREATE INDEX IF NOT EXISTS wg_notice_deliveries_due
 ON wg_notice_deliveries(next_attempt,notice,uid)
 WHERE status IN ('pending','sending','retry');
-- Public schema: no browser or anon DB access, all routes enforce signed UID.
ALTER TABLE wg_notices ENABLE ROW LEVEL SECURITY;
ALTER TABLE wg_notice_uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wg_notice_reads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wg_notice_deliveries ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON wg_notices,wg_notice_uploads,wg_notice_reads,wg_notice_deliveries
 FROM PUBLIC;
"""
KINDS = {
 "photo": {"image/jpeg": ".jpg", "image/png": ".png",
           "image/webp": ".webp"},
 "gif": {"image/gif": ".gif"},
 "video": {"video/mp4": ".mp4", "video/quicktime": ".mov"},
}
UPLOAD_LIMIT = 60 * 1024 * 1024
TELEGRAM_MEDIA_LIMIT = 18 * 1024 * 1024
URL_REGEX = re.compile(r"\[([^\]\r\n]{1,120})\]\((https://[^\s)]{1,1200})\)|(https://[^\s<>]{1,1200})")
VALID_TOKEN = re.compile(r"[A-Za-z0-9_-]{24,90}")


def safe_url(url):
    if not isinstance(url, str) or len(url) > 1200:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    if len(parsed.hostname) > 253 or any(ord(c)<32 for c in url):
        return None
    return url


def format_html(message):
    """Safe clickable phrases [title](https://...) and visible HTTPS URLs."""
    message = str(message or "")
    result = []
    end = 0
    for match in URL_REGEX.finditer(message):
        result.append(html.escape(message[end:match.start()]))
        link = match.group(2) or match.group(3)
        clean = safe_url(link)
        if clean:
            label = match.group(1) or link
            result.append('<a href="' + html.escape(clean, quote=True)
                          + '">' + html.escape(label) + '</a>')
        else:
            result.append(html.escape(match.group(0)))
        end = match.end()
    result.append(html.escape(message[end:]))
    return "".join(result)


def audience_where():
    return """FROM wg_users u LEFT JOIN wg_access_blocks blk ON blk.uid=u.uid
              WHERE NOT COALESCE(blk.blocked,FALSE)"""


async def _registered(g, uid, at=None):
    if at is None:
        return bool(await g.db().fetchval(
            "SELECT 1 FROM wg_users WHERE uid=$1", uid))
    return bool(await g.db().fetchval("""
       SELECT 1 FROM wg_users u
        LEFT JOIN wg_access_blocks blk ON blk.uid=u.uid
       WHERE u.uid=$1 AND u.first_seen<=$2
         AND NOT COALESCE(blk.blocked,FALSE)
       """, uid, at))


async def admin_operation(g, uid, op, data):
    if not is_admin(uid):
        raise web.HTTPForbidden(text="Solo administradores.")
    db = g.db()
    if op == "admin-notices-audience":
        count = await db.fetchval("SELECT count(*) " + audience_where())
        return {"users": int(count or 0), "telegram_note":
                "Los avisos se publican para todos. Telegram solo puede entregar "
                "DM a personas que hayan iniciado conversación con el bot."}
    if op == "admin-notices-list":
        rows = await db.fetch("""
           SELECT n.id,n.message,n.media_kind,n.created,n.audience,
            (SELECT COUNT(*)::int FROM wg_notice_deliveries d
              WHERE d.notice=n.id AND d.status='delivered') AS delivered,
            (SELECT COUNT(*)::int FROM wg_notice_deliveries d
              WHERE d.notice=n.id AND d.status='unreachable') AS unreachable,
            (SELECT COUNT(*)::int FROM wg_notice_deliveries d
              WHERE d.notice=n.id AND d.status IN
                ('pending','retry','sending')) AS pending,
            (SELECT COUNT(*)::int FROM wg_notice_reads r
              WHERE r.notice=n.id) AS read_in_web
           FROM wg_notices n WHERE n.published
           ORDER BY n.id DESC LIMIT 15
        """)
        return {"notices": [dict(n) for n in rows]}
    if op == "admin-notices-publish":
        content = data.get("message", "")
        token = data.get("upload_token")
        if (not isinstance(content,str) or len(content)>3000 or
                "\x00" in content or data.get("confirm")!="ENVIAR_A_TODOS"):
            raise web.HTTPBadRequest(text="Revisa el texto y confirma el envío.")
        content = content.strip()
        if token is not None and (not isinstance(token,str) or not VALID_TOKEN.fullmatch(token)):
            raise web.HTTPBadRequest(text="Adjunto inválido.")
        if not content and not token:
            raise web.HTTPBadRequest(text="Escribe un mensaje o adjunta multimedia.")
        for match in URL_REGEX.finditer(content):
            link = match.group(2) or match.group(3)
            if not safe_url(link):
                raise web.HTTPBadRequest(text="Enlace HTTPS inválido.")
        now = time.time()
        async with db.acquire() as conn,conn.transaction():
            upload = None
            if token:
                upload = await conn.fetchrow("""
                  SELECT token,media_key,media_kind,mime,filename,size
                  FROM wg_notice_uploads
                  WHERE token=$1 AND actor=$2 AND used=FALSE
                    AND created>$3 FOR UPDATE""", token,uid,now-86400)
                if not upload:
                    raise web.HTTPBadRequest(text="Adjunto no encontrado o utilizado.")
            if upload and len(content)>800:
                raise web.HTTPBadRequest(
                    text="Con multimedia el texto admite hasta 800 caracteres.")
            audience = await conn.fetchval(
                "SELECT count(*) " + audience_where())
            if not audience:
                raise web.HTTPBadRequest(text="No hay usuarios registrados para recibir el aviso.")
            row = await conn.fetchrow("""
                INSERT INTO wg_notices
                  (actor,message,media_key,media_kind,mime,filename,media_size,created,audience)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id
            """,uid,content,
                upload["media_key"] if upload else None,
                upload["media_kind"] if upload else None,
                upload["mime"] if upload else None,
                upload["filename"] if upload else None,
                upload["size"] if upload else 0,now,int(audience))
            notice_id = int(row["id"])
            await conn.execute("""
                INSERT INTO wg_notice_deliveries(notice,uid,next_attempt)
                SELECT $1,u.uid,$2
                FROM wg_users u LEFT JOIN wg_access_blocks blk ON blk.uid=u.uid
                WHERE NOT COALESCE(blk.blocked,FALSE)
                ON CONFLICT DO NOTHING
            """,notice_id,now)
            if upload:
                await conn.execute(
                    "UPDATE wg_notice_uploads SET used=TRUE WHERE token=$1",
                    token)
        await g.log(uid, "admin-notice-published")
        return {"ok":True,"id":notice_id,"audience":int(audience)}
    raise web.HTTPNotFound()


async def user_operation(g, uid, op, data):
    if not await _registered(g,uid):
        raise web.HTTPForbidden(text="Usuario no registrado.")
    db=g.db()
    if op=="notices-count":
        unread=await db.fetchval("""
            SELECT COUNT(*)::int
            FROM wg_notices n
            JOIN wg_users u ON u.uid=$1 AND u.first_seen<=n.created
            LEFT JOIN wg_notice_reads r ON r.notice=n.id AND r.uid=$1
            WHERE n.published AND r.notice IS NULL
        """,uid)
        return {"unread":int(unread or 0)}
    if op=="notices-list":
        cursor=identifier(data.get("cursor",0),0)
        rows=await db.fetch("""
            SELECT n.id,n.message,n.media_kind,n.mime,n.filename,n.created,
                   (r.notice IS NOT NULL) AS read
            FROM wg_notices n
            JOIN wg_users u ON u.uid=$1 AND u.first_seen<=n.created
            LEFT JOIN wg_notice_reads r ON r.notice=n.id AND r.uid=$1
            WHERE n.published AND n.id<$2
            ORDER BY n.id DESC LIMIT 21
        """,uid,cursor or 2**63-1)
        notices=[]
        for row in rows[:20]:
            item=dict(row)
            if item["media_kind"]:
                item["media"]=(f"/web-galerias/notice-media/{item['id']}?t="
                               +temporary_link(g,uid,item["id"],"notice-media"))
            else:
                item["media"]=None
            notices.append(item)
        return {"notices":notices,
                "next":int(rows[19]["id"]) if len(rows)>20 else None}
    if op=="notices-mark-read":
        ids=data.get("ids")
        if not isinstance(ids,list) or not 1<=len(ids)<=50:
            raise web.HTTPBadRequest(text="Selecciona avisos para marcar como leídos.")
        ids=list(dict.fromkeys(identifier(v) for v in ids))
        await db.execute("""
            INSERT INTO wg_notice_reads(notice,uid,read_at)
            SELECT n.id,$1,$2 FROM wg_notices n
            JOIN wg_users u ON u.uid=$1 AND u.first_seen<=n.created
            WHERE n.published AND n.id=ANY($3::bigint[])
            ON CONFLICT(uid,notice) DO NOTHING
        """,uid,time.time(),ids)
        return {"ok":True}
    raise web.HTTPNotFound()


async def upload(g,request):
    """Stream a single admin attachment to R2; do not buffer large video in RAM."""
    uid=await g.auth(request)
    if not is_admin(uid):
        raise web.HTTPForbidden(text="Solo administradores.")
    kind=request.query.get("kind")
    mime=request.content_type
    if kind not in KINDS or mime not in KINDS[kind]:
        raise web.HTTPBadRequest(text="Admite JPG, PNG, WebP, GIF o MP4/MOV.")
    if not g.storage.ready:
        raise web.HTTPServiceUnavailable(text="Almacenamiento no disponible.")
    suffix=KINDS[kind][mime]
    max_size=(25*1024*1024 if kind in ("photo","gif") else UPLOAD_LIMIT)
    if request.content_length and request.content_length>max_size:
        raise web.HTTPRequestEntityTooLarge(max_size=max_size,
             actual_size=request.content_length)
    filename=(request.query.get("filename") or ("archivo"+suffix))[:110]
    filename=re.sub(r"[^A-Za-z0-9._-]","_",filename).strip("._") or "archivo"+suffix
    count=0
    fd=tempfile.NamedTemporaryFile(mode="wb",suffix=suffix,delete=False)
    path=Path(fd.name)
    try:
        with fd:
            async for chunk in request.content.iter_chunked(256*1024):
                count+=len(chunk)
                if count>max_size:
                    raise web.HTTPRequestEntityTooLarge(max_size=max_size,actual_size=count)
                fd.write(chunk)
        if count<16:
            raise web.HTTPBadRequest(text="Archivo vacío.")
        with open(path,"rb") as stream:
            header=stream.read(32)
        magic=(mime=="image/jpeg" and header.startswith(b"\xff\xd8\xff") or
               mime=="image/png" and header.startswith(b"\x89PNG\r\n\x1a\n") or
               mime=="image/webp" and header.startswith(b"RIFF") and
                   header[8:12]==b"WEBP" or
               mime=="image/gif" and header.startswith((b"GIF87a",b"GIF89a")) or
               mime in ("video/mp4","video/quicktime") and b"ftyp" in header[:16])
        if not magic:
            raise web.HTTPBadRequest(text="El contenido no coincide con el formato.")
        if kind in ("photo","gif"):
            def verify_image():
                from PIL import Image
                with Image.open(path) as img:
                    if img.width*img.height>32_000_000 or img.width<1 or img.height<1:
                        raise ValueError("Tamaño de imagen no permitido")
                    if kind=="gif" and img.n_frames>350:
                        raise ValueError("GIF demasiado largo")
                    img.verify()
            try: await asyncio.to_thread(verify_image)
            except (OSError,ValueError):
                raise web.HTTPBadRequest(text="Imagen o GIF no válido.") from None
        token=secrets.token_urlsafe(30)
        key=f"notices/{secrets.token_hex(16)}{suffix}"
        await g.storage.upload(path,key,mime)
        await g.db().execute("""
            INSERT INTO wg_notice_uploads(token,actor,media_key,media_kind,
                mime,filename,size,created)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8)
        """,token,uid,key,kind,mime,filename,count,time.time())
        return {"upload_token":token,"kind":kind,"size":count,"name":filename}
    finally:
        path.unlink(missing_ok=True)


async def media(g,request):
    p=validate_link(g,request.query.get("t"),"notice-media")
    uid=identifier(p["uid"])
    notice_id=identifier(request.match_info["notice"])
    if p["item"]!=notice_id:
        raise web.HTTPForbidden()
    row=await g.db().fetchrow("""
       SELECT n.media_key,n.media_kind,n.mime,n.created
       FROM wg_notices n WHERE n.id=$1 AND n.published
    """,notice_id)
    if not row or not row["media_key"] or not await _registered(g,uid,row["created"]):
        raise web.HTTPForbidden(text="Contenido no autorizado.")
    url=g.storage.signed_get(row["media_key"],duration=240)
    raise web.HTTPFound(url,headers={"Cache-Control":"private, no-store",
                                    "Referrer-Policy":"no-referrer"})


async def _claim(db):
    now=time.time()
    return await db.fetchrow("""
       WITH available AS (
         SELECT d.notice,d.uid
           FROM wg_notice_deliveries d
           JOIN wg_notices n ON n.id=d.notice AND n.published
          WHERE d.status IN ('pending','retry','sending')
            AND d.next_attempt<$1
          ORDER BY d.notice,d.uid LIMIT 1
          FOR UPDATE OF d SKIP LOCKED
       )
       UPDATE wg_notice_deliveries d
       SET status='sending',attempts=d.attempts+1,
           next_attempt=$1+120
       FROM available a
       WHERE d.notice=a.notice AND d.uid=a.uid
       RETURNING d.notice,d.uid,d.attempts
    """,now)


async def _finish(db,notice,uid,status,error="",retry_after=0):
    await db.execute("""
      UPDATE wg_notice_deliveries
      SET status=$3,next_attempt=$4,
          delivered_at=CASE WHEN $3='delivered' THEN $5 ELSE delivered_at END,
          last_error=$6
      WHERE notice=$1 AND uid=$2
    """,notice,uid,status,
       time.time()+retry_after if retry_after else 0,time.time(),str(error or "")[:180])


async def _telegram_one(session,g,delivery,token):
    """Send one, never read Telegram's updates or change another bot's webhook."""
    db=g.db()
    notice=await db.fetchrow("""
      SELECT id,message,media_key,media_kind,mime,media_size
      FROM wg_notices WHERE id=$1 AND published
    """,delivery["notice"])
    if not notice:
        return "failed","Aviso retirado",0
    body=format_html(notice["message"])
    method="sendMessage"
    payload={"chat_id":str(delivery["uid"]),
             "text":body or "📣 Nuevo aviso en Web Galerías",
             "parse_mode":"HTML","disable_web_page_preview":True}
    if notice["media_key"]:
        if int(notice["media_size"] or 0)<=TELEGRAM_MEDIA_LIMIT:
            if notice["media_kind"]=="photo":method="sendPhoto";label="photo"
            elif notice["media_kind"]=="gif":method="sendAnimation";label="animation"
            elif notice["media_kind"]=="video" and notice["mime"]=="video/mp4":
                method="sendVideo";label="video"
            else:label=None
            if label:
                payload.pop("text",None)
                payload.pop("disable_web_page_preview",None)
                payload[label]=g.storage.signed_get(notice["media_key"],duration=300)
                payload["caption"]=body[:950] or None
        else:
            payload["text"] += "\n\n📎 Archivo multimedia disponible dentro de Web Galerías."
    # Protect existing main user-view traffic: low-rate async Telegram requests.
    async with session.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=payload,timeout=ClientTimeout(total=28)) as response:
        try: result=await response.json(content_type=None)
        except Exception: result={"ok":False,"description":"Respuesta inválida"}
    if result.get("ok"):
        return "delivered","",0
    code=int(result.get("error_code") or response.status)
    reason=str(result.get("description") or "Error Telegram")[:180]
    if code==403 or any(x in reason.lower() for x in (
        "bot was blocked","chat not found","user is deactivated",
        "bot can't initiate conversation")):
        return "unreachable",reason,0
    if code==429:
        seconds=(result.get("parameters") or {}).get("retry_after",30)
        return "retry",reason,min(600,max(5,int(seconds)))
    if code>=500:
        return "retry",reason,30
    return "failed",reason,0


async def run(g):
    """Never delay page rendering; invoices, scans, and R2 sync run independently."""
    await asyncio.sleep(8)
    async with ClientSession() as session:
        while not g.closed:
            try:
                # Sending requires an actual running bot token. A recipient
                # who blocked it still gets the notice in their web inbox.
                token=os.getenv("GALERIAS_BROADCAST_BOT_TOKEN","").strip() or g.s.token
                if not token or not g.storage.ready:
                    await asyncio.sleep(15)
                    continue
                db=g.db()
                job=await _claim(db)
                if not job:
                    await asyncio.sleep(4)
                    continue
                try:
                    status,error,delay=await _telegram_one(session,g,job,token)
                except asyncio.CancelledError:raise
                except Exception as exc:
                    status,error,delay="retry",type(exc).__name__,30
                if status=="retry" and job["attempts"]>=5:
                    status,delay="failed",0
                await _finish(db,job["notice"],job["uid"],status,error,delay)
                await asyncio.sleep(0.25)  # <4 Bot API sends per second.
            except asyncio.CancelledError:raise
            except Exception as exc:
                log.warning("Notice Telegram fanout pending (%s)",type(exc).__name__)
                await asyncio.sleep(8)
