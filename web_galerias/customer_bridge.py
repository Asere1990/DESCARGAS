"""Low-priority authenticated sync from Web Galerías to CUSTOMER-SERVICE.

No changes to Stripe checkout, Telegram accounts, Gallery sync, or R2 backups.
The existing paid period and the original nonce-bound Telegram owner are the
only source of membership identity. Failed syncs are retried, never silently
converted to "paid".
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time

import httpx

log = logging.getLogger("cubanitas-bridge")
_AUTH_CACHE = {}


def ready():
    return len(os.getenv("CUBANITAS_BRIDGE_SECRET", "")) >= 48


def endpoint():
    url = os.getenv(
        "CUBANITAS_BRIDGE_URL",
        "https://customer-service-uimy.onrender.com/cubanitas/bridge",
    ).rstrip("/")
    if not url.startswith("https://") or not url.endswith("/cubanitas/bridge"):
        raise ValueError("Invalid internal CUSTOMER-SERVICE endpoint")
    return url


def signed_payload(data: dict):
    secret = os.getenv("CUBANITAS_BRIDGE_SECRET", "")
    if len(secret) < 48:
        raise RuntimeError("CUBANITAS bridge not configured")
    body = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), (stamp + ".").encode() + body, hashlib.sha256
    ).hexdigest()
    return body, {
        "Content-Type": "application/json",
        "X-Cubanitas-Time": stamp,
        "X-Cubanitas-Signature": signature,
    }


async def post_signed(operation: str, data: dict, *, timeout=6):
    if operation not in {"verify", "sync", "profile", "offers-link", "chat-link", "chat-unread", "cancel-renewal"}:
        raise ValueError("Unknown bridge operation")
    body, headers = signed_payload(data)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        response = await client.post(
            endpoint() + "/" + operation, content=body, headers=headers
        )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("Invalid signed bridge response")
    return value


async def verify_customer_bot(init_data: str, g) -> int:
    """Trust only CUSTOMER-SERVICE's server-side Telegram signature check."""
    if not ready():
        raise ValueError("Customer bot not integrated")
    key = hashlib.sha256(init_data.encode()).hexdigest()
    entry = _AUTH_CACHE.get(key)
    if entry and entry[1] > time.time():
        return entry[0]
    reply = await post_signed("verify", {"init_data": init_data}, timeout=4)
    uid = reply.get("uid")
    if type(uid) is not int or uid <= 0:
        raise ValueError("Customer bot identity not verified")
    if reply.get("blocked") is True:
        await g.db().execute(
            """INSERT INTO wg_access_blocks(uid,blocked,updated)
               VALUES($1,TRUE,$2)
               ON CONFLICT(uid) DO UPDATE SET blocked=TRUE,updated=$2""",
            uid, time.time()
        )
    if len(_AUTH_CACHE) > 400:
        _AUTH_CACHE.clear()
    _AUTH_CACHE[key] = (uid, time.time() + 45)
    return uid


async def sync_once(g):
    """At most 30 external records per cycle; completely outside user requests."""
    if not ready():
        return 0
    db = g.db()
    rows = await db.fetch(
        """SELECT s.uid,s.customer,s.subscription,s.state,s.paid_until,s.changed,
                  s.cancel_at_period_end,
                  c.session,c.created,COALESCE(u.name,'') AS telegram_name
           FROM wg_subscriptions s
           JOIN LATERAL (
              SELECT session,created FROM wg_checkouts
              WHERE subscription=s.subscription AND session IS NOT NULL
              ORDER BY created DESC LIMIT 1
           ) c ON TRUE
           LEFT JOIN wg_users u ON u.uid=s.uid
           LEFT JOIN wg_bridge_snapshots b ON b.subscription=s.subscription
           WHERE b.subscription IS NULL OR s.changed>COALESCE(b.last_change,0)
              OR COALESCE(b.last_checked,0)<$1
           ORDER BY COALESCE(b.last_checked,0) ASC
           LIMIT 30""", time.time() - 90,
    )
    ok = 0
    price = os.getenv("GALERIAS_STRIPE_PRICE_ID", "")
    for row in rows:
        if not str(row["subscription"]).startswith("sub_"):
            continue
        data = {
            "telegram_id": int(row["uid"]),
            "subscription_id": str(row["subscription"]),
            "customer_id": str(row["customer"]) if str(row["customer"] or "").startswith("cus_") else None,
            "checkout_session_id": str(row["session"]),
            "price_id": price,
            "state": str(row["state"]),
            "paid_until": int(row["paid_until"]),
            "cancel_at_period_end": bool(row["cancel_at_period_end"]),
            "first_paid_at": int(row["created"]),
            "source_changed": float(row["changed"]),
            "telegram_name": str(row["telegram_name"] or "")[:100],
        }
        try:
            response = await post_signed("sync", data, timeout=8)
            if response.get("ok") is not True or type(response.get("blocked")) is not bool:
                raise ValueError("Incomplete acknowledgement")
            async with db.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """INSERT INTO wg_access_blocks(uid,blocked,updated)
                           VALUES($1,$2,$3) ON CONFLICT(uid) DO UPDATE
                           SET blocked=excluded.blocked, updated=excluded.updated""",
                        row["uid"], response["blocked"], time.time()
                    )
                    await conn.execute(
                        """INSERT INTO wg_bridge_snapshots(subscription,last_change,last_checked)
                           VALUES($1,$2,$3) ON CONFLICT(subscription) DO UPDATE SET
                           last_change=GREATEST(wg_bridge_snapshots.last_change,excluded.last_change),
                           last_checked=excluded.last_checked""",
                        row["subscription"], float(row["changed"]), time.time()
                    )
            ok += 1
        except Exception as exc:
            # A failed optional mirror never denies a valid Stripe-paid user.
            log.warning("CUBANITAS status sync pending (%s)", type(exc).__name__)
            break
        await asyncio.sleep(0.05)
    return ok


async def run(g):
    while not g.closed:
        try:
            await sync_once(g)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("CUBANITAS mirror pending (%s)", type(exc).__name__)
        await asyncio.sleep(18)
