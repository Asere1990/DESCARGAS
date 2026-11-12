"""Read-only commercial reporting with verified, deduplicated Stripe events.

Metrics do not query Stripe. The public app only writes one visit per day;
purchase/renewal/cancellation events come AFTER the existing Stripe webhook
has already validated and committed its original subscription logic.
"""
import logging
import time
from datetime import datetime,timedelta,timezone

from aiohttp import web

from .permissions import ACCESS,ROOT,file_ids,is_admin,payment_test_user

log=logging.getLogger("web-galerias-commercial")
SCHEMA="""
CREATE TABLE IF NOT EXISTS wg_visit_days(
 uid BIGINT NOT NULL,
 visit_day DATE NOT NULL,
 PRIMARY KEY(uid,visit_day));
CREATE INDEX IF NOT EXISTS wg_visit_days_day ON wg_visit_days(visit_day);
CREATE TABLE IF NOT EXISTS wg_commercial_events(
 source TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('purchase','renewal','cancellation')),
 uid BIGINT NOT NULL,
 subscription TEXT,
 occurred DOUBLE PRECISION NOT NULL,
 amount_cents BIGINT NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS wg_commercial_events_recent
 ON wg_commercial_events(occurred DESC,kind);
ALTER TABLE wg_visit_days ENABLE ROW LEVEL SECURITY;
ALTER TABLE wg_commercial_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON wg_visit_days,wg_commercial_events FROM PUBLIC;
"""


async def record_verified_stripe_event(g,kind,obj,event_id,affected,created):
    """Best-effort analytics after Stripe access changes have committed.

    Never trust unbound Stripe subscriptions: only an existing verified
    checkout-session/nonce may count as a Web Galerías purchase or renewal.
    No failure here may retry an already-processed billing webhook.
    """
    if not affected or not isinstance(obj,dict):
        return
    if kind in ("checkout.session.completed","checkout.session.async_payment_succeeded"):
        session=obj.get("id")
        if not isinstance(session,str) or not session.startswith("cs_"):
            return
        source="checkout:"+session
        category="purchase"
        cents=int(obj.get("amount_total") or 0)
        row=await g.db().fetchrow("""
          SELECT uid FROM wg_checkouts WHERE session=$1 AND subscription=$2
          LIMIT 1""",session,affected)
    elif kind=="invoice.paid" and obj.get("billing_reason")=="subscription_cycle":
        if str(obj.get("currency") or "usd").lower()!="usd":
            return
        invoice=obj.get("id")
        if not isinstance(invoice,str) or not invoice.startswith("in_"):
            return
        source="invoice:"+invoice
        category="renewal"
        cents=int(obj.get("amount_paid") or 0)
        row=await g.db().fetchrow("""
          SELECT uid FROM wg_checkouts
          WHERE subscription=$1 AND session IS NOT NULL
          ORDER BY created DESC LIMIT 1""",affected)
    elif kind=="customer.subscription.deleted":
        source="cancel:"+str(event_id)
        category="cancellation"
        cents=0
        row=await g.db().fetchrow("""
          SELECT uid FROM wg_checkouts
          WHERE subscription=$1 AND session IS NOT NULL
          ORDER BY created DESC LIMIT 1""",affected)
    else:
        return
    if not row:
        return
    try:
        occurred=float(created)
    except (ValueError,TypeError):
        occurred=time.time()
    if not 0<occurred<time.time()+86400:
        occurred=time.time()
    await g.db().execute("""
      INSERT INTO wg_commercial_events(source,kind,uid,subscription,occurred,amount_cents)
      VALUES($1,$2,$3,$4,$5,$6)
      ON CONFLICT(source) DO NOTHING
    """,source,category,int(row["uid"]),affected,occurred,max(0,cents))


async def report(g,uid,data):
    if not is_admin(uid):
        raise web.HTTPForbidden(text="Solo administradores.")
    days=data.get("days",30)
    if type(days) is not int or days not in (7,30,90):
        raise web.HTTPBadRequest(text="Selecciona 7, 30 o 90 días.")
    today=datetime.now(timezone.utc).date()
    first=today-timedelta(days=days-1)
    end=today+timedelta(days=1)
    start_ts=datetime.combine(first,datetime.min.time(),timezone.utc).timestamp()
    end_ts=datetime.combine(end,datetime.min.time(),timezone.utc).timestamp()
    admin_ids=sorted(file_ids(ROOT/"admins.txt"))
    legacy_free=sorted(user for user in file_ids(ACCESS)
                       if not payment_test_user(user))
    db=g.db()
    metrics=await db.fetchrow("""
       SELECT
       (SELECT COUNT(DISTINCT uid)::int FROM wg_visit_days
           WHERE visit_day>=$1 AND visit_day<$2
             AND uid<>ALL($3::bigint[])) AS visitors,
       (SELECT COUNT(DISTINCT uid)::int FROM wg_checkouts
           WHERE created >= $4 AND created < $5
             AND uid<>ALL($3::bigint[])) AS checkout_users,
       (SELECT COUNT(*)::int FROM wg_checkouts
           WHERE created >= $4 AND created < $5
             AND uid<>ALL($3::bigint[])) AS checkout_attempts,
       (SELECT COUNT(*)::int FROM wg_commercial_events
           WHERE kind='purchase' AND occurred >=$4 AND occurred<$5
             AND uid<>ALL($3::bigint[])) AS purchases,
       (SELECT COUNT(*)::int FROM wg_commercial_events
           WHERE kind='renewal' AND occurred >=$4 AND occurred<$5
             AND uid<>ALL($3::bigint[])) AS renewals,
       (SELECT COUNT(*)::int FROM wg_commercial_events
           WHERE kind='cancellation' AND occurred >=$4 AND occurred<$5
             AND uid<>ALL($3::bigint[])) AS cancellations,
       (SELECT COALESCE(SUM(amount_cents),0)::bigint FROM wg_commercial_events
           WHERE kind IN ('purchase','renewal') AND occurred >=$4 AND occurred<$5
             AND uid<>ALL($3::bigint[])) AS gross_cents,
       (SELECT COUNT(*)::int FROM wg_subscriptions s
            LEFT JOIN wg_access_blocks b ON b.uid=s.uid
            WHERE s.state IN ('active','trialing') AND s.paid_until>$6
              AND NOT COALESCE(b.blocked,FALSE)
              AND s.uid<>ALL($3::bigint[])) AS active_paid,
       (SELECT COUNT(*)::int FROM (
          SELECT x.uid FROM unnest($7::bigint[]) AS x(uid)
           WHERE NOT EXISTS(SELECT 1 FROM wg_free_access gr WHERE gr.uid=x.uid)
          UNION ALL
          SELECT uid FROM wg_free_access WHERE enabled
        ) free
        LEFT JOIN wg_access_blocks b ON b.uid=free.uid
        WHERE NOT COALESCE(b.blocked,FALSE)
          AND free.uid<>ALL($3::bigint[])) AS active_free
    """,first,end,admin_ids,start_ts,end_ts,time.time(),legacy_free)
    # Only 7 or 14 points: no heavy gallery scans or unbounded Stripe queries.
    trend_start=today-timedelta(days=min(days,14)-1)
    daily=await db.fetch("""
       SELECT day::date AS day,
         (SELECT COUNT(DISTINCT v.uid)::int FROM wg_visit_days v
          WHERE v.visit_day=day::date AND v.uid<>ALL($3::bigint[])) AS visitors,
         (SELECT COUNT(*)::int FROM wg_commercial_events e
          WHERE e.kind='purchase'
          AND e.occurred>=EXTRACT(EPOCH FROM day AT TIME ZONE 'UTC')
          AND e.occurred<EXTRACT(EPOCH FROM (day+INTERVAL '1 day') AT TIME ZONE 'UTC')
          AND e.uid<>ALL($3::bigint[])) AS purchases
       FROM generate_series($1::date,$2::date,'1 day'::interval) AS day
       ORDER BY day
    """,trend_start,today,admin_ids)
    result={k:int(metrics[k] or 0) for k in (
        "visitors","checkout_users","checkout_attempts","purchases",
        "renewals","cancellations","gross_cents","active_paid","active_free")}
    result["conversion"]=round(
        100*result["purchases"]/result["checkout_attempts"],1
    ) if result["checkout_attempts"] else 0.0
    result["days"]=days
    result["from"]=first.isoformat()
    result["through"]=today.isoformat()
    result["trend"]=[{"day":r["day"].isoformat(),
                      "visitors":int(r["visitors"] or 0),
                      "purchases":int(r["purchases"] or 0)} for r in daily]
    result["note"]=(
        "Visitas únicas diarias y eventos de compra, renovación y cancelación "
        "registrados desde la activación de estas métricas. Las suscripciones "
        "activas muestran el estado vigente; no se infieren pagos históricos. "
        "Ingresos brutos registrados, sin descontar reembolsos.")
    return result
