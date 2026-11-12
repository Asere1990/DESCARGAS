"""Independent Web Galerías membership-entry and expiry notifications.

Only the isolated CLOUDFLARE process starts this worker. It reads the verified
$6.99 Web Galerías ledger; it never touches Telegram group membership,
DESCARGAS sessions, Stripe charges, R2 uploads or the $1.99 download add-on.

A persistent transition ledger and retry queue avoid duplicate notifications
from Stripe retries, periodic renewals and restarts.
"""
import asyncio
from datetime import datetime
from html import escape
import logging
import os
import re
import time
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger("web-galerias-notifications")
CHAT_ID = -1003208307392
MEMBERSHIP = "📸𝐆𝐀𝐋𝐄𝐑𝐈𝐀𝐒 𝐂𝐔𝐁𝐀𝐍𝐀𝐒®"
TZ = ZoneInfo("America/New_York")

SCHEMA = """
-- The first deployment timestamp prevents old purchases being announced as new.
CREATE TABLE IF NOT EXISTS wg_member_notice_rollout(
 id SMALLINT PRIMARY KEY CHECK (id=1),
 created DOUBLE PRECISION NOT NULL
);
INSERT INTO wg_member_notice_rollout(id,created)
 VALUES(1,EXTRACT(EPOCH FROM clock_timestamp()))
 ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS wg_member_notice_state(
 uid BIGINT PRIMARY KEY,
 subscription TEXT NOT NULL,
 active BOOLEAN NOT NULL,
 version BIGINT NOT NULL DEFAULT 0,
 observed DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS wg_member_notice_outbox(
 id BIGSERIAL PRIMARY KEY,
 uid BIGINT NOT NULL,
 version BIGINT NOT NULL,
 subscription TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('entry','expired','ended')),
 paid_until DOUBLE PRECISION NOT NULL,
 telegram_name TEXT NOT NULL DEFAULT '',
 created DOUBLE PRECISION NOT NULL,
 next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0,
 sent_at DOUBLE PRECISION,
 message_id BIGINT,
 last_error TEXT NOT NULL DEFAULT '',
 UNIQUE(uid,version)
);
CREATE INDEX IF NOT EXISTS wg_member_notice_outbox_due
 ON wg_member_notice_outbox(next_attempt,id) WHERE sent_at IS NULL;
ALTER TABLE wg_member_notice_rollout ENABLE ROW LEVEL SECURITY;
ALTER TABLE wg_member_notice_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE wg_member_notice_outbox ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON wg_member_notice_rollout,wg_member_notice_state,
 wg_member_notice_outbox FROM PUBLIC;
"""


def is_paid_active(status, paid_until, now):
    return status in ("active", "trialing") and float(paid_until or 0) > now


def notice_kind(previous, active, changed, rollout, paid_until, now):
    """A renewal does not create a new entry, and a cancellation is not expiry."""
    if previous is None:
        # Historical subscriptions are baseline, not a flood of old entries.
        return "entry" if active and changed >= rollout else None
    if bool(previous) == bool(active):
        return None
    if active:
        return "entry"
    return "expired" if paid_until <= now else "ended"


def format_message(uid, telegram_name, kind, paid_until):
    if kind not in ("entry", "expired", "ended") or int(uid) <= 0:
        raise ValueError("Invalid membership transition")
    uid = int(uid)
    display = escape(str(telegram_name or "").strip()[:100] or f"Usuario {uid}")
    person = f'<a href="tg://user?id={uid}"><u>{display}</u></a>'
    when = datetime.fromtimestamp(max(0, float(paid_until)), TZ).strftime("%d/%m/%Y · %I:%M %p ET")
    if kind == "entry":
        title = "🟢 <b>ENTRADA · MEMBRESÍA WEB</b>"
        status = "✅ Pago confirmado y acceso activado"
        date_caption = "Válida hasta"
    elif kind == "expired":
        title = "🔴 <b>SALIDA · MEMBRESÍA WEB VENCIDA</b>"
        status = "🔒 Período pagado vencido; acceso finalizado"
        date_caption = "Venció"
    else:
        title = "🔴 <b>SALIDA · ACCESO WEB FINALIZADO</b>"
        status = "🔒 La suscripción dejó de autorizar el acceso"
        date_caption = "Período registrado hasta"
    return (
        f"{title}\n\n"
        f"📸 <b>{MEMBERSHIP}</b>\n"
        f"👤 Usuario: {person}\n"
        f"🆔 Telegram ID: <code>{uid}</code>\n"
        f"📅 {date_caption}: {when}\n"
        f"{status}"
    )


async def stage(g, *, now=None):
    """Persist only real access transitions, atomically with their outbox rows."""
    now = time.time() if now is None else float(now)
    async with g.db().acquire() as conn:
        async with conn.transaction():
            # One process is deployed today; this also protects future replicas.
            if not await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock(7263470211::bigint)"
            ):
                return 0
            rollout = await conn.fetchval(
                "SELECT created FROM wg_member_notice_rollout WHERE id=1"
            )
            if rollout is None:
                raise RuntimeError("Membership notification schema not ready")
            # Read changes only. The user's gallery bootstrap never calls this.
            rows = await conn.fetch("""
               SELECT s.uid,s.subscription,s.state,s.paid_until,s.changed,
                      n.active AS prior_active,n.subscription AS prior_sub,
                      n.version,COALESCE(u.name,'') AS telegram_name
               FROM wg_subscriptions s
               LEFT JOIN wg_member_notice_state n ON n.uid=s.uid
               LEFT JOIN wg_users u ON u.uid=s.uid
               WHERE s.subscription IS NOT NULL AND s.subscription<>''
                 AND (n.uid IS NULL OR n.active IS DISTINCT FROM
                     (s.state IN ('active','trialing') AND s.paid_until>$1)
                   OR n.subscription IS DISTINCT FROM s.subscription)
               ORDER BY s.uid LIMIT 200
            """, now)
            added = 0
            for row in rows:
                uid = int(row["uid"])
                if uid <= 0:
                    continue
                sub = str(row["subscription"])
                active = is_paid_active(row["state"], row["paid_until"], now)
                prior = row["prior_active"]
                version = int(row["version"] or 0)
                kind = notice_kind(prior, active, float(row["changed"] or 0),
                                   float(rollout), float(row["paid_until"]), now)
                if prior is None:
                    version = 1 if kind else 0
                    await conn.execute("""
                       INSERT INTO wg_member_notice_state
                         (uid,subscription,active,version,observed)
                       VALUES($1,$2,$3,$4,$5) ON CONFLICT(uid) DO NOTHING
                    """, uid, sub, active, version, now)
                else:
                    version += 1 if kind else 0
                    await conn.execute("""
                       UPDATE wg_member_notice_state
                          SET subscription=$2,active=$3,version=$4,observed=$5
                        WHERE uid=$1
                    """, uid, sub, active, version, now)
                if kind:
                    await conn.execute("""
                        INSERT INTO wg_member_notice_outbox
                          (uid,version,subscription,kind,paid_until,
                           telegram_name,created)
                        VALUES($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT(uid,version) DO NOTHING
                    """, uid, version, sub, kind, float(row["paid_until"]),
                         str(row["telegram_name"] or "")[:100], now)
                    added += 1
            return added


def token_for(g):
    token = (os.getenv("GALERIAS_NOTIFICATIONS_BOT_TOKEN") or
             getattr(getattr(g, "s", None), "token", "") or "").strip()
    return token if re.fullmatch(r"[0-9]{5,15}:[A-Za-z0-9_-]{30,}", token) else None


async def probe(g):
    """Check group accessibility without posting an unsolicited test message."""
    token = token_for(g)
    if not token:
        log.warning("Membership notifications unavailable: no valid bot token")
        return False
    url = f"https://api.telegram.org/bot{token}/getChat"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as client:
            async with client.get(url, params={"chat_id": CHAT_ID}) as response:
                payload = await response.json(content_type=None)
                good = response.status == 200 and payload.get("ok") is True
                if not good:
                    log.warning("Membership notification group inaccessible (HTTP %s)",
                                response.status)
                else:
                    log.warning("Membership notification group reachable")
                return good
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
        log.warning("Membership notification group check pending (%s)",
                    type(exc).__name__)
        return False


async def deliver(g, *, now=None):
    """Send only queued verified transitions, recording success in PostgreSQL."""
    token = token_for(g)
    if not token:
        return 0
    now = time.time() if now is None else float(now)
    rows = await g.db().fetch("""
        SELECT id,uid,kind,telegram_name,paid_until,attempts
          FROM wg_member_notice_outbox
         WHERE sent_at IS NULL AND next_attempt<=$1
         ORDER BY id LIMIT 8
    """, now)
    if not rows:
        return 0
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    sent = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
        for row in rows:
            text = format_message(row["uid"], row["telegram_name"],
                                  row["kind"], row["paid_until"])
            error = ""
            retry_after = 0
            msg_id = None
            try:
                async with client.post(url, json={
                    "chat_id": CHAT_ID, "text": text,
                    "parse_mode": "HTML", "disable_web_page_preview": True
                }) as response:
                    body = await response.json(content_type=None)
                    if response.status == 200 and body.get("ok") is True:
                        msg_id = int((body.get("result") or {}).get("message_id") or 0)
                    else:
                        error = f"Telegram HTTP {response.status}"
                        retry_after = int((body.get("parameters") or {}).get(
                            "retry_after") or 0)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError,
                    KeyError, TypeError) as exc:
                error = type(exc).__name__
            if msg_id:
                await g.db().execute("""
                    UPDATE wg_member_notice_outbox
                       SET sent_at=$2,message_id=$3,last_error=''
                     WHERE id=$1 AND sent_at IS NULL
                """, row["id"], time.time(), msg_id)
                sent += 1
            else:
                attempts = int(row["attempts"] or 0) + 1
                delay = max(retry_after, min(600, 10 * (2 ** min(attempts, 6))))
                await g.db().execute("""
                    UPDATE wg_member_notice_outbox
                       SET attempts=$2,next_attempt=$3,last_error=$4
                     WHERE id=$1 AND sent_at IS NULL
                """, row["id"], attempts, time.time()+delay, error[:120])
                if attempts == 1 or attempts % 6 == 0:
                    log.warning("Membership alert queued for retry (%s)", error)
    return sent


async def run(g):
    # Never block users, group membership, checkout responses, or gallery sync.
    await probe(g)
    while not g.closed:
        try:
            await stage(g)
            await deliver(g)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Membership notification cycle pending (%s)",
                        type(exc).__name__)
        await asyncio.sleep(25)
