"""Authenticated opt-in renewal cancellation for the Web subscription only.

Existing $6.99 and separately bought $1.99 unlimited-download subscriptions
remain paid/usable until their period ends. Uses the already signed
CUSTOMER-SERVICE bridge and its existing Stripe secret. Nothing happens on
page load, and no Telegram sessions or unrelated subscriptions are changed.
"""
import os
import re
import time
from aiohttp import web

from . import customer_bridge


def _valid_id(value,prefix):
    return isinstance(value,str) and bool(re.fullmatch(prefix+r"_[A-Za-z0-9_]+",value))


async def cancel(g,uid,confirmation):
    if confirmation!="CANCELAR_RENOVACION_WEB":
        raise web.HTTPBadRequest(text="Confirma la cancelación de la renovación.")
    if not customer_bridge.ready():
        raise web.HTTPServiceUnavailable(text="La cancelación no está disponible en este momento.")

    db=g.db()
    main=await db.fetchrow("""
      SELECT subscription,state,paid_until,cancel_at_period_end
      FROM wg_subscriptions WHERE uid=$1
    """,uid)
    now=time.time()
    if (not main or not _valid_id(main["subscription"],"sub")
            or main["state"] not in ("active","trialing")
            or float(main["paid_until"])<=now):
        raise web.HTTPForbidden(text="No existe una membresía Web pagada vigente.")

    # No Plus subscription is ever inferred from name, email, or customer.
    # Exactly the user's existing nonce-bound paid checkouts authorize extras.
    rows=await db.fetch("""
      SELECT DISTINCT s.subscription,s.price_id,s.cancel_at_period_end
      FROM wg_plus_checkouts c
      JOIN wg_plus_subscriptions s ON s.subscription=c.subscription
      WHERE c.uid=$1 AND c.session IS NOT NULL
        AND s.status IN ('active','trialing') AND s.paid_until>$2
    """,uid,now)
    price=os.getenv("GALERIAS_PLUS_PRICE","")
    if len(rows)>10:
        raise web.HTTPServiceUnavailable(text="Consulta con soporte para cancelar los extras.")
    if rows and not _valid_id(price,"price"):
        raise web.HTTPServiceUnavailable(text="No se pudo identificar la renovación de descargas ilimitadas.")
    extras=[]
    seen={main["subscription"]}
    for row in rows:
        sub=row["subscription"]
        if (not _valid_id(sub,"sub") or sub in seen or row["price_id"]!=price):
            # Never cancel the main membership while an extra might continue billing.
            raise web.HTTPServiceUnavailable(text="Es necesario verificar primero el extra de descargas ilimitadas.")
        seen.add(sub)
        extras.append({"subscription":sub,"price":price})

    payload={
        "telegram_id":uid,
        "subscription_id":main["subscription"],
        "confirm":confirmation,
        "plus":extras,
    }
    try:
        response=await customer_bridge.post_signed(
            "cancel-renewal",payload,timeout=25)
    except Exception:
        raise web.HTTPServiceUnavailable(
            text="No se pudo confirmar la cancelación en Stripe. Inténtalo de nuevo.") from None
    if not isinstance(response,dict) or not isinstance(response.get("main"),dict):
        raise web.HTTPServiceUnavailable(text="Stripe no devolvió confirmación de la renovación.")
    main_ok=response["main"].get("scheduled") is True
    plus_result=response.get("plus")
    if not isinstance(plus_result,list):
        raise web.HTTPServiceUnavailable(text="No se confirmaron los extras.")
    plus_status={
        row["subscription"]:bool(row.get("scheduled") is True)
        for row in plus_result if isinstance(row,dict)
        and _valid_id(row.get("subscription"),"sub")
    }
    # Never update a different subscription, even if a bridge payload is wrong.
    if set(plus_status)!=set(row["subscription"] for row in extras):
        raise web.HTTPServiceUnavailable(text="No se verificaron todos los extras.")
    now=time.time()
    if main_ok:
        await db.execute("""
            UPDATE wg_subscriptions
            SET cancel_at_period_end=TRUE,changed=$3
            WHERE uid=$1 AND subscription=$2 AND state IN ('active','trialing')
        """,uid,main["subscription"],now)
        await db.execute("""
            UPDATE wg_link_subscriptions
            SET cancel_at_period_end=TRUE,changed=GREATEST(changed,$2)
            WHERE subscription=$1
        """,main["subscription"],now)
    for extra in extras:
        sub=extra["subscription"]
        if plus_status.get(sub):
            await db.execute("""
                UPDATE wg_plus_subscriptions SET cancel_at_period_end=TRUE,
                changed=GREATEST(changed,$2)
                WHERE subscription=$1 AND price_id=$3
            """,sub,now,price)
    plus_ok=all(plus_status.values())
    until=int(main["paid_until"] or 0)
    end=response["main"].get("end")
    if main_ok and type(end) is int and until<end<4102444800:
        until=end
    return {
        "ok":bool(main_ok and plus_ok),
        "main_scheduled":main_ok,
        "plus_scheduled":sum(plus_status.values()),
        "plus_total":len(extras),
        "active_until":until,
    }
