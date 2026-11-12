"""Read-only, administrator-only mirror of a Web Galerías user's collections.

Uses the same personal/favorite queries as the signed user's Mini App, but
issues media links bound to the actual administrator. No impersonation,
subscription changes, file changes or Telegram account sessions.
"""
from datetime import date
import re
import time

from aiohttp import web

from .core import identifier, utc_day
from .permissions import is_admin
from .downloads import temporary_link
from . import personal, user_features, download_plus


def _preview(g, admin_uid, item):
    """Replace an owner's private media token with the current admin's token."""
    mid = identifier(item["id"])
    out = dict(item)
    for field, kind in (("thumb", "thumb"), ("view", "view")):
        if item.get(field):
            out[field] = (
                f"/web-galerias/media/{mid}/{kind}?t="
                + temporary_link(g, admin_uid, mid, kind)
            )
    return out


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise web.HTTPBadRequest(text="Fecha incorrecta.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise web.HTTPBadRequest(text="Fecha incorrecta.") from None


async def operation(g, admin_uid, op, data):
    if not is_admin(admin_uid):
        raise web.HTTPForbidden(text="Solo administradores.")
    target = identifier(data.get("telegram_id"))
    db = g.db()
    user = await db.fetchrow(
        "SELECT uid,name FROM wg_users WHERE uid=$1", target
    )
    if not user:
        raise web.HTTPNotFound(text="Usuario no registrado.")
    if op == "admin-mirror-overview":
        plan = await download_plus.tier(g, target)
        used = int(await db.fetchval(
            "SELECT used FROM wg_quota WHERE uid=$1 AND day=$2",
            target, utc_day()) or 0)
        totals = await db.fetchrow("""
          SELECT COUNT(*)::int AS batches,
                 COALESCE(SUM(cardinality(ids)),0)::bigint AS files
          FROM wg_download_tickets WHERE uid=$1
        """, target)
        today = await db.fetchval("""
          SELECT COALESCE(SUM(cardinality(ids)),0)::bigint
          FROM wg_download_tickets WHERE uid=$1 AND day=$2
        """, target, utc_day())
        count_favs = await db.fetchval(
            "SELECT COUNT(*) FROM wg_favorites WHERE uid=$1", target)
        count_folders = await db.fetchval(
            "SELECT COUNT(*) FROM wg_personal_folders WHERE uid=$1", target)
        daily = plan["daily_limit"]
        return {
            "telegram_id": target, "name": str(user["name"] or "Usuario"),
            "tier": str(plan["tier"]), "unlimited": bool(plan["quota_unlimited"]),
            "daily_limit": daily, "used": used,
            "remaining": None if daily is None else max(0, int(daily)-used),
            "today_requested": int(today or 0),
            "total_requested": int(totals["files"] or 0) if totals else 0,
            "batches": int(totals["batches"] or 0) if totals else 0,
            "favorites": int(count_favs or 0), "folders": int(count_folders or 0),
            "day": utc_day().isoformat(),
        }
    if op == "admin-mirror-folders":
        # Exact user-visible folder counts, filtering and selected-cover order.
        folders = await personal.folders(g, target)
        output = []
        for entry in folders:
            item = dict(entry)
            cover = str(item.get("cover") or "")
            if cover.startswith("/web-galerias/personal-cover/"):
                fid = identifier(item["id"])
                item["cover"] = (f"/web-galerias/admin/personal-cover/{fid}?t="
                                 + temporary_link(g, admin_uid, fid,
                                                  "admin-personal-cover"))
            else:
                match = re.match(r"^/web-galerias/media/(\d+)/thumb\?t=", cover)
                if match:
                    mid = identifier(match.group(1))
                    item["cover"] = (f"/web-galerias/media/{mid}/thumb?t="
                                     + temporary_link(g, admin_uid, mid, "thumb"))
                else:
                    item["cover"] = None
            output.append(item)
        return {"folders": output}
    if op in ("admin-mirror-folder-items", "admin-mirror-favorites"):
        cursor = identifier(data.get("cursor", 0), 0)
        query = data.get("query", "")
        if not isinstance(query, str) or len(query) > 100:
            raise web.HTTPBadRequest(text="Búsqueda incorrecta.")
        kind = personal.media_filter(data.get("kind", "all"))
        if op == "admin-mirror-folder-items":
            folder = identifier(data.get("personal_folder"))
            result = await personal.items(
                g, target, folder, query, cursor, kind, lambda _uid, mid, media_kind:
                    temporary_link(g, admin_uid, mid, media_kind)
            )
            # personal.items already builds links for the administrator via
            # the supplied signer while retaining the user's visibility rules.
            return result
        result = await user_features.items(
            g, target, {"cursor": cursor, "query": query, "kind": kind})
        return {"items": [_preview(g, admin_uid, item)
                          for item in result["items"]],
                "next": result["next"]}
    if op == "admin-mirror-download-dates":
        before = data.get("before")
        before = _date(before) if before else None
        rows = await db.fetch("""
          SELECT day,COUNT(*)::int AS requests,
                 COALESCE(SUM(cardinality(ids)),0)::bigint AS files
          FROM wg_download_tickets
          WHERE uid=$1 AND ($2::date IS NULL OR day<$2::date)
          GROUP BY day ORDER BY day DESC LIMIT 31
        """, target, before)
        page = rows[:30]
        return {"dates": [{"date": item["day"].isoformat(),
                           "files": int(item["files"]),
                           "requests": int(item["requests"])}
                          for item in page],
                "next": page[-1]["day"].isoformat() if len(rows)>30 else None}
    if op == "admin-mirror-download-items":
        day = _date(data.get("date"))
        offset = data.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 100000:
            raise web.HTTPBadRequest(text="Página incorrecta.")
        # The existing quota is debited on reserve(), so these are durable
        # *accounted-for* requests, not proof that iOS saved each file.
        # Never return or log the one-use bearer ticket token.
        rows = await db.fetch("""
          SELECT x.media_id AS id,m.kind,m.filename,m.state,
                 m.thumb_key,m.file_key,t.expires
          FROM wg_download_tickets t
          CROSS JOIN LATERAL UNNEST(t.ids) WITH ORDINALITY
               AS x(media_id,position)
          LEFT JOIN wg_media m ON m.id=x.media_id
          WHERE t.uid=$1 AND t.day=$2
          ORDER BY t.expires DESC,t.token DESC,x.position DESC
          LIMIT 41 OFFSET $3
        """, target, day, offset)
        items = []
        for row in rows[:40]:
            mid = int(row["id"])
            visible = row["state"] in ("preview", "ready")
            ready = row["state"] == "ready" and bool(row["file_key"])
            items.append({
                "id": mid, "kind": row["kind"] or "file",
                "filename": row["filename"] or "Archivo no disponible",
                "available": bool(ready),
                "requested_at": max(0, int(float(row["expires"])-300)),
                "thumb": (f"/web-galerias/media/{mid}/thumb?t="
                          + temporary_link(g, admin_uid, mid, "thumb"))
                         if visible and row["thumb_key"] else None,
                "view": (f"/web-galerias/media/{mid}/view?t="
                         + temporary_link(g, admin_uid, mid, "view"))
                        if ready else None,
            })
        return {"items": items,
                "next": offset+40 if len(rows)>40 else None,
                "date": day.isoformat()}
    raise web.HTTPNotFound()


async def personal_cover(g, request):
    """Signed admin-only uploaded custom folder cover, no owner impersonation."""
    from .downloads import validate_link
    proof = validate_link(g, request.query.get("t"), "admin-personal-cover")
    admin_uid = identifier(proof["uid"])
    fid = identifier(request.match_info["folder"])
    if not is_admin(admin_uid) or fid != proof["item"]:
        raise web.HTTPForbidden(text="Acceso administrativo no autorizado.")
    row = await g.db().fetchrow("""
      SELECT c.mime,c.body
      FROM wg_personal_covers c
      JOIN wg_personal_folders f ON f.id=c.folder AND f.uid=c.uid
      WHERE f.id=$1
    """, fid)
    if not row:
        raise web.HTTPNotFound(text="Portada no disponible.")
    return web.Response(body=bytes(row["body"]), content_type=row["mime"],
                        headers={"Cache-Control":"private, max-age=120",
                                 "X-Content-Type-Options":"nosniff",
                                 "Referrer-Policy":"no-referrer"})
