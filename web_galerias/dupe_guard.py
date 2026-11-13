"""CUBANITAS Web-only duplicate renewal prevention.

The existing nonce-bound paid checkouts are the ONLY source of Telegram
ownership; do not infer identity from email or customer Stripe ID.
No Telegram sessions, downloads, media catalog, or other memberships touched.
"""
import asyncio
import logging
import os
import time

from . import customer_bridge

log=logging.getLogger("web-galerias-web-dedupe")


SCHEMA="""
CREATE TABLE IF NOT EXISTS wg_web_dedupe_reviews(
 uid BIGINT PRIMARY KEY,
 source_signature TEXT NOT NULL,
 checked_at DOUBLE PRECISION NOT NULL
);
"""

async def dedupe_once(g):
    if not customer_bridge.ready():
        return 0
    db=g.db()
    now=time.time()
    price=os.getenv("GALERIAS_STRIPE_PRICE_ID","")
    # Same payment-linked Telegram ID, not email or Stripe Customer guess.
    # Canceled receipts are included to mirror CUSTOMER-SERVICE's most recent
    # valid intention. Failed/incomplete/unpaid/paused are not intentions.
    candidates=await db.fetch("""
      SELECT c.uid,
        ARRAY_AGG(DISTINCT p.subscription ORDER BY p.subscription) AS subscriptions,
        STRING_AGG(DISTINCT p.subscription||':'||p.status||':'||
                  p.cancel_at_period_end::text,';' ORDER BY
                  p.subscription||':'||p.status||':'||p.cancel_at_period_end::text)
                  AS signature
      FROM wg_checkouts c
      JOIN wg_link_subscriptions p ON p.subscription=c.subscription
      WHERE c.session IS NOT NULL AND p.price_id=$1
        AND p.status IN ('active','trialing','canceled')
        AND p.paid_until>0
      GROUP BY c.uid
      HAVING COUNT(DISTINCT p.subscription)>1
        AND COUNT(DISTINCT p.subscription)
           FILTER(WHERE p.status IN ('active','trialing')
                    AND NOT p.cancel_at_period_end)>0
      ORDER BY c.uid DESC LIMIT 16
    """,price)
    completed=0
    for row in candidates:
        uid=int(row["uid"])
        subs=sorted(set(row["subscriptions"] or []))
        signature=str(row["signature"] or "")
        if not 2<=len(subs)<=12:
            continue
        try:
            prior=await db.fetchrow("""
                SELECT source_signature,checked_at FROM wg_web_dedupe_reviews
                WHERE uid=$1
            """,uid)
            if prior and prior["source_signature"]==signature and (
                    float(prior["checked_at"])>now-900):
                continue
            response=await customer_bridge.post_signed(
                "dedupe-web",{"telegram_id":uid,"subscriptions":subs},
                timeout=18)
            kept=response.get("kept")
            intention=response.get("intent")
            older=response.get("older")
            if (kept is not None and kept not in subs
                or kept is None and intention!="canceled"
                or kept is not None and intention!="active"
                or not isinstance(older,list)
                or response.get("ok") is not True):
                raise ValueError("Stripe duplicate review incomplete")
            confirmed=[x["subscription"] for x in older
                       if isinstance(x,dict) and x.get("scheduled") is True
                       and isinstance(x.get("subscription"),str)]
            if (len(confirmed)!=len(older) or len(set(confirmed))!=len(confirmed)
                or not set(confirmed).issubset(set(subs)-({kept} if kept else set()))):
                raise ValueError("Stripe duplicate set invalid")
            for old in sorted(confirmed):
                await db.execute("""
                    UPDATE wg_link_subscriptions
                    SET cancel_at_period_end=TRUE,
                        changed=GREATEST(changed,$2)
                    WHERE subscription=$1 AND price_id=$3
                """,old,time.time(),price)
            if kept:
                # Only the latest actually paid, verified Web subscription
                # becomes canonical; a delayed webhook cannot restore old.
                await db.execute("""
                    UPDATE wg_subscriptions SET
                        customer=p.customer,subscription=p.subscription,
                        state=p.status,paid_until=p.paid_until,
                        cancel_at_period_end=p.cancel_at_period_end,
                        subscription_started_at=GREATEST(
                          wg_subscriptions.subscription_started_at,
                          p.subscription_started_at),
                        changed=GREATEST(wg_subscriptions.changed,$3)
                    FROM wg_link_subscriptions p
                    WHERE wg_subscriptions.uid=$1
                      AND p.subscription=$2 AND p.price_id=$4
                      AND p.status IN ('active','trialing') AND p.paid_until>$3
                      AND EXISTS(SELECT 1 FROM wg_checkouts c
                          WHERE c.uid=$1 AND c.subscription=$2
                            AND c.session IS NOT NULL)
                """,uid,kept,time.time(),price)
            elif confirmed:
                # CUSTOMER-SERVICE: last genuine intention was cancellation.
                # Do not revoke already paid access; stop renewals at expiry.
                await db.execute("""
                    UPDATE wg_subscriptions SET cancel_at_period_end=TRUE,
                       changed=GREATEST(changed,$2)
                    WHERE uid=$1 AND subscription=ANY($3::text[])
                """,uid,time.time(),confirmed)
            await db.execute("""
                INSERT INTO wg_web_dedupe_reviews(uid,source_signature,checked_at)
                VALUES($1,$2,$3)
                ON CONFLICT(uid) DO UPDATE SET
                 source_signature=excluded.source_signature,
                 checked_at=excluded.checked_at
            """,uid,signature,time.time())
            completed+=1
            log.info("Web duplicate review completed; older renewal count=%s",
                     len(confirmed))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Web duplicate review retry pending (%s)",type(exc).__name__)
        await asyncio.sleep(.05)
    return completed

async def run(g):
    while not g.closed:
        try:
            await dedupe_once(g)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Web duplicate monitor retry pending (%s)",type(exc).__name__)
        await asyncio.sleep(15)
