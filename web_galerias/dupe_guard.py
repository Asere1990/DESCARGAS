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


async def dedupe_once(g):
    if not customer_bridge.ready():
        return 0
    db=g.db()
    now=time.time()
    price=os.getenv("GALERIAS_STRIPE_PRICE_ID","")
    candidates=await db.fetch("""
      SELECT c.uid,ARRAY_AGG(DISTINCT p.subscription) AS subscriptions
      FROM wg_checkouts c
      JOIN wg_link_subscriptions p ON p.subscription=c.subscription
      WHERE c.session IS NOT NULL AND p.price_id=$2
        AND p.status IN ('active','trialing')
        AND p.paid_until>$1 AND NOT p.cancel_at_period_end
      GROUP BY c.uid HAVING COUNT(DISTINCT p.subscription)>1
      ORDER BY MAX(c.created) DESC
      LIMIT 12
    """,now,price)
    completed=0
    for row in candidates:
        uid=int(row["uid"])
        subs=sorted(set(row["subscriptions"]))
        if not 2<=len(subs)<=12:
            continue
        try:
            response=await customer_bridge.post_signed(
                "dedupe-web",{"telegram_id":uid,"subscriptions":subs},
                timeout=18)
            kept=response.get("kept")
            older=response.get("older")
            if (kept not in subs or not isinstance(older,list)
                or not isinstance(response.get("ok"),bool)):
                raise ValueError("Invalid Stripe duplicate review")
            confirmed={x.get("subscription") for x in older
                       if isinstance(x,dict) and x.get("scheduled") is True}
            expected=set(subs)-{kept}
            if not confirmed.issubset(expected):
                raise ValueError("Invalid Web subscription selection")
            # Stripe controls billing. Only after verified success do we
            # reflect the cancellation in the independent Web read model.
            for old in sorted(confirmed):
                await db.execute("""
                    UPDATE wg_link_subscriptions
                    SET cancel_at_period_end=TRUE,
                        changed=GREATEST(changed,$2)
                    WHERE subscription=$1 AND price_id=$3
                """,old,time.time(),price)
            # If the customer had two current receipts, only the latest
            # Stripe-created paid subscription may remain canonical.
            if response["ok"] and confirmed==expected:
                await db.execute("""
                    UPDATE wg_subscriptions SET
                        customer=p.customer,subscription=p.subscription,
                        state=p.status,paid_until=p.paid_until,
                        cancel_at_period_end=p.cancel_at_period_end,
                        changed=GREATEST(wg_subscriptions.changed,$3)
                    FROM wg_link_subscriptions p
                    WHERE wg_subscriptions.uid=$1
                      AND p.subscription=$2 AND p.price_id=$4
                      AND p.status IN ('active','trialing') AND p.paid_until>$3
                      AND EXISTS(SELECT 1 FROM wg_checkouts c
                          WHERE c.uid=$1 AND c.subscription=$2
                            AND c.session IS NOT NULL)
                """,uid,kept,time.time(),price)
                completed+=1
                log.warning("Web paid duplicate normalized uid=%s, other_renewals=%s",
                            uid,len(confirmed))
            else:
                log.warning("Web duplicate cancellation partially deferred uid=%s",
                            uid)
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
