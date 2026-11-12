"""Verified administrator inspection of the *actual* selected user account.

One shared customer bootstrap, same folders/favorites/media signatures and
quota. Authentication remains the administrator's own signed Telegram session;
extra admin moderation controls are explicitly separated and audited.
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
        # The selected user and the administrator use exactly ONE bootstrap:
        # the normal customer routine. No imitation or separately maintained
        # quota/folder/status logic. The only added data enables admin tools.
        result = await g.bootstrap(target, track_visit=False)
        result["admin_view"] = True
        result["admin_controls"] = True
        result["owner_telegram_id"] = target
        result["owner_name"] = str(row["name"] or "Usuario")[:100]
        result["owner_has_access"] = bool(result["allowed"])
        return result

    # An admin may inspect an expired user's billing status, but their content
    # is not silently accessible as if that customer's own membership were paid.
    if not await g.entitled(target):
        raise web.HTTPForbidden(text="La membresía de este usuario no está vigente.")

    if op == "admin-view-quota-plus-checkout":
        # Exactly the same Stripe session flow and entitlement as the
        # selected user's Plus button; an admin click is an explicit purchase
        # choice, never an automatic subscription change.
        return await download_plus.checkout(g, target)

    if op == "admin-view-media-status":
        # Re-check the authoritative R2-backed media row each time the admin
        # opens a user folder/favorite/download. The iframe doesn't auto-refresh
        # its full lists to protect client scrolling, so cached preview status
        # might be stale after the original finishes uploading.
        media_id = identifier(data.get("media"))
        row = await db.fetchrow("""
          SELECT m.id,m.folder,m.kind,m.mime,m.state,m.file_key,m.thumb_key,
                 f.active
          FROM wg_media m JOIN wg_folders f ON f.id=m.folder
          WHERE m.id=$1
        """, media_id)
        if not row or not row["active"] or row["state"] not in ("preview","ready"):
            raise web.HTTPNotFound(text="Archivo no disponible.")
        # The user view never gets an administrative media key. Links are signed
        # for the actual verified administrator and remain bound to the item.
        if not await g.permitted(target, row["folder"]):
            raise web.HTTPForbidden(text="No autorizado.")
        ready = row["state"]=="ready" and bool(row["file_key"])
        visible = bool(row["thumb_key"])
        return {
            "media": media_id, "kind": row["kind"], "mime": row["mime"],
            "available": ready,
            "view": (
                f"/web-galerias/media/{media_id}/view?t="
                + temporary_link(g, target, media_id, "view")
            ) if ready else None,
            "thumb": (
                f"/web-galerias/media/{media_id}/thumb?t="
                + temporary_link(g, target, media_id, "thumb")
            ) if visible else None,
        }

    if op == "admin-view-collections":
        return await user_features.collections(g, target)

    if op == "admin-view-favorite-set":
        result = await user_features.set_favorite(g, target, data)
        await g.log(actor, f"admin-user-favorite:{target}")
        return result

    if op.startswith("admin-view-personal-"):
        source_op = op.removeprefix("admin-view-")
        # The same personal actions, with verified admin ownership of the
        # session and an explicit user target; never mutate the admin's folders.
        allowed = {
            "personal-create", "personal-add", "personal-remove",
            "personal-move", "personal-rename", "personal-delete",
            "personal-set-cover", "personal-cover-upload",
            "personal-cover-reset",
        }
        if source_op not in allowed:
            raise web.HTTPNotFound()
        result = await personal.operation(g, target, source_op, data)
        await g.log(actor, f"admin-user-personal:{target}:{source_op}")
        return result

    if op == "admin-view-favorites":
        # The exact same favorites and media links the customer receives.
        return await user_features.items(g, target, data)

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
                lambda owner, media_id, media_kind:
                    temporary_link(g, owner, media_id, media_kind)
            )
        folder = identifier(data.get("folder"))
        return await g.list_items(
            target, folder, query, cursor,
            lambda owner, media_id, media_kind:
                temporary_link(g, owner, media_id, media_kind),
            kind
        )

    if op == "admin-view-locate":
        return await location.locate(g, target, data)

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
