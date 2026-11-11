"""Membresías exclusivas de Web Galerías; no reutiliza ni cambia facturación de DESCARGAS."""
import asyncio
import os
import secrets
import time
from aiohttp import web

def ready():
    from .payment_link_checkout import configured
    if configured():return True
    return all(os.getenv(k) for k in ("GALERIAS_STRIPE_KEY","GALERIAS_STRIPE_PRICE_ID",
                                     "GALERIAS_STRIPE_WEBHOOK_SECRET"))

def from_subscription(sub):
    end=int(sub.get("current_period_end") or 0)
    if not end:
        end=max([int(item.get("current_period_end") or 0)
                 for item in sub.get("items",{}).get("data",[])]+[0])
    return end

async def checkout(g,uid):
    from .core import customer_bot_signed
    if customer_bot_signed(uid) and await g.db().fetchval(
            "SELECT blocked FROM wg_access_blocks WHERE uid=$1",uid):
        raise web.HTTPForbidden(text="Cuenta bloqueada. Contacta con soporte.")
    if await g.entitled(uid):return {"allowed":True}
    from .permissions import payment_test_user
    if (os.getenv("GALERIAS_PUBLIC","0")!="1"
        and not payment_test_user(uid) and not customer_bot_signed(uid)):
        raise web.HTTPForbidden(text="El acceso todavía está en prueba privada.")
    if not ready():
        raise web.HTTPServiceUnavailable(text="Pagar con tarjeta aún no está configurado.")
    from .payment_link_checkout import configured,checkout as new_checkout
    if configured():
        return await new_checkout(g,uid)
    import stripe
    stripe.api_key=os.environ["GALERIAS_STRIPE_KEY"]
    nonce=secrets.token_urlsafe(24)
    db=g.db()
    await db.execute("INSERT INTO wg_checkouts(nonce,uid,created) VALUES($1,$2,$3)",
                     nonce,uid,time.time())
    # Devolver al DM del bot después del pago, no a una web sin initData de Telegram.
    bot=os.getenv("GALERIAS_RETURN_BOT","Descargas_Robot").lstrip("@")
    if not bot.replace("_","").isalnum():
        raise web.HTTPServiceUnavailable(text="El bot de retorno no está configurado.")
    url="https://t.me/"+bot+"?start=galerias"
    try:
        session=await asyncio.to_thread(stripe.checkout.Session.create,
           mode="subscription",
           line_items=[{"price":os.environ["GALERIAS_STRIPE_PRICE_ID"],"quantity":1}],
           client_reference_id=str(uid),metadata={"gallery_nonce":nonce},
           subscription_data={"metadata":{"gallery_uid":str(uid)}},
           success_url=url,cancel_url=url)
    except Exception:
        raise web.HTTPServiceUnavailable(text="No se pudo crear el pago. Inténtalo después.") from None
    await db.execute("UPDATE wg_checkouts SET session=$2 WHERE nonce=$1",nonce,session.id)
    return {"url":session.url}

async def webhook(g,request):
    if not ready():raise web.HTTPServiceUnavailable(text="Los pagos no están configurados.")
    from .payment_link_checkout import configured,webhook as link_webhook
    if configured():return await link_webhook(g,request)
    raw=await request.read()
    if len(raw)>200000:raise web.HTTPRequestEntityTooLarge(max_size=200000,actual_size=len(raw))
    import stripe
    try:
        event=stripe.Webhook.construct_event(raw,request.headers.get("Stripe-Signature",""),
                                              os.environ["GALERIAS_STRIPE_WEBHOOK_SECRET"])
    except Exception:raise web.HTTPBadRequest(text="Firma de pago incorrecta.") from None
    stripe.api_key=os.environ["GALERIAS_STRIPE_KEY"]
    event=dict(event);kind=event["type"];obj=event["data"]["object"]
    db=g.db()
    eid=str(event["id"])
    if await db.fetchval("SELECT 1 FROM wg_payment_events WHERE id=$1",eid):
        return web.json_response({"ok":True})
    if kind=="checkout.session.completed" and obj.get("payment_status")=="paid":
        nonce=obj.get("metadata",{}).get("gallery_nonce")
        row=await db.fetchrow("SELECT uid FROM wg_checkouts WHERE nonce=$1 AND session=$2",
                              nonce,obj.get("id"))
        sub_id=obj.get("subscription")
        if row and sub_id:
            lines=await asyncio.to_thread(stripe.checkout.Session.list_line_items,obj["id"],limit=10)
            price=os.environ["GALERIAS_STRIPE_PRICE_ID"]
            if not any(item.get("price",{}).get("id")==price for item in lines["data"]):
                raise web.HTTPForbidden(text="El pago no corresponde a Web Galerías.")
            sub=await asyncio.to_thread(stripe.Subscription.retrieve,sub_id)
            until=from_subscription(sub)
            if until<=time.time():raise web.HTTPServiceUnavailable(text="Período no confirmado.")
            await db.execute("""INSERT INTO wg_subscriptions(uid,customer,subscription,state,paid_until,changed)
               VALUES($1,$2,$3,'active',$4,$5)
               ON CONFLICT(uid) DO UPDATE SET customer=excluded.customer,
               subscription=excluded.subscription,state=excluded.state,
               paid_until=GREATEST(wg_subscriptions.paid_until,excluded.paid_until),
               changed=excluded.changed""",row["uid"],str(obj.get("customer") or ""),str(sub_id),
               until,time.time())
    elif kind in ("invoice.paid","invoice.payment_succeeded"):
        parent=obj.get("parent") or {}
        details=parent.get("subscription_details") or {}
        sub_id=obj.get("subscription") or details.get("subscription")
        if sub_id:
            lines=(obj.get("lines") or {}).get("data") or []
            until=max([int((x.get("period") or {}).get("end") or 0) for x in lines]+[0])
            if until:
                await db.execute("""UPDATE wg_subscriptions SET state='active',
                   paid_until=GREATEST(paid_until,$2),changed=$3
                   WHERE subscription=$1""",str(sub_id),until,time.time())
    elif kind=="invoice.payment_failed":
        parent=obj.get("parent") or {}
        sub_id=obj.get("subscription") or (parent.get("subscription_details") or {}).get("subscription")
        if sub_id:
            await db.execute("""UPDATE wg_subscriptions SET state='past_due',changed=$2
               WHERE subscription=$1""",str(sub_id),time.time())
    elif kind in ("customer.subscription.updated","customer.subscription.deleted"):
        status=str(obj.get("status") or "canceled")
        if kind.endswith(".deleted"):status="canceled"
        await db.execute("""UPDATE wg_subscriptions SET state=$2,
           paid_until=GREATEST(paid_until,$3),changed=$4
           WHERE subscription=$1""",str(obj.get("id")),status,from_subscription(obj),time.time())
    await db.execute("INSERT INTO wg_payment_events(id,created) VALUES($1,$2) ON CONFLICT DO NOTHING",
                     eid,time.time())
    return web.json_response({"ok":True})
