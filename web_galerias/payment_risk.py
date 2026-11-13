"""Private, durable CUBANITAS risk events; no other subscription changes.

Risk claims arrive ONLY through the existing Stripe-signed Web webhook. An
internal HMAC call asks CUSTOMER-SERVICE to recheck the real Stripe event and
resolve its original Web subscription. No email/account-guessing or Telegram.
"""
import asyncio
import logging
import os
import re
import time
from . import customer_bridge

log=logging.getLogger("web-galerias-payment-risk")

SCHEMA="""
CREATE TABLE IF NOT EXISTS wg_web_risk_events(
 event_id TEXT PRIMARY KEY,
 kind TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued',
 next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS wg_web_risk_due
 ON wg_web_risk_events(state,next_attempt);
CREATE TABLE IF NOT EXISTS wg_web_revocations(
 subscription TEXT PRIMARY KEY,
 reason TEXT NOT NULL,
 event_id TEXT NOT NULL,
 at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS wg_web_dispute_blocks(
 uid BIGINT PRIMARY KEY,
 event_id TEXT NOT NULL,
 at DOUBLE PRECISION NOT NULL
);
-- A full refund blocks this Web membership until a NEW invoice.paid after
-- the event, even if a newer subscription was already active at refund time.
CREATE TABLE IF NOT EXISTS wg_web_refund_blocks(
 uid BIGINT PRIMARY KEY,
 event_id TEXT NOT NULL,
 event_created DOUBLE PRECISION NOT NULL,
 at DOUBLE PRECISION NOT NULL
);
"""


async def stage_event(conn,kind,event_id,created):
    if kind not in ("charge.refunded","charge.dispute.created"):
        return
    await conn.execute("""
        INSERT INTO wg_web_risk_events(event_id,kind,created,next_attempt)
        VALUES($1,$2,$3,$4) ON CONFLICT(event_id) DO NOTHING
    """,event_id,kind,float(created or time.time()),time.time())


async def _process(g,row):
    event_id=str(row["event_id"])
    kind=str(row["kind"])
    db=g.db()
    try:
        checked=await customer_bridge.post_signed(
            "risk-web",{"event_id":event_id},timeout=14)
        if not isinstance(checked,dict):
            raise ValueError("Invalid Stripe risk response")
        if checked.get("verified") is not True:
            if checked.get("retry") is True:
                raise RuntimeError("Invoice or subscription mapping pending")
            await db.execute("UPDATE wg_web_risk_events SET state='done' WHERE event_id=$1",
                             event_id)
            return
        reason=checked.get("kind")
        if reason not in ("refund","dispute") or (
            reason=="refund" and kind!="charge.refunded" or
            reason=="dispute" and kind!="charge.dispute.created"):
            raise ValueError("Stripe event type mismatch")
        sub=checked.get("subscription")
        if not isinstance(sub,str) or not re.fullmatch(r"sub_[A-Za-z0-9_]+",sub):
            raise ValueError("Invalid verified Stripe subscription")
        # Only nonce-bound verified Web purchases may be revoked.
        verified=await db.fetchrow("""
            SELECT c.uid FROM wg_checkouts c
            JOIN wg_link_subscriptions p ON p.subscription=c.subscription
            WHERE c.subscription=$1 AND c.session IS NOT NULL
              AND p.price_id=$2 AND p.paid_until>0
            ORDER BY c.created DESC LIMIT 1
        """,sub,os.getenv("GALERIAS_STRIPE_PRICE_ID",""))
        if not verified:
            raise RuntimeError("Verified Stripe receipt not yet in Web database")
        uid=int(verified["uid"])
        when=time.time()
        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                   INSERT INTO wg_web_revocations(subscription,reason,event_id,at)
                   VALUES($1,$2,$3,$4)
                   ON CONFLICT(subscription) DO UPDATE SET
                     reason=excluded.reason,event_id=excluded.event_id,
                     at=excluded.at
                """,sub,reason,event_id,when)
                if reason=="dispute":
                    await conn.execute("""
                        INSERT INTO wg_web_dispute_blocks(uid,event_id,at)
                        VALUES($1,$2,$3) ON CONFLICT(uid) DO UPDATE
                        SET event_id=excluded.event_id,at=excluded.at
                    """,uid,event_id,when)
                elif reason=="refund":
                    # Mirror CUSTOMER-SERVICE refund_blocks: only a NEW paid
                    # invoice restores Web. Never let an earlier refund's
                    # delayed webhook re-block a later paid receipt.
                    event_created=int(checked.get("event_created") or 0)
                    if event_created<=0:
                        raise ValueError("Missing confirmed Stripe refund timestamp")
                    await conn.execute("""
                        INSERT INTO wg_web_refund_blocks(uid,event_id,event_created,at)
                        VALUES($1,$2,$3,$4) ON CONFLICT(uid) DO UPDATE SET
                          event_id=CASE WHEN EXCLUDED.event_created>
                               wg_web_refund_blocks.event_created
                               THEN EXCLUDED.event_id ELSE wg_web_refund_blocks.event_id END,
                          event_created=GREATEST(wg_web_refund_blocks.event_created,
                                                 EXCLUDED.event_created),
                          at=GREATEST(wg_web_refund_blocks.at,EXCLUDED.at)
                    """,uid,event_id,event_created,when)
                # Trigger existing signed CRM/NOTIFICACIONES sync promptly.
                # Paid Stripe receipts are not deleted or changed.
                await conn.execute("""
                    UPDATE wg_subscriptions SET changed=GREATEST(changed,$2)
                    WHERE uid=$1
                """,uid,when)
                await conn.execute("""
                    UPDATE wg_web_risk_events SET state='done',last_error=''
                    WHERE event_id=$1
                """,event_id)
        log.warning("CUBANITAS Web access revoked from verified Stripe %s event (%s)",
                    reason,event_id[-6:])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        wait=min(900,15*(2**min(int(row["attempts"] or 0),6)))
        await db.execute("""
            UPDATE wg_web_risk_events
               SET state='queued',attempts=attempts+1,
                   next_attempt=$2,last_error=$3
             WHERE event_id=$1
        """,event_id,time.time()+wait,type(exc).__name__)
        log.warning("CUBANITAS risk verification deferred (%s)",type(exc).__name__)


async def process_once(g):
    if not customer_bridge.ready():
        return
    db=g.db()
    for _ in range(5):
        row=await db.fetchrow("""
            WITH due AS (
                SELECT event_id FROM wg_web_risk_events
                WHERE state IN ('queued','running') AND next_attempt<=$1
                ORDER BY created ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE wg_web_risk_events r SET state='running',
                  next_attempt=$2
            FROM due WHERE r.event_id=due.event_id
            RETURNING r.event_id,r.kind,r.attempts
        """,time.time(),time.time()+90)
        if not row:
            break
        await _process(g,row)


async def run(g):
    while not g.closed:
        try:
            await process_once(g)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("CUBANITAS risk queue retry pending (%s)",type(exc).__name__)
        await asyncio.sleep(4)
