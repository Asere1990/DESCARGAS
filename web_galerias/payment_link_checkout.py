"""Web Galerías Stripe Payment Link checkout (no Stripe secret API key in this service).

The payment link is a live Stripe-hosted Checkout for the operator's existing
$6.99/month subscription product. A random, one-time client reference joins the
signed Stripe event to the Telegram ID. Only paid invoice periods grant access.
Webhook events can arrive in any order; their information is reconciled in DB.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit, urlunsplit
from aiohttp import web


def configured():
    url=os.getenv("GALERIAS_STRIPE_PAYMENT_LINK_URL","").strip()
    payment_link=os.getenv("GALERIAS_STRIPE_PAYMENT_LINK_ID","").strip()
    price=os.getenv("GALERIAS_STRIPE_PRICE_ID","").strip()
    webhook=os.getenv("GALERIAS_STRIPE_WEBHOOK_SECRET","").strip()
    parsed=urlsplit(url)
    return (parsed.scheme=="https" and parsed.hostname=="buy.stripe.com"
            and not parsed.username and not parsed.password
            and bool(parsed.path.strip("/")) and not parsed.fragment
            and payment_link.startswith("plink_") and len(payment_link)>10
            and price.startswith("price_") and len(price)>10
            and webhook.startswith("whsec_") and len(webhook)>12)


async def checkout(g,uid):
    """Only called on the Buy button. Never charges or activates on page load."""
    if not configured():
        raise web.HTTPServiceUnavailable(text="El enlace de Stripe no está preparado.")
    url=urlsplit(os.environ["GALERIAS_STRIPE_PAYMENT_LINK_URL"].strip())
    if url.query:
        # Don't use arbitrary pre-existing parameters from pasted checkout URL.
        raise web.HTTPServiceUnavailable(text="Enlace Stripe inesperado.")
    nonce=secrets.token_urlsafe(27)
    await g.db().execute(
        "INSERT INTO wg_checkouts(nonce,uid,created) VALUES($1,$2,$3)",
        nonce,uid,time.time())
    return {"url":urlunsplit((url.scheme,url.netloc,url.path,
                             urlencode({"client_reference_id":nonce}),""))}


def price_matches(line,price_id):
    """Handle Stripe API's old price and newer invoice line pricing formats."""
    price=line.get("price")
    if isinstance(price,str) and price==price_id:return True
    if isinstance(price,dict) and price.get("id")==price_id:return True
    pricing=line.get("pricing") or {}
    details=pricing.get("price_details") or {}
    if details.get("price")==price_id:return True
    parent=line.get("parent") or {}
    sub_item=parent.get("subscription_item_details") or {}
    embedded=sub_item.get("price") or {}
    if embedded==price_id:return True
    return isinstance(embedded,dict) and embedded.get("id")==price_id


def subscription_id_from_invoice(obj):
    value=obj.get("subscription")
    if isinstance(value,str) and value.startswith("sub_"):return value
    parent=obj.get("parent") or {}
    detail=parent.get("subscription_details") or {}
    value=detail.get("subscription")
    if isinstance(value,str) and value.startswith("sub_"):return value
    for line in (obj.get("lines") or {}).get("data",[]):
        node=(line.get("parent") or {}).get("subscription_item_details") or {}
        value=node.get("subscription")
        if isinstance(value,str) and value.startswith("sub_"):return value
    return None


def invoice_paid_period(obj,price):
    if obj.get("paid") is False or obj.get("status") not in ("paid",None):
        return 0
    end=0
    for line in (obj.get("lines") or {}).get("data",[]):
        if price_matches(line,price):
            end=max(end,int((line.get("period") or {}).get("end") or 0))
    return end


def subscription_period(obj,price):
    rows=(obj.get("items") or {}).get("data",[])
    valid=any(price_matches(item,price) for item in rows)
    if not valid:return None
    until=max([int(obj.get("current_period_end") or 0)]+[
        int(item.get("current_period_end") or 0) for item in rows])
    return until


async def stage_subscription(connection,obj,event_time):
    sub_id=obj.get("id","")
    price=os.environ["GALERIAS_STRIPE_PRICE_ID"]
    period=subscription_period(obj,price)
    if not str(sub_id).startswith("sub_") or period is None:return None
    state=str(obj.get("status") or "incomplete")
    scheduled=bool(obj.get("cancel_at_period_end",False))
    started=max(0,int(obj.get("created") or 0))
    await connection.execute("""INSERT INTO wg_link_subscriptions(
          subscription,customer,price_id,status,period_end,changed,cancel_at_period_end,
          subscription_started_at)
       VALUES($1,$2,$3,$4,$5,$6,$7,$8)
       ON CONFLICT(subscription) DO UPDATE SET
        customer=excluded.customer,price_id=excluded.price_id,
        period_end=GREATEST(wg_link_subscriptions.period_end,excluded.period_end),
        status=CASE WHEN wg_link_subscriptions.changed<=$6
           THEN excluded.status ELSE wg_link_subscriptions.status END,
        cancel_at_period_end=CASE WHEN wg_link_subscriptions.changed<=$6
           THEN excluded.cancel_at_period_end ELSE wg_link_subscriptions.cancel_at_period_end END,
        subscription_started_at=GREATEST(wg_link_subscriptions.subscription_started_at,$8),
        changed=GREATEST(wg_link_subscriptions.changed,$6)""",
        sub_id,str(obj.get("customer") or ""),price,state,period,event_time,
        scheduled,started)
    return sub_id


async def stage_paid_invoice(connection,obj,event_time=None):
    sub_id=subscription_id_from_invoice(obj)
    if not sub_id:return None
    price=os.environ["GALERIAS_STRIPE_PRICE_ID"]
    until=invoice_paid_period(obj,price)
    if until<=time.time():return None
    customer=str(obj.get("customer") or "")
    paid_at=int(event_time or (obj.get("status_transitions") or {}).get("paid_at")
                or obj.get("created") or 0)
    await connection.execute("""INSERT INTO wg_link_subscriptions(
         subscription,customer,price_id,status,period_end,paid_until,changed,
         last_invoice_paid_at)
       VALUES($1,$2,$3,'active',$4,$4,0,$5)
       ON CONFLICT(subscription) DO UPDATE SET
        paid_until=GREATEST(wg_link_subscriptions.paid_until,$4),
        period_end=GREATEST(wg_link_subscriptions.period_end,$4),
        last_invoice_paid_at=GREATEST(
             wg_link_subscriptions.last_invoice_paid_at,EXCLUDED.last_invoice_paid_at),
        customer=CASE WHEN wg_link_subscriptions.customer=''
          THEN excluded.customer ELSE wg_link_subscriptions.customer END,
        price_id=$3,
        status=CASE WHEN wg_link_subscriptions.status IN ('canceled','unpaid')
          THEN wg_link_subscriptions.status ELSE 'active' END""",
        sub_id,customer,price,until,paid_at)
    return sub_id


async def bind_paid_checkout(connection,obj):
    if (obj.get("payment_link")!=os.environ["GALERIAS_STRIPE_PAYMENT_LINK_ID"]
        or obj.get("mode")!="subscription"
        or obj.get("payment_status")!="paid"
        or str(obj.get("currency") or "").lower()!="usd"
        or int(obj.get("amount_total") or 0)!=699):
        return None
    sub_id=obj.get("subscription")
    nonce=obj.get("client_reference_id")
    session=obj.get("id")
    if not (isinstance(sub_id,str) and sub_id.startswith("sub_")
            and isinstance(nonce,str) and len(nonce)>=20
            and isinstance(session,str) and session.startswith("cs_")):
        return None
    owner=await connection.fetchrow("""SELECT uid FROM wg_checkouts
       WHERE nonce=$1 AND created>$2 AND
         (session IS NULL OR session=$3) FOR UPDATE""",
        nonce,time.time()-86400,session)
    if not owner:return None
    await connection.execute("""UPDATE wg_checkouts
       SET session=$2,subscription=$3
       WHERE nonce=$1 AND (session IS NULL OR session=$2)""",
       nonce,session,sub_id)
    return sub_id


async def reconcile_access(connection,sub_id):
    if not sub_id:return
    data=await connection.fetchrow("""SELECT c.uid,p.customer,p.subscription,
        p.status,p.price_id,p.paid_until,p.cancel_at_period_end,
        GREATEST(p.subscription_started_at,c.created) AS started_at
      FROM wg_checkouts c JOIN wg_link_subscriptions p
        ON p.subscription=c.subscription
      WHERE c.subscription=$1 AND c.session IS NOT NULL
      ORDER BY c.created DESC LIMIT 1""",sub_id)
    if not data:return
    if (data["price_id"]!=os.environ["GALERIAS_STRIPE_PRICE_ID"]
        or data["paid_until"]<=time.time()):
        return
    state=data["status"]
    if state not in ("active","trialing"):
        # A valid paid period never restores a canceled/failed subscription.
        await connection.execute("""UPDATE wg_subscriptions
           SET state=$2,changed=$3 WHERE subscription=$1""",
           sub_id,state,time.time())
        return
    await connection.execute("""INSERT INTO wg_subscriptions(
        uid,customer,subscription,state,paid_until,changed,
        cancel_at_period_end,subscription_started_at)
      VALUES($1,$2,$3,$4,$5,$6,$7,$8)
      ON CONFLICT(uid) DO UPDATE SET
       customer=excluded.customer,
       subscription=excluded.subscription,
       state=excluded.state,
       paid_until=CASE WHEN wg_subscriptions.subscription=excluded.subscription
         THEN GREATEST(wg_subscriptions.paid_until,excluded.paid_until)
         ELSE excluded.paid_until END,
       cancel_at_period_end=excluded.cancel_at_period_end,
       subscription_started_at=GREATEST(wg_subscriptions.subscription_started_at,$8),
       changed=excluded.changed
      WHERE excluded.subscription_started_at>=wg_subscriptions.subscription_started_at
        OR wg_subscriptions.subscription=excluded.subscription""",
       data["uid"],data["customer"],sub_id,state,data["paid_until"],
       time.time(),bool(dict(data).get("cancel_at_period_end",False)),
       float(dict(data).get("started_at") or time.time()))



async def recover_independently_verified_purchase(g):
    """One-time, admin-controlled repair for a verified paid receipt.

    The payment was independently checked against Stripe's LIVE account:
    completed Checkout with exact payment link + $6.99, paid invoice
    with exact Price ID, active subscription with current paid period.
    There is NO new Stripe charge. The existing random checkout reference
    must match a nonce already issued by this installation to this UID.

    A one-use receipt is delivered via CLOUDFLARE service environment only,
    never through an unauthenticated web route, and is audited in SQL.
    """
    raw=os.getenv("GALERIAS_VERIFIED_PAID_RECOVERY","").strip()
    if not raw:return None
    try:
        receipt=json.loads(raw)
        if not isinstance(receipt,dict):raise ValueError("receipt type")
        uid=receipt.get("uid")
        nonce=receipt.get("nonce")
        session=receipt.get("session")
        subscription=receipt.get("subscription")
        customer=receipt.get("customer")
        invoice=receipt.get("invoice")
        expiry=receipt.get("paid_until")
        price=receipt.get("price_id")
        if (type(uid) is not int or uid<=0 or
            str(uid) not in {v.strip() for v in
                os.getenv("GALERIAS_PAYMENT_TEST_UIDS","").split(",")} or
            not isinstance(nonce,str) or len(nonce)<25 or len(nonce)>80 or
            not isinstance(session,str) or not session.startswith("cs_live_") or
            not isinstance(subscription,str) or not subscription.startswith("sub_") or
            not isinstance(customer,str) or not customer.startswith("cus_") or
            not isinstance(invoice,str) or not invoice.startswith("in_") or
            price!=os.getenv("GALERIAS_STRIPE_PRICE_ID") or
            receipt.get("payment_link")!=os.getenv("GALERIAS_STRIPE_PAYMENT_LINK_ID") or
            receipt.get("currency")!="usd" or receipt.get("amount_paid")!=699 or
            receipt.get("livemode") is not True or
            type(expiry) is not int or not time.time()<expiry<time.time()+40*86400):
            raise ValueError("invalid or expired verified receipt")
    except (TypeError,ValueError):
        raise ValueError("Invalid independent paid-recovery receipt") from None

    db=g.db()
    audit="gallery_paid_verified_recovery:"+subscription
    async with db.acquire() as conn:
        async with conn.transaction():
            if await conn.fetchval(
                "SELECT 1 FROM wg_activity WHERE actor=$1 AND operation=$2",
                uid,audit):
                return "already_reconciled"
            checkout=await conn.fetchrow("""SELECT uid,session,subscription,created
                FROM wg_checkouts WHERE nonce=$1 AND uid=$2 FOR UPDATE""",nonce,uid)
            if (not checkout or
                checkout["uid"]!=uid or
                checkout["session"] not in (None,session) or
                checkout["subscription"] not in (None,subscription) or
                not time.time()-8*86400<float(checkout["created"])<=time.time()+60):
                return "original_checkout_not_found"

            # This record is evidence of the already-paid period, not a
            # synthetic Stripe event and not a new purchase.
            await conn.execute("""INSERT INTO wg_link_subscriptions(
                 subscription,customer,price_id,status,period_end,paid_until,changed)
               VALUES($1,$2,$3,'active',$4,$4,$5)
               ON CONFLICT(subscription) DO UPDATE SET
                 customer=excluded.customer,price_id=excluded.price_id,
                 paid_until=GREATEST(wg_link_subscriptions.paid_until,excluded.paid_until),
                 period_end=GREATEST(wg_link_subscriptions.period_end,excluded.period_end),
                 status=CASE WHEN wg_link_subscriptions.status IN
                    ('canceled','unpaid') THEN wg_link_subscriptions.status
                    ELSE 'active' END""",
                 subscription,customer,price,expiry,time.time())
            await conn.execute("""UPDATE wg_checkouts
                  SET session=$2,subscription=$3
                  WHERE nonce=$1 AND uid=$4
                    AND (session IS NULL OR session=$2)
                    AND (subscription IS NULL OR subscription=$3)""",
                  nonce,session,subscription,uid)
            await reconcile_access(conn,subscription)
            active=await conn.fetchval("""SELECT 1 FROM wg_subscriptions
                 WHERE uid=$1 AND subscription=$2 AND state='active'
                   AND paid_until>$3""",uid,subscription,time.time())
            if not active:
                raise ValueError("Paid receipt did not activate access")
            await conn.execute("""INSERT INTO wg_activity(actor,operation,at)
                VALUES($1,$2,$3)""",uid,audit,time.time())
    return "reconciled"




async def recover_missing_paid_access(g, uid=None, actor=None):
    """Repair only missing access backed by a verified, bound, current invoice.

    Do not overwrite any existing access row, grant a free month, alter Stripe,
    or clear an administrative/risk block. An atomic INSERT is idempotent even
    if two workers or a manual command run together.
    """
    if not configured():
        return []
    now=time.time()
    price=os.environ["GALERIAS_STRIPE_PRICE_ID"]
    async with g.db().acquire() as conn:
        async with conn.transaction():
            rows=await conn.fetch("""
                WITH candidates AS (
                    SELECT DISTINCT ON (c.uid) c.uid,p.customer,p.subscription,
                        p.status,p.paid_until,p.cancel_at_period_end,
                        GREATEST(p.subscription_started_at,c.created) AS started_at
                    FROM wg_checkouts c
                    JOIN wg_link_subscriptions p ON p.subscription=c.subscription
                    LEFT JOIN wg_subscriptions s ON s.uid=c.uid
                    WHERE ($3::bigint IS NULL OR c.uid=$3)
                      AND left(c.session,8)='cs_live_'
                      AND p.price_id=$1 AND p.status IN ('active','trialing')
                      AND p.paid_until>$2 AND p.last_invoice_paid_at>0
                      AND left(p.customer,4)='cus_' AND s.uid IS NULL
                      AND NOT EXISTS (SELECT 1 FROM wg_web_dispute_blocks d
                                      WHERE d.uid=c.uid)
                      AND NOT EXISTS (SELECT 1 FROM wg_access_blocks b
                                      WHERE b.uid=c.uid AND b.blocked)
                      AND NOT EXISTS (SELECT 1 FROM wg_web_refund_blocks f
                          WHERE f.uid=c.uid AND f.event_created>=p.last_invoice_paid_at)
                    ORDER BY c.uid,GREATEST(p.subscription_started_at,c.created) DESC,
                        p.last_invoice_paid_at DESC,p.subscription DESC
                    LIMIT $4
                )
                INSERT INTO wg_subscriptions(uid,customer,subscription,state,
                    paid_until,changed,cancel_at_period_end,subscription_started_at)
                SELECT uid,customer,subscription,status,paid_until,$2,
                    cancel_at_period_end,started_at FROM candidates
                ON CONFLICT DO NOTHING
                RETURNING uid,subscription,paid_until
            """,price,now,uid,1 if uid is not None else 30)
            for row in rows:
                await conn.execute("""
                    INSERT INTO wg_activity(actor,operation,at)
                    VALUES($1,$2,$3)
                """,actor if actor is not None else row["uid"],
                    ("gallery_paid_access_manual:" if actor is not None else
                     "gallery_paid_access_auto:")+row["subscription"],now)
            return [dict(row) for row in rows]


def verified_repair_request(raw, headers, secret, now):
    """Authenticate the reverse server-to-server command, not a public login."""
    stamp=headers.get("X-Web-Recover-Time","")
    signature=headers.get("X-Web-Recover-Signature","")
    if (len(secret)<48 or len(raw)>1024 or
        not re.fullmatch(r"[0-9]{1,12}",stamp) or
        not re.fullmatch(r"[0-9a-f]{64}",signature) or
        abs(now-int(stamp))>60):
        raise web.HTTPForbidden(text="Solicitud administrativa no autorizada.")
    expected=hmac.new(secret.encode(),
        b"web-recover-v1."+stamp.encode()+b"."+raw,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,signature):
        raise web.HTTPForbidden(text="Solicitud administrativa no autorizada.")
    try:
        data=json.loads(raw)
        if (not isinstance(data,dict) or set(data)!={"uid","actor"} or
            any(type(data[k]) is not int or not 0<data[k]<2**63 for k in ("uid","actor"))):
            raise ValueError("invalid target")
    except (ValueError,TypeError,UnicodeError):
        raise web.HTTPBadRequest(text="Telegram ID inválido.") from None
    return data


async def manual_paid_access_repair(g, request):
    data=verified_repair_request(await request.read(),request.headers,
        os.getenv("CUBANITAS_BRIDGE_SECRET",""),time.time())
    if not configured():
        raise web.HTTPServiceUnavailable(text="Pagos Web no disponibles.")
    uid=data["uid"]
    rows=await recover_missing_paid_access(g,uid,data["actor"])
    paid=await g.db().fetchrow("""
        SELECT s.paid_until FROM wg_subscriptions s
        JOIN wg_link_subscriptions p ON p.subscription=s.subscription
        WHERE s.uid=$1 AND s.state IN ('active','trialing') AND s.paid_until>$2
          AND p.price_id=$3 AND p.status IN ('active','trialing')
          AND p.paid_until>=s.paid_until AND p.last_invoice_paid_at>0
          AND EXISTS (SELECT 1 FROM wg_checkouts c WHERE c.uid=s.uid
                      AND c.subscription=s.subscription AND left(c.session,8)='cs_live_')
    """,uid,time.time(),os.environ["GALERIAS_STRIPE_PRICE_ID"])
    if paid and await g.entitled(uid):
        result={"status":"recovered" if rows else "already_active",
                "paid_until":int(paid["paid_until"])}
    else:
        result={"status":"not_recoverable"}
    return web.json_response(result,headers={"Cache-Control":"no-store"})


def event_subscription_for_lock(kind,obj):
    """Serialize the checkout, invoice and subscription events for one sub."""
    if kind in ("checkout.session.completed","checkout.session.async_payment_succeeded"):
        value=obj.get("subscription")
    elif kind in ("invoice.paid","invoice.payment_failed"):
        value=subscription_id_from_invoice(obj)
    elif kind in ("customer.subscription.created","customer.subscription.updated",
                  "customer.subscription.deleted"):
        value=obj.get("id")
    else:
        return None
    return value if isinstance(value,str) and value.startswith("sub_") else None


async def webhook(g,request):
    """Verify Stripe signature before reading metadata, UID or event state."""
    import stripe
    raw=await request.read()
    if len(raw)>200000:
        raise web.HTTPRequestEntityTooLarge(max_size=200000,actual_size=len(raw))
    try:
        signed=stripe.Webhook.construct_event(raw,
            request.headers.get("Stripe-Signature",""),
            os.environ["GALERIAS_STRIPE_WEBHOOK_SECRET"])
    except Exception:
        raise web.HTTPBadRequest(text="Firma del webhook Stripe inválida.") from None
    # stripe-python 15+ returns stripe.Event, which is NOT iterable.
    # .to_dict() produces recursively plain dicts for nested event objects.
    # Retain simple Mapping support for old clients and test fixtures.
    if hasattr(signed,"to_dict"):
        event=signed.to_dict()
    elif isinstance(signed,dict):
        event=signed
    else:
        raise web.HTTPBadRequest(text="Formato de evento Stripe inesperado.")
    kind=event.get("type")
    data=event.get("data") or {}
    obj=data.get("object") or {}
    eid=event.get("id")
    if not isinstance(eid,str) or not eid.startswith("evt_"):
        raise web.HTTPBadRequest(text="Evento Stripe inválido.")
    # An invoice or subscription event from other Stripe products may reach
    # this endpoint, but can never grant access without an authenticated
    # matching checkout nonce + exact recurring price.
    db=g.db()
    async with db.acquire() as conn:
        async with conn.transaction():
            if await conn.fetchval(
                    "SELECT 1 FROM wg_payment_events WHERE id=$1",eid):
                return web.json_response({"ok":True})
            # Checkout and invoice may arrive concurrently in separate transactions.
            # Lock before any staging so the second event sees the committed first.
            lock_sub=event_subscription_for_lock(kind,obj)
            if lock_sub:
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                                   "wg:stripe:"+lock_sub)
                if await conn.fetchval("SELECT 1 FROM wg_payment_events WHERE id=$1",eid):
                    return web.json_response({"ok":True})
            affected=None
            if kind in ("checkout.session.completed",
                        "checkout.session.async_payment_succeeded"):
                affected=await bind_paid_checkout(conn,obj)
            elif kind=="invoice.paid":
                affected=await stage_paid_invoice(
                    conn,obj,int(event.get("created") or 0))
            elif kind in ("customer.subscription.created",
                          "customer.subscription.updated",
                          "customer.subscription.deleted"):
                affected=await stage_subscription(
                    conn,obj,int(event.get("created") or time.time()))
                if kind=="customer.subscription.deleted" and affected:
                    await conn.execute("""UPDATE wg_link_subscriptions
                        SET status='canceled' WHERE subscription=$1""",affected)
            elif kind=="invoice.payment_failed":
                affected=subscription_id_from_invoice(obj)
                if affected:
                    await conn.execute("""UPDATE wg_link_subscriptions
                        SET status='past_due' WHERE subscription=$1
                        AND status NOT IN ('canceled','unpaid')""",affected)
            if affected:await reconcile_access(conn,affected)
            # Plus $1.99 is a separate, nonce-bound subscription. Its ledger
            # cannot modify the original $6.99 membership and shares this
            # already verified event / idempotency transaction.
            from .download_plus import apply_webhook as apply_plus_webhook
            await apply_plus_webhook(conn,kind,obj,int(event.get("created") or time.time()))
            # Risk evidence is queued only after Stripe signature verification.
            # The live Stripe provider is independently checked before any
            # user is denied paid access. No work on Telegram or other plans.
            from .payment_risk import stage_event as stage_risk
            await stage_risk(conn,kind,eid,event.get("created"))
            await conn.execute("""INSERT INTO wg_payment_events(id,created)
                VALUES($1,$2) ON CONFLICT DO NOTHING""",eid,time.time())
    # Commercial analytics is a separate, best-effort read/write after the
    # original payment transaction commits. An analytics fault can never
    # roll back paid access, duplicate an invoice, or make Stripe retry.
    try:
        from .commercial import record_verified_stripe_event
        await record_verified_stripe_event(
            g,kind,obj,eid,affected,event.get("created"))
    except Exception as exc:
        import logging
        logging.getLogger("web-galerias-commercial").warning(
            "Verified Stripe analytics pending (%s)",type(exc).__name__)
    return web.json_response({"ok":True})
