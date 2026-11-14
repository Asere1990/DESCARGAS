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
-- Short-lived heartbeat from visible customer gallery pages only.
CREATE TABLE IF NOT EXISTS wg_gallery_presence(
 uid BIGINT PRIMARY KEY, updated DOUBLE PRECISION NOT NULL,
 view TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS wg_gallery_presence_updated
 ON wg_gallery_presence(updated DESC);
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
        from .personal import media_filter
        from .core import normal
        kind=media_filter(data.get("kind","all"))
        query=data.get("query","")
        if not isinstance(query,str) or len(query)>100:
            raise web.HTTPBadRequest(text="Búsqueda incorrecta.")
        query=normal(query.strip())[:100]
        exists = await db.fetchval("SELECT 1 FROM wg_folders WHERE id=$1", folder)
        if not exists:
            raise web.HTTPNotFound(text="Galería no encontrada.")
        rows = await db.fetch("""
            SELECT m.id,m.folder,m.message,m.kind,m.mime,m.filename,m.date,
                   m.state,m.thumb_key,m.file_key
              FROM wg_media m WHERE
                (m.folder=$1 OR m.folder IN (
                  SELECT source_folder FROM wg_folder_sources WHERE parent_folder=$1))
                AND m.state IN ('preview','ready')
                AND ($3::text='all' OR m.kind=$3)
                AND ($4::text='' OR to_tsvector('simple',m.search_text)
                     @@ plainto_tsquery('simple',$4))
                AND m.id<$2 ORDER BY m.id DESC LIMIT 61""",
            folder, cursor or 2**63-1,kind,query)
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

    if op == "admin-user-stats":
        # Counts paid, valid Web memberships separately from live presence.
        # No Telegram connection, customer profile fetch, or history preload.
        now=time.time()
        row=await db.fetchrow("""
           SELECT
             (SELECT COUNT(*)::int FROM wg_subscriptions s
               LEFT JOIN wg_access_blocks b ON b.uid=s.uid
               WHERE s.state IN ('active','trialing') AND s.paid_until>$1
                 AND NOT COALESCE(b.blocked,FALSE)) AS paid,
             (SELECT COUNT(*)::int FROM wg_gallery_presence p
               LEFT JOIN wg_access_blocks b ON b.uid=p.uid
               WHERE p.updated>$2 AND p.view IN ('library','album','viewer')
                 AND NOT COALESCE(b.blocked,FALSE)) AS online
        """,now,now-75)
        return {"paid":int(row["paid"] or 0),"online":int(row["online"] or 0)}

    if op == "admin-free-access-status":
        target=identifier(data.get("telegram_id"))
        override=await db.fetchval("SELECT enabled FROM wg_free_access WHERE uid=$1",target)
        return {"telegram_id":target,
                "free_access":await g.free_granted(target),
                "override":override,"admin":is_admin(target)}

    if op == "admin-free-access-set":
        target=identifier(data.get("telegram_id"))
        enabled=data.get("enabled")
        if type(enabled) is not bool or data.get("confirm")!="CAMBIAR_ACCESO_GRATIS":
            raise web.HTTPBadRequest(text="Confirma el cambio de acceso gratuito.")
        if is_admin(target):
            raise web.HTTPForbidden(text="No se modifican cuentas administradoras.")
        # User may not have visited Web Galerías yet: grants remain saved.
        await db.execute("""INSERT INTO wg_free_access(uid,enabled,updated,changed_by)
          VALUES($1,$2,$3,$4) ON CONFLICT(uid) DO UPDATE
          SET enabled=excluded.enabled,updated=excluded.updated,
              changed_by=excluded.changed_by""",target,enabled,time.time(),uid)
        await g.log(uid,"admin-free-grant" if enabled else "admin-free-revoke")
        return {"ok":True,"telegram_id":target,"free_access":enabled}

    if op == "admin-free-access-list":
        after=identifier(data.get("after",0),0)
        file_grants=sorted(uid for uid in file_ids(ACCESS)
                           if not payment_test_user(uid))
        rows=await db.fetch("""
            WITH file_ids AS (SELECT unnest($1::bigint[]) AS uid),
                 candidates AS (
                   SELECT uid FROM file_ids UNION SELECT uid FROM wg_free_access
                 )
            SELECT c.uid,u.name,COALESCE(gr.enabled,f.uid IS NOT NULL) AS active
            FROM candidates c LEFT JOIN file_ids f ON f.uid=c.uid
            LEFT JOIN wg_free_access gr ON gr.uid=c.uid
            LEFT JOIN wg_users u ON u.uid=c.uid
            WHERE c.uid>$2
            ORDER BY c.uid LIMIT 51
        """,file_grants,after)
        return {"users":[{"telegram_id":int(r["uid"]),
                         "name":str(r["name"] or "Usuario"),
                         "free_access":bool(r["active"])} for r in rows[:50]],
                "next":int(rows[49]["uid"]) if len(rows)>50 else None}

    if op == "admin-users-page":
        # Load only the selected list; never enumerate all user records first.
        scope=data.get("scope","all")
        if scope not in ("all","paid","expired","free","visitors","profiles","recent"):
            raise web.HTTPBadRequest(text="Sección de usuarios no válida.")
        hours=data.get("hours",0)
        if type(hours) is not int or hours not in (0,24,168,720):
            raise web.HTTPBadRequest(text="Período no válido.")
        query=data.get("query","")
        if not isinstance(query,str) or len(query)>100:
            raise web.HTTPBadRequest(text="Búsqueda de usuarios inválida.")
        query=query.strip().lower()
        after=data.get("after")
        if after is None:
            last_seen=None
            after_uid=0
        else:
            if not isinstance(after,dict) or type(after.get("seen")) not in (int,float):
                raise web.HTTPBadRequest(text="Página de usuarios inválida.")
            last_seen=float(after["seen"])
            if not 0<=last_seen<=time.time()+86400:
                raise web.HTTPBadRequest(text="Página de usuarios inválida.")
            after_uid=identifier(after.get("uid"))
        now=time.time()
        free_ids=sorted(value for value in file_ids(ACCESS)
                        if not payment_test_user(value))
        admins=sorted(file_ids(ROOT/"admins.txt"))
        rows=await db.fetch("""
          SELECT u.uid,u.name,u.last_seen,
                 COALESCE(s.state IN ('active','trialing') AND s.paid_until>$1,FALSE) AS paid,
                 COALESCE(b.blocked,FALSE) AS blocked,
                 COALESCE(p.updated>$2 AND p.view IN ('library','album','viewer'),FALSE) AS online,
                 COALESCE(gr.enabled,u.uid=ANY($7::bigint[])) AS is_free,
                 COALESCE(s.cancel_at_period_end,FALSE) AS cancel_scheduled,
                 EXISTS(
                   SELECT 1 FROM wg_plus_checkouts xc
                   JOIN wg_plus_subscriptions xs ON xs.subscription=xc.subscription
                   WHERE xc.uid=u.uid AND xc.session IS NOT NULL
                     AND xs.price_id=$10
                     AND xs.status IN ('active','trialing')
                     AND xs.paid_until>$1
                 ) AS plus_bought
          FROM wg_users u
          LEFT JOIN wg_subscriptions s ON s.uid=u.uid
          LEFT JOIN wg_access_blocks b ON b.uid=u.uid
          LEFT JOIN wg_gallery_presence p ON p.uid=u.uid
          LEFT JOIN wg_free_access gr ON gr.uid=u.uid
          WHERE ($3::text='' OR POSITION($3 IN LOWER(u.name))>0
                        OR POSITION($3 IN u.uid::text)>0)
            AND ($4::double precision IS NULL
              OR (u.last_seen,u.uid)<($4,$5::bigint))
            AND (
               $6::text IN ('all','profiles','recent')
               OR ($6='paid' AND s.state IN ('active','trialing')
                     AND s.paid_until>$1 AND NOT COALESCE(b.blocked,FALSE))
               OR ($6='expired' AND s.paid_until>0
                     AND (s.state NOT IN ('active','trialing') OR s.paid_until<=$1))
               OR ($6='free' AND COALESCE(gr.enabled,u.uid=ANY($7::bigint[]))
                     AND NOT COALESCE(b.blocked,FALSE))
               OR ($6='visitors' AND (s.uid IS NULL OR s.paid_until<=0)
                     AND NOT COALESCE(gr.enabled,u.uid=ANY($7::bigint[]))
                     AND NOT (u.uid=ANY($9::bigint[])))
            )
            AND ($8::int=0 OR u.last_seen>=$1-($8::int*3600))
          ORDER BY u.last_seen DESC,u.uid DESC LIMIT 41
        """,now,now-75,query,last_seen,after_uid,scope,free_ids,hours,admins,
            os.getenv("GALERIAS_PLUS_PRICE",""))
        page=rows[:40]
        return {"users":[{
            "telegram_id":int(r["uid"]),"name":str(r["name"] or "Usuario"),
            "paid":bool(r["paid"]),"blocked":bool(r["blocked"]),
            "free_access":bool(r["is_free"]) and not r["blocked"],
            "online":bool(r["online"]) and not r["blocked"],
            "cancel_scheduled":bool(dict(r).get("cancel_scheduled")) and bool(r["paid"]),
            "plus_bought":bool(dict(r).get("plus_bought")),
            "last_seen":r["last_seen"],
        } for r in page],
            "next":{"seen":float(page[-1]["last_seen"]),"uid":int(page[-1]["uid"])}
              if len(rows)>40 else None}

    if op == "admin-user-access-status":
        target=identifier(data.get("telegram_id"))
        row=await db.fetchrow("""
            SELECT u.uid,u.name,u.first_seen,u.last_seen,
              s.state,s.paid_until,COALESCE(b.blocked,FALSE) AS blocked,
              gr.enabled AS free_override,
              COALESCE(s.cancel_at_period_end,FALSE) AS cancel_scheduled,
              EXISTS(
                SELECT 1 FROM wg_plus_checkouts xc
                JOIN wg_plus_subscriptions xs ON xs.subscription=xc.subscription
                WHERE xc.uid=u.uid AND xc.session IS NOT NULL
                AND xs.price_id=$2 AND xs.status IN ('active','trialing')
                AND xs.paid_until>$3
              ) AS plus_bought
            FROM wg_users u LEFT JOIN wg_subscriptions s ON s.uid=u.uid
            LEFT JOIN wg_access_blocks b ON b.uid=u.uid
            LEFT JOIN wg_free_access gr ON gr.uid=u.uid
            WHERE u.uid=$1""",target,os.getenv("GALERIAS_PLUS_PRICE",""),time.time())
        if not row:raise web.HTTPNotFound(text="Usuario no registrado.")
        override=row["free_override"]
        return {**dict(row),"telegram_id":target,
                "free_access":bool((override if override is not None
                                  else is_tester(target) and not payment_test_user(target))
                                  and not row["blocked"])}

    if op == "admin-user-access-change":
        target=identifier(data.get("telegram_id"))
        blocked=data.get("blocked")
        if type(blocked) is not bool or data.get("confirm")!="CAMBIAR_ACCESO_GALERIAS":
            raise web.HTTPBadRequest(text="Confirma el cambio de acceso.")
        if is_admin(target) or target==uid:
            raise web.HTTPForbidden(text="No se puede retirar una cuenta administradora.")
        if not await db.fetchval("SELECT 1 FROM wg_users WHERE uid=$1",target):
            raise web.HTTPNotFound(text="Usuario no registrado.")
        now=time.time()
        async with db.acquire() as conn,conn.transaction():
            await conn.execute("""
              INSERT INTO wg_access_blocks(uid,blocked,updated)
              VALUES($1,$2,$3) ON CONFLICT(uid) DO UPDATE
                SET blocked=excluded.blocked,updated=excluded.updated""",
              target,blocked,now)
            if blocked:
                await conn.execute("DELETE FROM wg_gallery_presence WHERE uid=$1",target)
                await conn.execute("UPDATE wg_download_tickets SET expires=$2 WHERE uid=$1 AND expires>$2",target,now)
        await g.log(uid,"admin-user-retired" if blocked else "admin-user-restored")
        return {"ok":True,"blocked":blocked}

    if op == "admin-user-personal-folders":
        target=identifier(data.get("telegram_id"))
        rows=await db.fetch("""
            SELECT p.id,p.name,p.created,
              count(i.media)::int AS total,
              count(m.id) FILTER(WHERE m.state IN ('preview','ready'))::int AS visible
            FROM wg_personal_folders p
            LEFT JOIN wg_personal_items i ON i.folder=p.id AND i.uid=p.uid
            LEFT JOIN wg_media m ON m.id=i.media
            WHERE p.uid=$1 GROUP BY p.id
            ORDER BY p.created DESC,p.id DESC LIMIT 101""",target)
        return {"folders":[dict(row) for row in rows[:100]]}

    if op == "admin-user-personal-items":
        target=identifier(data.get("telegram_id"))
        folder=identifier(data.get("personal_folder"))
        cursor=identifier(data.get("cursor",0),0)
        exists=await db.fetchval("SELECT 1 FROM wg_personal_folders WHERE uid=$1 AND id=$2",target,folder)
        if not exists:raise web.HTTPNotFound(text="Carpeta de usuario no encontrada.")
        rows=await db.fetch("""
          SELECT m.id,m.kind,m.filename,m.state,m.thumb_key,m.file_key
          FROM wg_personal_items i JOIN wg_media m ON m.id=i.media
          WHERE i.uid=$1 AND i.folder=$2 AND m.state IN ('preview','ready')
            AND m.id<$3 ORDER BY m.id DESC LIMIT 41
        """,target,folder,cursor or 2**63-1)
        return {"items":[{
            "id":int(r["id"]),"kind":r["kind"],"filename":r["filename"],
            "thumb":("/web-galerias/media/"+str(r["id"])+"/thumb?t="+
                     temporary_link(g,uid,r["id"],"thumb")) if r["thumb_key"] else None,
            "view":("/web-galerias/media/"+str(r["id"])+"/view?t="+
                    temporary_link(g,uid,r["id"],"view"))
                    if r["state"]=="ready" and r["file_key"] else None
        } for r in rows[:40]],
          "next":int(rows[39]["id"]) if len(rows)>40 else None}

    if op == "admin-user-personal-remove":
        target=identifier(data.get("telegram_id"))
        folder=identifier(data.get("personal_folder"))
        ids=data.get("ids")
        if (not isinstance(ids,list) or not 1<=len(ids)<=100
              or data.get("confirm")!="QUITAR_DE_CARPETA"):
            raise web.HTTPBadRequest(text="Confirma los archivos a retirar.")
        ids=list(dict.fromkeys(identifier(v) for v in ids))
        async with db.acquire() as conn,conn.transaction():
            owned=await conn.fetchval("""
                SELECT 1 FROM wg_personal_folders
                WHERE uid=$1 AND id=$2 FOR UPDATE""",target,folder)
            if not owned:raise web.HTTPNotFound(text="Carpeta de usuario no encontrada.")
            matches=await conn.fetchval("""
                SELECT count(*) FROM wg_personal_items
                WHERE uid=$1 AND folder=$2 AND media=ANY($3::bigint[])""",
                target,folder,ids)
            if matches!=len(ids):
                raise web.HTTPConflict(text="La carpeta cambió; actualiza su contenido.")
            await conn.execute("""
              DELETE FROM wg_personal_items WHERE uid=$1 AND folder=$2
              AND media=ANY($3::bigint[])""",target,folder,ids)
            await conn.execute("""
              UPDATE wg_personal_folders SET cover_media=NULL
              WHERE uid=$1 AND id=$2 AND cover_media=ANY($3::bigint[])""",target,folder,ids)
        await g.log(uid,"admin-personal-remove")
        return {"ok":True,"removed":len(ids)}

    if op == "admin-user-personal-delete":
        target=identifier(data.get("telegram_id"))
        folder=identifier(data.get("personal_folder"))
        if data.get("confirm")!="ELIMINAR_CARPETA_PERSONAL":
            raise web.HTTPBadRequest(text="Confirma la eliminación de esta carpeta.")
        result=await db.execute("""
            DELETE FROM wg_personal_folders WHERE uid=$1 AND id=$2""",target,folder)
        if result!="DELETE 1":
            raise web.HTTPNotFound(text="Carpeta no encontrada.")
        await g.log(uid,"admin-personal-folder-delete")
        return {"ok":True}

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
