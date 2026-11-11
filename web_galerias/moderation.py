"""Isolated, audited admin moderation of CUBANITAS media and private user directory.

The Telegram source remains untouched. "removed" is a durable tombstone:
the ordinary sync worker never re-publishes it even after a source rescan.
R2 deletion is optional and handled by a separate low-priority retry queue.
"""
from __future__ import annotations
import asyncio
import logging
import os
import time

from aiohttp import web

from .permissions import is_admin, is_tester, payment_test_user, file_ids, ROOT, ACCESS

log = logging.getLogger("cubanitas-moderation")
SCHEMA = """
CREATE TABLE IF NOT EXISTS wg_media_removals(
 media BIGINT PRIMARY KEY REFERENCES wg_media(id),
 folder BIGINT NOT NULL, message BIGINT NOT NULL,
 admin_uid BIGINT NOT NULL, removed_at DOUBLE PRECISION NOT NULL,
 remove_r2 BOOLEAN NOT NULL DEFAULT FALSE);
CREATE INDEX IF NOT EXISTS wg_media_removals_folder
 ON wg_media_removals(folder,removed_at DESC);
CREATE TABLE IF NOT EXISTS wg_r2_deletion_queue(
 key TEXT PRIMARY KEY, media BIGINT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt DOUBLE PRECISION NOT NULL,
 completed_at DOUBLE PRECISION NOT NULL DEFAULT 0,
 last_error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS wg_r2_delete_due ON
 wg_r2_deletion_queue(next_attempt) WHERE completed_at=0;
"""


async def operation(g, uid, op, data):
    """Never called for customer operations; require existing admins.txt on every call."""
    from .core import identifier
    from .downloads import temporary_link
    if not is_admin(uid):
        raise web.HTTPForbidden(text="Esta función es exclusiva del administrador.")
    db = g.db()

    if op == "admin-media-list":
        folder = identifier(data.get("folder"))
        cursor = identifier(data.get("cursor", 0), 0)
        exists = await db.fetchval("SELECT 1 FROM wg_folders WHERE id=$1", folder)
        if not exists:
            raise web.HTTPNotFound(text="Galería no encontrada.")
        rows = await db.fetch("""
            SELECT m.id,m.folder,m.message,m.kind,m.mime,m.filename,m.date,
                   m.state,m.thumb_key,m.file_key
              FROM wg_media m WHERE m.folder=$1
                AND m.state IN ('preview','ready')
                AND m.id<$2 ORDER BY m.id DESC LIMIT 61""",
            folder, cursor or 2**63-1)
        result = []
        for row in rows[:60]:
            item = {k: row[k] for k in (
                "id", "folder", "message", "kind", "mime", "filename", "date")}
            item["ready"] = row["state"] == "ready"
            item["thumb"] = (
                "/web-galerias/media/"+str(item["id"])+"/thumb?t="+
                temporary_link(g,uid,item["id"],"thumb")
            ) if row["thumb_key"] else None
            item["view"] = (
                "/web-galerias/media/"+str(item["id"])+"/view?t="+
                temporary_link(g,uid,item["id"],"view")
            ) if row["state"]=="ready" and row["file_key"] else None
            result.append(item)
        return {"items": result, "next": rows[59]["id"] if len(rows)>60 else None}

    if op == "admin-media-remove":
        ids = data.get("ids")
        cloud = data.get("remove_cloudflare")
        if (not isinstance(ids,list) or not 1 <= len(ids) <= 200
            or type(cloud) is not bool
            or data.get("confirm") != "CONFIRMAR_ELIMINACION"):
            raise web.HTTPBadRequest(text="Confirma de 1 a 200 archivos y el destino del respaldo.")
        ids = list(dict.fromkeys(identifier(i) for i in ids))
        now = time.time()
        scheduled = 0
        async with db.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch("""
                    SELECT id,folder,message,thumb_key,file_key
                      FROM wg_media WHERE id=ANY($1::bigint[])
                        AND state IN ('preview','ready') FOR UPDATE""", ids)
                if len(rows) != len(ids):
                    raise web.HTTPConflict(text="El catálogo cambió. Actualiza la selección.")
                # All media serving, search, personal folders and old download tickets
                # already filter to preview/ready. This revokes ALL copies immediately.
                await conn.execute("""UPDATE wg_media SET state='removed',available=0,
                   error='admin_removed' WHERE id=ANY($1::bigint[])
                   AND state IN ('preview','ready')""",ids)
                for row in rows:
                    await conn.execute("""
                      INSERT INTO wg_media_removals
                        (media,folder,message,admin_uid,removed_at,remove_r2)
                      VALUES($1,$2,$3,$4,$5,$6)
                      ON CONFLICT(media) DO NOTHING""",
                      row["id"],row["folder"],row["message"],uid,now,cloud)
                    if cloud:
                        for key in {row["thumb_key"], row["file_key"]} - {None, ""}:
                            try:
                                safe = g.storage.safe_key(key)
                            except ValueError:
                                raise web.HTTPBadRequest(
                                    text="Una clave de respaldo no es válida; no se eliminó nada."
                                ) from None
                            await conn.execute("""
                                INSERT INTO wg_r2_deletion_queue(media,key,next_attempt)
                                VALUES($1,$2,$3)
                                ON CONFLICT(key) DO NOTHING
                            """,row["id"],safe,now+600)
                            scheduled += 1
        await g.log(uid,op+(":r2" if cloud else ":web"))
        return {"ok":True,"removed":len(ids),"cloudflare_queued":scheduled}

    if op == "admin-media-r2-status":
        row=await db.fetchrow("""
            SELECT COUNT(*) FILTER(WHERE completed_at=0)::int AS pending,
                   COUNT(*) FILTER(WHERE completed_at>0)::int AS completed
            FROM wg_r2_deletion_queue""")
        return dict(row or {})

    if op == "admin-active-users":
        cursor=identifier(data.get("after",0),0)
        # Existing testers/admins are active without paid Stripe subscription,
        # except payment-gated testers who must have paid first.
        exempt=sorted(uid for uid in
            (file_ids(ACCESS) | file_ids(ROOT/"admins.txt"))
            if not payment_test_user(uid))
        rows=await db.fetch("""
          SELECT u.uid,u.name,u.last_seen,
                 s.state,s.paid_until
          FROM wg_users u LEFT JOIN wg_subscriptions s ON s.uid=u.uid
          WHERE u.uid>$1 AND
              ((s.state IN ('active','trialing') AND s.paid_until>$2)
                OR u.uid=ANY($3::bigint[]))
          ORDER BY u.uid ASC LIMIT 201
        """,cursor,time.time(),exempt)
        return {
          "users":[{
            "telegram_id":int(row["uid"]),
            "name":str(row["name"] or "Usuario"),
            "last_seen":row["last_seen"],
            "status":"Activa",
          } for row in rows[:200]],
          "next":int(rows[199]["uid"]) if len(rows)>200 else None
        }

    if op == "admin-user-profile":
        target=identifier(data.get("telegram_id"))
        from .customer_bridge import ready,post_signed
        if not ready():
            raise web.HTTPServiceUnavailable(
                text="Consulta de CUSTOMER-SERVICE no configurada.")
        try:
            profile=await post_signed("profile",{
                "admin_uid":int(uid),"telegram_id":target,
            },timeout=7)
        except Exception as exc:
            log.warning("CUBANITAS administrative profile unavailable (%s)",
                        type(exc).__name__)
            raise web.HTTPServiceUnavailable(
                text="No se pudo consultar la ficha central ahora.") from None
        if not profile.get("found"):
            return {"found":False,"telegram_id":target}
        return profile

    raise web.HTTPNotFound()


async def run_deletions(g):
    """Low-priority, durable queue: never blocks the user-visible gallery.

    Repeat successful deletions after 1h and 24h in case an in-flight
    Telegram -> R2 upload finished after the admin removed the media.
    """
    while not g.closed:
        try:
            if g.ready and g.storage.ready:
                await deletion_cycle(g)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("CUBANITAS R2 cleanup pending (%s)",type(exc).__name__)
        await asyncio.sleep(30)


async def deletion_cycle(g):
    rows=await g.db().fetch("""
       SELECT key,media,attempts FROM wg_r2_deletion_queue
       WHERE completed_at=0 AND next_attempt<=$1
       ORDER BY next_attempt ASC LIMIT 2""",time.time())
    for row in rows:
        key=row["key"]
        try:
            await asyncio.to_thread(
                g.storage._client().delete_object,Bucket=g.storage.bucket,
                Key=g.storage.safe_key(key))
        except Exception as exc:
            wait=min(3600,120*(2**min(5,row["attempts"])))
            await g.db().execute("""
              UPDATE wg_r2_deletion_queue SET attempts=attempts+1,
                 last_error=$2,next_attempt=$3 WHERE key=$1""",
              key,type(exc).__name__,time.time()+wait)
            continue
        count=row["attempts"]+1
        if count>=3:
            await g.db().execute("""
              UPDATE wg_r2_deletion_queue SET attempts=$2,completed_at=$3,
              next_attempt=$3,last_error='' WHERE key=$1""",
              key,count,time.time())
        else:
            await g.db().execute("""
              UPDATE wg_r2_deletion_queue SET attempts=$2,
              next_attempt=$3,last_error='' WHERE key=$1""",
              key,count,time.time()+(3600 if count==1 else 86400))
        await asyncio.sleep(0.2)
