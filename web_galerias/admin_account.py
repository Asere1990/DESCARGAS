"""Authenticated admin look-through using the real Web Galerías client UI.

All collection lookups are scoped to the selected *customer*. Media and cover
links are signed for the actual verified admin, never for the customer.
No Telegram sessions or download quota can be impersonated; browsing is read
only except existing explicit administrator moderation operations.
"""
import time

from aiohttp import web

from .core import identifier, utc_day
from .permissions import is_admin
from .downloads import temporary_link
from .covers import temporary_cover
from . import personal, user_features, location, download_plus
from .admin_mirror import _preview


def safe_owner_id(value):
    return identifier(value)


async def operation(g, actor, op, data):
    if not is_admin(actor):
        raise web.HTTPForbidden(text="Solo administradores verificados.")
    target = safe_owner_id(data.get("telegram_id"))
    db = g.db()
    row = await db.fetchrow("SELECT uid,name FROM wg_users WHERE uid=$1", target)
    if not row:
        raise web.HTTPNotFound(text="Usuario no registrado en Web Galerías.")
    # Every call re-verifies the signed administrator. A user-supplied target
    # ID never acts as an auth principal or becomes an ordinary user session.
    if op == "admin-view-bootstrap":
        allowed = await g.entitled(target)
        folders = await g.folder_list(target) if allowed else []
        owned = await personal.folders(g, target) if allowed else []
        if allowed:
            for folder in folders:
                # Exactly the folder visibility and public names of this owner.
                # Only the administrator receives the short-lived signed URLs.
                folder.pop("pending", None)
                folder.pop("complete", None)
                folder.pop("error", None)
                if folder.pop("cover_updated", None):
                    folder["cover"] = temporary_cover(g, actor, folder["id"])
                else:
                    cover = await db.fetchval("""
                      SELECT id FROM wg_media
                       WHERE folder=$1 AND state IN ('preview','ready')
                         AND thumb_key IS NOT NULL
                       ORDER BY date DESC LIMIT 1
                    """, folder["id"])
                    folder["cover"] = (
                        f"/web-galerias/media/{cover}/thumb?t="
                        + temporary_link(g, actor, cover, "thumb")
                    ) if cover else None
            for folder in owned:
                cover = str(folder.get("cover") or "")
                if cover.startswith("/web-galerias/personal-cover/"):
                    fid = identifier(folder["id"])
                    folder["cover"] = (
                        f"/web-galerias/admin/personal-cover/{fid}?t="
                        + temporary_link(g, actor, fid, "admin-personal-cover")
                    )
                elif cover.startswith("/web-galerias/media/"):
                    from urllib.parse import urlsplit
                    from re import fullmatch
                    path = urlsplit(cover).path
                    match = fullmatch(r"/web-galerias/media/([0-9]+)/thumb", path)
                    if match:
                        mid = identifier(match.group(1))
                        folder["cover"] = (
                            f"/web-galerias/media/{mid}/thumb?t="
                            + temporary_link(g, actor, mid, "thumb"))
                    else:
                        folder["cover"] = None
        plan = await download_plus.tier(g, target) if allowed else {
            "tier": "basic", "daily_limit": download_plus.BASE_LIMIT,
            "quota_unlimited": False, "plus_active": False,
            "linked_descargas": False,
        }
        limit = plan["daily_limit"]
        used = 0 if limit is None else int(await db.fetchval(
            "SELECT used FROM wg_quota WHERE uid=$1 AND day=$2",
            target, utc_day()) or 0)
        return {
            "admin": True, "admin_view": True, "allowed": bool(allowed),
            "owner_telegram_id": target,
            "owner_name": str(row["name"] or "Usuario")[:100],
            "owner_has_access": bool(allowed),
            "design": await g.design(),
            "folders": folders, "personal_folders": owned,
            "photos": sum(int(x["photos"]) for x in folders),
            "videos": sum(int(x["videos"]) for x in folders),
            "quota_tier": plan["tier"],
            "quota_daily_limit": limit, "quota_unlimited": plan["quota_unlimited"],
            "quota_plus_active": plan["plus_active"],
            "quota_linked": plan["linked_descargas"],
            "remaining": None if limit is None else max(0, limit-used),
            "used": used, "free_access": bool(allowed and await g.free_granted(target)),
            "checkout_enabled": False, "checkout_ready": False,
            "quota_plus_available": False, "quota_link_bot": None,
            "quota_link_miniapp": None,
            "storage_ready": g.storage.ready,
            "pilot": False,
        }

    # An admin may inspect an expired user's billing status, but their content
    # is not silently accessible as if that customer's own membership were paid.
    if not await g.entitled(target):
        raise web.HTTPForbidden(text="La membresía de este usuario no está vigente.")

    if op == "admin-view-collections":
        return await user_features.collections(g, target)

    if op == "admin-view-favorites":
        result = await user_features.items(g, target, data)
        return {
            "items": [_preview(g, actor, item) for item in result["items"]],
            "next": result["next"],
        }

    if op == "admin-view-items":
        cursor = identifier(data.get("cursor", 0), 0)
        query = data.get("query", "")
        if not isinstance(query, str) or len(query) > 100:
            raise web.HTTPBadRequest(text="Búsqueda incorrecta.")
        kind = personal.media_filter(data.get("kind", "all"))
        # Return the EXACT contents permitted to the target, not all galleries
        # available to the administrator and not the administrator's favorites.
        if data.get("collection") is not None:
            return await personal.items(
                g, target, data["collection"], query, cursor, kind,
                lambda _, media_id, media_kind:
                    temporary_link(g, actor, media_id, media_kind)
            )
        folder = identifier(data.get("folder"))
        return await g.list_items(
            target, folder, query, cursor,
            lambda _, media_id, media_kind:
                temporary_link(g, actor, media_id, media_kind),
            kind
        )

    if op == "admin-view-locate":
        result = await location.locate(g, target, data)
        return {
            **result,
            "items": [_preview(g, actor, item) for item in result["items"]]
        }

    if op == "admin-view-remove-favorites":
        ids = data.get("ids")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                or data.get("confirm") != "QUITAR_FAVORITOS_ADMIN"):
            raise web.HTTPBadRequest(text="Confirma los favoritos a retirar.")
        ids = list(dict.fromkeys(identifier(x) for x in ids))
        result = await db.execute(
            "DELETE FROM wg_favorites WHERE uid=$1 AND media=ANY($2::bigint[])",
            target, ids)
        await g.log(actor, "admin-view-favorite-removed")
        return {"ok": True, "removed": int(result.split()[-1])}

    raise web.HTTPNotFound()
