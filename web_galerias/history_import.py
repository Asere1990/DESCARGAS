"""Explicit Telegram history imports using Web Galerías' own reader sessions."""
import asyncio
import logging
import re
import secrets
import time

from aiohttp import web
from telethon import errors
from telethon.tl.types import auth

from .core import identifier
from .media_types import classify

LOG = logging.getLogger("web-galerias-history")


class ImportPaused(Exception):
    """A recoverable problem with a message suitable for the admin panel."""
SCHEMA = """
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS error TEXT NOT NULL DEFAULT '';
CREATE SEQUENCE IF NOT EXISTS wg_history_source_seq;
CREATE TABLE IF NOT EXISTS wg_history_readers (
    uid BIGINT PRIMARY KEY,
    session BYTEA NOT NULL
);
CREATE TABLE IF NOT EXISTS wg_history_imports (
    folder BIGINT NOT NULL REFERENCES wg_folders(id) ON DELETE CASCADE,
    peer BIGINT NOT NULL,
    source BIGINT NOT NULL UNIQUE REFERENCES wg_folders(id) ON DELETE CASCADE,
    uid BIGINT NOT NULL,
    cursor BIGINT NOT NULL DEFAULT 0,
    stop_at BIGINT,
    copied BIGINT NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued',
    available DOUBLE PRECISION NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(folder, peer)
);
ALTER TABLE wg_history_readers ENABLE ROW LEVEL SECURITY;
ALTER TABLE wg_history_imports ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON wg_history_readers, wg_history_imports FROM PUBLIC;
"""
SESSION_ERRORS = (
    errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
    errors.SessionExpiredError,
)


CODE_DELIVERY = {
    auth.SentCodeTypeApp: "Busca el código en el chat oficial «Telegram» de la cuenta que estás vinculando. Revisa también sus otros dispositivos.",
    auth.SentCodeTypeSms: "Revisa la app Mensajes del teléfono de la cuenta.",
    auth.SentCodeTypeCall: "Telegram indicó una llamada de voz que dicta el código.",
    auth.SentCodeTypeEmailCode: "Revisa el correo configurado para iniciar sesión en Telegram.",
}


def configured(g):
    return bool(g.storage.ready and g.s.cipher and g.s.api_id.isdigit()
                and re.fullmatch(r"[a-fA-F0-9]{32}", g.s.api_hash))


async def overview(g, actor, folder=None):
    if not getattr(g, "history_import_ready", False):
        return {"ready": False, "linked": False, "imports": []}
    db = g.db()
    readers = await db.fetch("SELECT uid FROM wg_history_readers ORDER BY uid")
    rows = await db.fetch("""
        SELECT folder, peer::text, uid::text, cursor, copied, state, error
        FROM wg_history_imports
        WHERE ($1::bigint IS NULL OR folder=$1)
        ORDER BY folder, peer
    """, folder)
    accounts = [str(row["uid"]) for row in readers]
    return {"ready": True, "linked": str(actor) in accounts, "uid": str(actor),
            "accounts": accounts, "imports": [dict(r) for r in rows]}


async def login(g, actor, step, data):
    pending = g.history_logins
    async with g.history_login_lock:
        now = time.monotonic()
        for uid, attempt in list(pending.items()):
            if now >= attempt["expires"]:
                pending.pop(uid)
        attempt = pending.get(actor)
        if step == "send":
            phone = re.sub(r"[ ()-]", "", str(data.get("phone", "")).strip())
            if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
                raise web.HTTPBadRequest(text="Escribe el teléfono con su prefijo internacional.")
            if now < g.history_next_code.get(actor, 0):
                raise web.HTTPTooManyRequests(text="Espera un minuto antes de pedir otro código.")
            g.history_next_code[actor] = now + 60
            pending.pop(actor, None)
            client = g.s.client("")
        else:
            if not attempt or attempt["step"] != step or attempt["flow"] != data.get("flow"):
                raise web.HTTPBadRequest(text="Solicita un nuevo código de Telegram.")
            if attempt["tries"] >= 5:
                pending.pop(actor, None)
                raise web.HTTPTooManyRequests(text="Solicita un nuevo código de Telegram.")
            attempt["tries"] += 1
            client = g.s.client(g.s.cipher.decrypt(attempt["session"]).decode())
        try:
            async with asyncio.timeout(25):
                await client.connect()
                if step == "send":
                    sent = await client.send_code_request(phone)
                    delivery_type = type(sent.type)
                    delivery = CODE_DELIVERY.get(delivery_type)
                    if not delivery:
                        LOG.warning("Gallery code delivery unsupported (%s)", delivery_type.__name__)
                        if delivery_type is auth.SentCodeTypeSetUpEmailRequired:
                            raise web.HTTPConflict(text="Telegram requiere configurar un correo de inicio de sesión en su app oficial antes de enviar un código.")
                        raise web.HTTPConflict(text="Telegram eligió un método de verificación que este panel aún no admite.")
                    flow = secrets.token_urlsafe(18)
                    pending[actor] = {
                        "flow": flow, "phone": phone, "hash": sent.phone_code_hash,
                        "session": g.s.cipher.encrypt(client.session.save().encode()),
                        "expires": now + 300, "tries": 0, "step": "code",
                    }
                    return {"step": "code", "flow": flow, "delivery": delivery}
                if step == "code":
                    code = str(data.get("code", "")).strip()
                    if not re.fullmatch(r"[0-9]{4,10}", code):
                        raise web.HTTPBadRequest(text="Escribe el código que envió Telegram.")
                    try:
                        me = await client.sign_in(attempt["phone"], code,
                                                  phone_code_hash=attempt["hash"])
                    except errors.SessionPasswordNeededError:
                        attempt.update(step="password", tries=0,
                                       session=g.s.cipher.encrypt(client.session.save().encode()))
                        return {"step": "password", "flow": attempt["flow"]}
                else:
                    password = str(data.get("password", ""))
                    if not 0 < len(password) <= 1024:
                        raise web.HTTPBadRequest(text="Escribe la contraseña de Telegram.")
                    me = await client.sign_in(password=password)
                pending.pop(actor, None)
                if not me or me.bot:
                    await client.log_out()
                    raise web.HTTPForbidden(text="Autoriza una cuenta personal de Telegram.")
                await g.db().execute("""
                    INSERT INTO wg_history_readers(uid,session) VALUES($1,$2)
                    ON CONFLICT(uid) DO UPDATE SET session=excluded.session
                """, me.id, g.s.cipher.encrypt(client.session.save().encode()))
                return {"step": "linked", "account": str(me.id)}
        except (errors.PhoneCodeInvalidError, errors.PasswordHashInvalidError):
            raise web.HTTPBadRequest(text="Telegram rechazó el código o la contraseña.") from None
        except (errors.PhoneCodeExpiredError, errors.PhoneCodeHashEmptyError):
            pending.pop(actor, None)
            raise web.HTTPBadRequest(text="El código venció. Solicita uno nuevo.") from None
        except errors.FloodWaitError as exc:
            g.history_next_code[actor] = time.monotonic() + max(60, exc.seconds)
            raise web.HTTPTooManyRequests(text=f"Telegram pide esperar {exc.seconds} segundos.") from None
        except errors.PhoneNumberInvalidError:
            raise web.HTTPBadRequest(text="Telegram rechazó el teléfono. Revisa el número y el prefijo internacional.") from None
        except TimeoutError:
            LOG.warning("Gallery Telegram request timed out at step %s", step)
            raise web.HTTPServiceUnavailable(text="Telegram tardó demasiado en responder. Espera un minuto antes de solicitar otro código.") from None
        except (errors.RPCError, OSError) as exc:
            LOG.warning("Gallery Telegram request failed at step %s (%s)", step, type(exc).__name__)
            raise web.HTTPBadRequest(text="Telegram no pudo completar la vinculación. Revisa los datos e inténtalo de nuevo.") from None
        finally:
            await client.disconnect()


async def operation(g, actor, op, data):
    if not getattr(g, "history_import_ready", False):
        raise web.HTTPServiceUnavailable(text="La importación directa aún no está disponible.")
    if op == "status":
        return await overview(g, actor, identifier(data.get("folder")))
    if op in ("send", "code", "password"):
        return await login(g, actor, op, data)
    if op != "start":
        raise web.HTTPNotFound()
    folder = identifier(data.get("folder"))
    uid = identifier(data.get("account"))
    raw_peer = str(data.get("peer", "")).strip()
    if not re.fullmatch(r"-[1-9][0-9]{2,18}", raw_peer) or int(raw_peer) <= -(2**63):
        raise web.HTTPBadRequest(text="Escribe el ID negativo completo del grupo o canal.")
    peer = int(raw_peer)
    async with g.db().acquire() as conn, conn.transaction():
        target = await conn.fetchval("""
            SELECT id FROM wg_folders f WHERE id=$1 AND NOT EXISTS (
                SELECT 1 FROM wg_folder_sources WHERE source_folder=f.id
            ) FOR UPDATE
        """, folder)
        if target is None:
            raise web.HTTPNotFound(text="Carpeta destino no encontrada.")
        if not await conn.fetchval("SELECT 1 FROM wg_history_readers WHERE uid=$1", uid):
            raise web.HTTPConflict(text="Esa cuenta debe autorizar su sesión en Web Galerías.")
        row = await conn.fetchrow("""
            SELECT source, state FROM wg_history_imports
            WHERE folder=$1 AND peer=$2 FOR UPDATE
        """, folder, peer)
        if row:
            if row["state"] in ("queued", "running"):
                return {"ok": True}
            await conn.execute("""
                UPDATE wg_history_imports SET uid=$3, state='queued',
                    available=0, error='', stop_at=CASE WHEN state='done' THEN NULL ELSE stop_at END
                WHERE folder=$1 AND peer=$2
            """, folder, peer, uid)
        else:
            source = await conn.fetchval("""
                INSERT INTO wg_folders(peer,name,created,source_enabled)
                VALUES(4611686018427387904 + nextval('wg_history_source_seq'),
                       $1,$2,FALSE) RETURNING id
            """, f"Historial de Telegram · {peer}", time.time())
            await conn.execute("""
                INSERT INTO wg_folder_sources(parent_folder,source_folder,created)
                VALUES($1,$2,$3)
            """, folder, source, time.time())
            await conn.execute("""
                INSERT INTO wg_history_imports(folder,peer,source,uid) VALUES($1,$2,$3,$4)
            """, folder, peer, source, uid)
    await g.log(actor, "admin-history-import-start")
    return {"ok": True}


async def import_history(g, job):
    from .sync import backup_one, catalog

    db = g.db()
    uid, source = job["uid"], job["source"]
    token = await db.fetchval("SELECT session FROM wg_history_readers WHERE uid=$1", uid)
    if not token:
        raise ImportPaused("La cuenta debe autorizarse en Web Galerías.")
    client = g.s.client(g.s.cipher.decrypt(token).decode())
    last_checked = float("-inf")

    async def guard():
        nonlocal last_checked
        now = time.monotonic()
        if now - last_checked >= 3:
            if not await db.fetchval(
                "SELECT 1 FROM wg_history_imports WHERE source=$1 AND state='running'", source
            ):
                raise ImportPaused("La importación perdió acceso a la carpeta o a la cuenta.")
            last_checked = now

    try:
        await client.connect()
        me = await client.get_me()
        if not me or me.id != uid:
            raise ImportPaused("La sesión de Telegram venció. Vuelve a vincular la cuenta.")
        dialog = None
        async for candidate in client.iter_dialogs():
            if candidate.id == job["peer"]:
                dialog = candidate
                break
        if not dialog or not (dialog.is_group or dialog.is_channel):
            raise ImportPaused("La cuenta debe estar en el grupo o canal indicado.")
        if getattr(dialog.entity, "noforwards", False):
            raise ImportPaused("Telegram marca este grupo o canal como contenido protegido.")
        stop_at = job["stop_at"]
        if stop_at is None:
            latest = await client.get_messages(dialog.input_entity, limit=1)
            stop_at = latest[0].id if latest else 0
            await db.execute("UPDATE wg_history_imports SET stop_at=$2 WHERE source=$1",
                             source, stop_at)
        if stop_at <= job["cursor"]:
            return
        last_seen = job["cursor"]
        async for message in client.iter_messages(
            dialog.input_entity, reverse=True, min_id=job["cursor"], max_id=stop_at + 1
        ):
            await guard()
            last_seen = message.id
            if (not classify(message)[0] or getattr(message, "noforwards", False)
                or getattr(message, "ttl_period", None)
                or getattr(getattr(message, "media", None), "ttl_seconds", None)):
                continue
            await catalog(g, source, message)
            item = await db.fetchrow("""
                SELECT * FROM wg_media WHERE folder=$1 AND message=$2
            """, source, message.id)
            if item and item["state"] == "pending":
                await backup_one(g, source, item, message, client, guard)
                if not await db.fetchval("""
                    SELECT 1 FROM wg_media WHERE folder=$1 AND message=$2 AND state='ready'
                """, source, message.id):
                    raise ImportPaused("La copia no se confirmó en R2.")
            await db.execute("""
                UPDATE wg_history_imports SET cursor=$2, copied=copied+
                    CASE WHEN EXISTS(SELECT 1 FROM wg_media
                                     WHERE folder=$1 AND message=$2 AND state='ready')
                         THEN 1 ELSE 0 END
                WHERE source=$1
            """, source, message.id)
        await db.execute("UPDATE wg_history_imports SET cursor=$2 WHERE source=$1",
                         source, last_seen)
    finally:
        await client.disconnect()


async def run(g):
    """A single worker, with the latest confirmed message persisted on every item."""
    db = g.db()
    while not g.closed:
        try:
            await db.execute(SCHEMA)
            g.history_import_ready = True
            async with db.acquire() as leader:
                if await leader.fetchval("SELECT pg_try_advisory_lock(784,24)"):
                    LOG.warning("Telegram history import ready")
                    try:
                        await work(g, db)
                    finally:
                        await leader.execute("SELECT pg_advisory_unlock(784,24)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("History worker retrying (%s)", type(exc).__name__)
        await asyncio.sleep(5)


async def work(g, db):
    await db.execute("UPDATE wg_history_imports SET state='queued' WHERE state='running'")
    while not g.closed:
        job = await db.fetchrow("""
            UPDATE wg_history_imports SET state='running',error='' WHERE source=(
                SELECT source FROM wg_history_imports
                WHERE state='queued' AND available<=$1 ORDER BY available,source LIMIT 1
            ) RETURNING *
        """, time.time())
        if not job:
            await asyncio.sleep(3)
            continue
        state, retry, error = "done", 0, ""
        try:
            await import_history(g, job)
        except errors.FloodWaitError as exc:
            state, retry = "queued", time.time() + max(1, exc.seconds)
            error = f"Telegram pidió una pausa de {exc.seconds} segundos. La copia se reanudará sola."
        except SESSION_ERRORS:
            state, error = "failed", "La sesión de Telegram venció. Vuelve a vincular la cuenta."
        except ImportPaused as exc:
            state, error = "failed", str(exc)[:200]
        except Exception as exc:
            LOG.warning("History import paused (%s)", type(exc).__name__)
            state, error = "failed", "No se pudo copiar el siguiente archivo. Pulsa Reanudar para volver a intentarlo."
        await db.execute("""
            UPDATE wg_history_imports SET state=$2,available=$3,error=$4 WHERE source=$1
        """, job["source"], state, retry, error)
