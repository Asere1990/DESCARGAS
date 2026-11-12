"""Isolated daily gallery download tiers and $1.99 recurring Plus entitlement.

Only the CLOUDFLARE Web Galerías database is changed. Never opens Telegram
sessions, updates DESCARGAS records or alters the existing $6.99 subscription.
The paid Plus entitlement requires a signed Stripe webhook and a matching,
nonce-bound checkout + independently paid invoice.
"""
import os
import re
import secrets
import time
from urllib.parse import urlsplit,urlunsplit,urlencode
from aiohttp import web

BASE_LIMIT=50
LINKED_LIMIT=200
PLUS_AMOUNT=199
SCHEMA="""
CREATE TABLE IF NOT EXISTS wg_plus_checkouts(
 nonce TEXT PRIMARY KEY,uid BIGINT NOT NULL,created DOUBLE PRECISION NOT NULL,
 session TEXT UNIQUE,subscription TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS wg_plus_checkouts_uid ON wg_plus_checkouts(uid,created DESC);
CREATE TABLE IF NOT EXISTS wg_plus_subscriptions(
 subscription TEXT PRIMARY KEY,customer TEXT NOT NULL DEFAULT '',
 price_id TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'incomplete',
 period_end DOUBLE PRECISION NOT NULL DEFAULT 0,
 paid_until DOUBLE PRECISION NOT NULL DEFAULT 0,
 changed DOUBLE PRECISION NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS wg_plus_invoices(
 invoice TEXT PRIMARY KEY,subscription TEXT NOT NULL,
 payment_intent TEXT,charge TEXT);
CREATE INDEX IF NOT EXISTS wg_plus_invoices_subscription ON wg_plus_invoices(subscription);
"""

def configured():
    link=os.getenv("GALERIAS_PLUS_URL","").strip()
    parsed=urlsplit(link)
    return bool(
        parsed.scheme=="https" and parsed.hostname=="buy.stripe.com"
        and bool(parsed.path.strip("/")) and not parsed.query
        and not parsed.fragment and not parsed.username and not parsed.password
        and re.fullmatch(r"plink_[A-Za-z0-9]+",os.getenv("GALERIAS_PLUS_LINK",""))
        and re.fullmatch(r"price_[A-Za-z0-9]+",os.getenv("GALERIAS_PLUS_PRICE",""))
        and os.getenv("GALERIAS_STRIPE_WEBHOOK_SECRET","").startswith("whsec_")
    )

async def linked_descargas(g,uid):
    """Confirm the *user's own* saved DESCARGAS session, never DATOS or a creator."""
    pool=getattr(getattr(getattr(g,"s",None),"repo",None),"pool",None)
    if pool is None:return False
    try:
        found=await pool.fetchval(
            "SELECT 1 FROM descarga_accounts WHERE uid=$1 AND session IS NOT NULL",uid)
        return bool(found)
    except Exception:
        # A transient source database error never grants a higher allowance.
        return False

async def paid_plus(g,uid,*,now=None):
    if not configured():return False
    now=time.time() if now is None else now
    return bool(await g.db().fetchval("""
        SELECT EXISTS(
          SELECT 1 FROM wg_plus_checkouts c
          JOIN wg_plus_subscriptions s ON s.subscription=c.subscription
          WHERE c.uid=$1 AND c.session IS NOT NULL
            AND s.price_id=$3 AND s.status IN ('active','trialing')
            AND s.paid_until>$2
        )""",uid,now,os.environ["GALERIAS_PLUS_PRICE"]))

async def tier(g,uid):
    # Preserve explicit pre-existing free grants, even when Stripe is unavailable.
    free=bool(await g.free_granted(uid))
    plus=False if free else await paid_plus(g,uid)
    if free or plus:
        return {"daily_limit":None,"tier":"unlimited",
                "quota_unlimited":True,"plus_active":plus,
                "linked_descargas":False}
    linked=await linked_descargas(g,uid)
    return {"daily_limit":LINKED_LIMIT if linked else BASE_LIMIT,
            "tier":"linked" if linked else "basic",
            "quota_unlimited":False,"plus_active":False,
            "linked_descargas":linked}

async def checkout(g,uid):
    if not await g.entitled(uid):
        raise web.HTTPForbidden(text="Necesitas una membresía Web activa.")
    if not configured():
        raise web.HTTPServiceUnavailable(text="El extra ilimitado todavía no está preparado.")
    details=await tier(g,uid)
    if details["quota_unlimited"]:
        return {"allowed":True,"quota_unlimited":True}
    nonce=secrets.token_urlsafe(27)
    await g.db().execute(
        "INSERT INTO wg_plus_checkouts(nonce,uid,created) VALUES($1,$2,$3)",
        nonce,uid,time.time())
    parsed=urlsplit(os.environ["GALERIAS_PLUS_URL"])
    return {"url":urlunsplit((parsed.scheme,parsed.netloc,parsed.path,
                             urlencode({"client_reference_id":nonce}),""))}

async def apply_webhook(conn,kind,obj,event_time):
    """Called only inside the existing, signature-verified Stripe transaction.

    The primary $6.99 handler remains responsible for its own subscriptions;
    these separate tables cannot modify main membership authorization.
    """
    if not configured():return
    from .payment_link_checkout import (subscription_id_from_invoice,
        subscription_period,invoice_paid_period)
    price=os.environ["GALERIAS_PLUS_PRICE"]
    link=os.getenv("GALERIAS_PLUS_LINK","")
    sub=None
    if kind in ("checkout.session.completed","checkout.session.async_payment_succeeded"):
        if (obj.get("payment_link")!=link
            or obj.get("mode")!="subscription"
            or obj.get("payment_status")!="paid"
            or str(obj.get("currency") or "").lower()!="usd"
            or int(obj.get("amount_subtotal") or obj.get("amount_total") or 0)!=PLUS_AMOUNT):
            return
        sub=obj.get("subscription")
        session=obj.get("id")
        nonce=obj.get("client_reference_id")
        if (not isinstance(sub,str) or not sub.startswith("sub_")
            or not isinstance(session,str) or not session.startswith("cs_")
            or not isinstance(nonce,str) or len(nonce)<20):
            return
        owner=await conn.fetchrow("""
            SELECT uid FROM wg_plus_checkouts WHERE nonce=$1 AND created>$2
            AND (session IS NULL OR session=$3) FOR UPDATE""",
            nonce,time.time()-86400,session)
        if not owner:return
        await conn.execute("""UPDATE wg_plus_checkouts
            SET session=$2,subscription=$3 WHERE nonce=$1
            AND (session IS NULL OR session=$2)
            AND (subscription IS NULL OR subscription=$3)""",nonce,session,sub)
    elif kind=="invoice.paid":
        sub=subscription_id_from_invoice(obj)
        if not sub:return
        end=invoice_paid_period(obj,price)
        if end<=time.time():return
        await conn.execute("""INSERT INTO wg_plus_subscriptions(
            subscription,customer,price_id,status,period_end,paid_until,changed)
            VALUES($1,$2,$3,'active',$4,$4,$5)
            ON CONFLICT(subscription) DO UPDATE SET
             customer=CASE WHEN wg_plus_subscriptions.customer=''
               THEN excluded.customer ELSE wg_plus_subscriptions.customer END,
             price_id=$3,
             period_end=GREATEST(wg_plus_subscriptions.period_end,$4),
             paid_until=GREATEST(wg_plus_subscriptions.paid_until,$4),
             status=CASE WHEN wg_plus_subscriptions.changed<=$5
               THEN 'active' ELSE wg_plus_subscriptions.status END,
             changed=GREATEST(wg_plus_subscriptions.changed,$5)""",
            sub,str(obj.get("customer") or ""),price,end,event_time)
        invoice=obj.get("id")
        if isinstance(invoice,str) and invoice.startswith("in_"):
            payment_intent=obj.get("payment_intent")
            charge=obj.get("charge")
            await conn.execute("""INSERT INTO wg_plus_invoices(
                invoice,subscription,payment_intent,charge)
                VALUES($1,$2,$3,$4)
                ON CONFLICT(invoice) DO UPDATE SET subscription=excluded.subscription,
                    payment_intent=COALESCE(excluded.payment_intent,wg_plus_invoices.payment_intent),
                    charge=COALESCE(excluded.charge,wg_plus_invoices.charge)""",
                invoice,sub,
                payment_intent if isinstance(payment_intent,str) else None,
                charge if isinstance(charge,str) else None)
    elif kind in ("customer.subscription.created",
                "customer.subscription.updated",
                "customer.subscription.deleted"):
        sub=obj.get("id")
        if not isinstance(sub,str) or not sub.startswith("sub_"):return
        end=subscription_period(obj,price)
        tracked=await conn.fetchval(
            "SELECT 1 FROM wg_plus_subscriptions WHERE subscription=$1 AND price_id=$2",
            sub,price)
        if end is None and not tracked:return
        state="canceled" if kind.endswith(".deleted") else str(obj.get("status") or "incomplete")
        await conn.execute("""INSERT INTO wg_plus_subscriptions(
           subscription,customer,price_id,status,period_end,changed)
           VALUES($1,$2,$3,$4,$5,$6)
           ON CONFLICT(subscription) DO UPDATE SET
            customer=excluded.customer,price_id=$3,
            period_end=GREATEST(wg_plus_subscriptions.period_end,$5),
            status=CASE WHEN wg_plus_subscriptions.changed<=$6
              THEN excluded.status ELSE wg_plus_subscriptions.status END,
            changed=GREATEST(wg_plus_subscriptions.changed,$6)""",
            sub,str(obj.get("customer") or ""),price,state,int(end or 0),event_time)
    elif kind=="invoice.payment_failed":
        sub=subscription_id_from_invoice(obj)
        if sub:
            await conn.execute("""UPDATE wg_plus_subscriptions
                SET status='past_due',changed=GREATEST(changed,$2)
                WHERE subscription=$1 AND price_id=$3 AND changed<=$2""",
                sub,event_time,price)
    elif kind in ("charge.refunded","charge.dispute.created"):
        # Fail closed for traceable charges only. Never change another product.
        charge=(obj.get("charge") if kind=="charge.dispute.created" else obj.get("id"))
        invoice=obj.get("invoice")
        intent=obj.get("payment_intent")
        if kind=="charge.refunded" and not (
            obj.get("refunded") is True or int(obj.get("amount_refunded") or 0)>0
        ):return
        await conn.execute("""UPDATE wg_plus_subscriptions s
          SET status='unpaid',changed=GREATEST(s.changed,$4)
          FROM wg_plus_invoices i
          WHERE i.subscription=s.subscription AND s.price_id=$5
          AND (i.invoice=$1 OR ($2::text IS NOT NULL AND i.charge=$2)
             OR ($3::text IS NOT NULL AND i.payment_intent=$3))""",
          invoice if isinstance(invoice,str) else None,
          charge if isinstance(charge,str) else None,
          intent if isinstance(intent,str) else None,event_time,price)
