"""Short-lived, read-only native iOS download links for purchased Telegram Stars files.

These links allow Safari's download manager to stream large attachments without
using a hidden iframe, Web Share, multi-gigabyte JS blobs, or a second Telegram
login. They never update linked sessions, account locks, databases or workers.

The encrypted bearer link is temporary. Every request also checks current admin
permission, the linked-account generation and the panel session version; the
existing purchase resolver confirms the exact Stars transaction.
"""
import time
from urllib.parse import quote

from aiohttp import web

from .creator_access import require_account
from .moderation import authorized

TICKET_SECONDS = 6 * 60 * 60
MAX_INDEX = 100


def issue(panel, actor, uid, generation, source, peer, transaction, index, version):
    """Create an opaque expiring URL; no database write or Telegram API call."""
    index = int(index)
    if not 0 <= index < MAX_INDEX:
        raise ValueError("Archivo comprado no válido.")
    if not isinstance(transaction, str) or not 0 < len(transaction) <= 256:
        raise ValueError("Compra de Telegram no válida.")
    ticket = panel.pack(
        "native-stars-file", actor=int(actor), uid=int(uid),
        generation=int(generation), source=int(source),
        peer=peer, transaction=transaction, index=index,
        version=str(version), issued=int(time.time()),
        expires=int(time.time()) + TICKET_SECONDS,
    )
    return panel.s.url.rstrip("/") + "/panel/api/native-stars-file?ticket=" + quote(ticket, safe="")


def validate(panel, token, now=None):
    """Validate ticket fields before using ANY linked account connection."""
    try:
        data = panel.unpack(token, "native-stars-file")
        now = int(time.time()) if now is None else int(now)
        fields = ("actor", "uid", "generation", "source", "index", "issued", "expires")
        if any(not isinstance(data.get(k), int) for k in fields):
            raise ValueError()
        if data["actor"] <= 0 or data["uid"] <= 0 or data["generation"] < 0:
            raise ValueError()
        if not 0 <= data["index"] < MAX_INDEX:
            raise ValueError()
        if not 0 < data["expires"] - data["issued"] <= TICKET_SECONDS:
            raise ValueError()
        if data["issued"] > now + 60 or now > data["expires"]:
            raise ValueError()
        if (not isinstance(data.get("version"), str)
                or not isinstance(data.get("peer"), str)
                or not isinstance(data.get("transaction"), str)
                or not data["transaction"] or len(data["transaction"]) > 256):
            raise ValueError()
        peer = panel.unpack(data["peer"], "peer")
        if any(peer.get(k) != data[k] for k in ("actor", "uid", "generation")):
            raise ValueError()
        from .web_panel import source_id
        if source_id(peer) != data["source"]:
            raise ValueError()
        return data
    except (TypeError, KeyError, ValueError, OverflowError):
        raise web.HTTPNotFound() from None


async def serve(panel, request):
    """Serve GET or HEAD to the native browser download manager, cookie-free."""
    if request.method not in ("GET", "HEAD"):
        raise web.HTTPMethodNotAllowed(request.method, ("GET", "HEAD"))
    token = request.query.get("ticket", "")
    if len(token) > 4000:
        raise web.HTTPNotFound()
    data = validate(panel, token)
    actor, uid = data["actor"], data["uid"]
    if not authorized(actor):
        raise web.HTTPNotFound()
    try:
        await require_account(panel.s, uid, actor)
        row = await panel.s.repo.account(uid)
        if (not row or not row["session"]
                or row["generation"] != data["generation"]):
            raise ValueError("La vinculación de Telegram ya no coincide.")
        current = await panel.s.repo.setting(
            f"web-panel-version:{panel.s.bot_id}:{actor}") or "0"
        if current != data["version"]:
            raise ValueError("El enlace fue revocado.")
    except ValueError:
        raise web.HTTPNotFound() from None
    purchase = panel.pack(
        "purchase", actor=actor, uid=uid, generation=data["generation"],
        source=data["source"], transaction=data["transaction"],
    )
    # The original paid-media resolver revalidates purchase ownership through
    # Telegram on EVERY request, including resumed HTTP ranges.
    return await panel.media(request, actor, uid, data["peer"], 1,
                             purchase=purchase, index=data["index"],
                             native_download=True)
