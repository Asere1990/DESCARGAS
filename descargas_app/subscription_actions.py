"""Owner-scoped cancellation and permanent blocks for verified disputed charges."""
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from stripe import StripeClient
from stripe._http_client import HTTPXClient
from .billing import object_id

MONTHS=('Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre')

def expiry_date(stamp):
    date=datetime.fromtimestamp(stamp,ZoneInfo('America/New_York'))
    return f'{date.day:02d}-{MONTHS[date.month-1]}-{date.year}'

@asynccontextmanager
async def stripe_client():
    key=os.environ.get('STRIPE_KEY','').strip()
    if not key:raise ValueError('La gestión de pagos no está disponible en este momento. Inténtalo más tarde.')
    http=HTTPXClient(timeout=20)
    try:yield StripeClient(key,stripe_version='2026-08-26.dahlia',max_network_retries=1,http_client=http)
    finally:await http.close_async()

async def membership(service,uid):
    rows=await service.repo.pool.fetch("SELECT provider,subscription,paid_until FROM descarga_memberships WHERE bot_id=$1 AND uid=$2 AND confirmed AND paid_until>$3 AND state NOT IN ('revoked','refunded','incomplete_expired')",service.bot_id,uid,time.time())
    return rows

async def summary(service,uid):
    rows=await membership(service,uid)
    return {'active':bool(rows),'until':expiry_date(max(r['paid_until'] for r in rows)) if rows else None}

async def cancel(service,uid):
    # Subscription identifiers come exclusively from the authenticated owner's rows.
    rows=await membership(service,uid)
    if not rows:return {'active':False,'message':'No tienes una membresía activa.'}
    for row in rows:
        if row['provider']=='stars':
            await service.api('editUserStarSubscription',{'user_id':uid,'telegram_payment_charge_id':row['subscription'],'is_canceled':True})
        elif row['provider']=='stripe':
            async with stripe_client() as client:
                await client.v1.subscriptions.update_async(row['subscription'],params={'cancel_at_period_end':True})
        else:raise ValueError('Método de pago no reconocido.')
    until=expiry_date(max(r['paid_until'] for r in rows))
    return {'active':True,'until':until,'message':'✅ Renovación cancelada. Tu membresía seguirá activa hasta el '+until+'. No se realizarán nuevas renovaciones de esta suscripción.'}

async def menu(service,uid):
    info=await summary(service,uid)
    if not info['active']:return await service.billing.prompt(uid)
    await service.api('sendMessage',{'chat_id':uid,'text':'⭐️ MEMBRESÍA ACTIVA\n\nTu membresía estará activa hasta el '+info['until']+'.',
        'reply_markup':{'inline_keyboard':[[{'text':'CANCELAR MEMBRESÍA','callback_data':'membership:cancel','style':'danger'}]]}})

async def disputes(service):
    rows=await service.repo.pool.fetch("SELECT id,charge FROM descarga_disputes WHERE state='pending' AND available<=$1 LIMIT 10",time.time())
    for row in rows:
        try:
            async with stripe_client() as client:
                charge=await client.v1.charges.retrieve_async(row['charge'])
                pi=object_id(charge.get('payment_intent'))
                if not pi:raise ValueError('Missing payment intent')
                payments=await client.v1.invoice_payments.list_async(params={'payment':{'type':'payment_intent','payment_intent':pi},'limit':100,'expand':['data.invoice']})
                subscriptions=set()
                for payment in payments.get('data',[]):
                    invoice=payment.get('invoice')
                    if isinstance(invoice,str):invoice=await client.v1.invoices.retrieve_async(invoice)
                    if not invoice:continue
                    sub=object_id(invoice.get('subscription')) or object_id((invoice.get('parent') or {}).get('subscription_details',{}).get('subscription'))
                    if sub:subscriptions.add(sub)
            owners=await service.repo.pool.fetch("SELECT DISTINCT bot_id,uid FROM descarga_memberships WHERE provider='stripe' AND subscription=ANY($1::text[])",list(subscriptions))
            if not owners:raise ValueError('Payment ownership not available yet')
            for owner in owners:
                await service.repo.pool.execute('INSERT INTO descarga_dispute_blocks(bot_id,uid,dispute) VALUES($1,$2,$3) ON CONFLICT DO NOTHING',owner['bot_id'],owner['uid'],row['id'])
            await service.repo.pool.execute("UPDATE descarga_disputes SET state='processed' WHERE id=$1",row['id'])
        except Exception:
            await service.repo.pool.execute('UPDATE descarga_disputes SET available=$2 WHERE id=$1',row['id'],time.time()+30)
