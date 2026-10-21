"""Monthly subscriptions: Telegram receipts and signed Stripe lifecycle events."""
import asyncio
import calendar
from datetime import datetime,timezone
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode
from aiohttp import web

STARS=500
PERIOD=2592000
class PaymentRequired(ValueError):pass

def next_month(timestamp):
    dt=datetime.fromtimestamp(timestamp,timezone.utc)
    year,month=(dt.year+1,1) if dt.month==12 else (dt.year,dt.month+1)
    return dt.replace(year=year,month=month,day=min(dt.day,calendar.monthrange(year,month)[1])).timestamp()

def verified_event(raw,signature,secret,now=None):
    if not secret:raise ValueError('Payments not configured')
    values={}
    for part in signature.split(','):
        key,_,value=part.partition('=');values.setdefault(key,[]).append(value)
    stamp=values.get('t',[''])[0]
    if not stamp.isdigit() or abs((now or time.time())-int(stamp))>300:raise ValueError('Invalid signature')
    expected=hmac.new(secret.encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected,v) for v in values.get('v1',[])):raise ValueError('Invalid signature')
    event=json.loads(raw)
    if not isinstance(event,dict) or not event.get('id') or not isinstance(event.get('created'),int):raise ValueError('Invalid event')
    return event

def object_id(value):return value.get('id','') if isinstance(value,dict) else value or ''
def invoice_subscription(obj):
    return object_id(obj.get('subscription')) or object_id(obj.get('parent',{}).get('subscription_details',{}).get('subscription'))
def line_price(line):
    return object_id(line.get('price')) or object_id(line.get('pricing',{}).get('price_details',{}).get('price'))

class Billing:
    def __init__(self,service):self.s=service;self.repo=service.repo
    async def configure(self):
        config={'url':os.environ.get('DESCARGAS_PAYMENT_LINK',''),'group':int(os.environ.get('DESCARGAS_PAYMENT_GROUP','0')),
            'ready':bool(os.environ.get('DESCARGAS_PAYMENT_LINK') and os.environ.get('DESCARGAS_STRIPE_WEBHOOK_SECRET') and os.environ.get('DESCARGAS_STRIPE_PRICE_ID') and os.environ.get('DESCARGAS_STRIPE_LINK_ID'))}
        await self.repo.set_setting('payment_config',json.dumps(config))
        # Previously verified purchases must not stay gated on the retired group.
        await self.repo.pool.execute("""UPDATE descarga_memberships m SET confirmed=TRUE
            WHERE m.bot_id=$1 AND m.provider='stripe' AND NOT m.confirmed
            AND EXISTS(SELECT 1 FROM descarga_payment_intents i WHERE i.bot_id=m.bot_id
                AND i.uid=m.uid AND i.provider='stripe' AND i.subscription=m.subscription)""",self.s.bot_id)
    async def require(self,uid,source=None):
        from .moderation import authorized
        if authorized(uid):return
        policy=await self.repo.policy(self.s.bot_id,uid,source)
        if policy['pay'] and await self.repo.member_until(self.s.bot_id,uid)<=time.time():raise PaymentRequired()
    async def prompt(self,uid,message_id=None):
        from .moderation import authorized
        if authorized(uid):return await self.s.tell(uid,'✅ Acceso de administrador. No necesitas una membresía para descargar.')
        if await self.repo.member_until(self.s.bot_id,uid)>time.time():
            return await self.s.tell(uid,'✅ Tu suscripción está activa. Vuelve a enviar tus enlaces.')
        intent=await self.repo.payment_intent(self.s.bot_id,uid,'stars')
        url=intent['url']
        if not url:
            url=await self.s.api('createInvoiceLink',{'title':'DESCARGAS · Suscripción mensual',
                'description':'Acceso personal a las descargas que requieren suscripción. Renovación automática cada 30 días.',
                'payload':intent['token'],'currency':'XTR','prices':[{'label':'Servicio mensual','amount':STARS}],
                'provider_token':'','subscription_period':PERIOD})
            await self.repo.invoice_url(intent['token'],url)
        buttons=[[{'text':'⭐️ PAGAR CON STARS','url':url,'style':'success'}]]
        config=json.loads(await self.repo.setting('payment_config') or '{}')
        stripe_ready=bool(config.get('ready'))
        if stripe_ready:
            buttons.extend([[{'text':'🍎 PAGAR Apple/Google PAY','callback_data':'pay:stripe','style':'primary'}],
                [{'text':'💳 PAGAR CON DÉBITO/CRÉDITO','callback_data':'pay:stripe','style':'primary'}]])
        extra='\n🍎𝐀𝐩𝐩𝐥𝐞/𝐆𝐨𝐨𝐠𝐥𝐞 𝐏𝐚𝐲: $6.99 USD mensuales.\n💳𝐃𝐞́𝐛𝐢𝐭𝐨/𝐂𝐫𝐞́𝐝𝐢𝐭𝐨: $6.99 USD mensuales.' if stripe_ready else ''
        await self.s.api('editMessageText' if message_id else 'sendMessage',{'chat_id':uid,**({'message_id':message_id} if message_id else {}),'text':'💳 𝐀𝐂𝐓𝐈𝐕𝐀𝐑 𝐃𝐄𝐒𝐂𝐀𝐑𝐆𝐀𝐒\n\n'
            '⭐️𝐓𝐞𝐥𝐞𝐠𝐫𝐚𝐦 𝐒𝐭𝐚𝐫𝐬: 500⭐ cada 30 días.'+extra+'\n\n'
            'Tu período de prueba ha expirado. Activa tu suscripción para continuar descargando contenido. Puedes cancelar la suscripción en cualquier momento.',
            'reply_markup':{'inline_keyboard':buttons}})
    async def update(self,update):
        if await self.group_update(update):return True
        pre=update.get('pre_checkout_query')
        if pre:
            uid=pre['from']['id'];row=await self.repo.intent(pre.get('invoice_payload',''))
            ok=bool(row and row['bot_id']==self.s.bot_id and row['uid']==uid and row['provider']=='stars' and pre.get('currency')=='XTR' and pre.get('total_amount')==STARS)
            if ok:ok=not await self.repo.is_blocked(self.s.bot_id,uid)
            if ok and not row['subscription']:ok=await self.repo.member_until(self.s.bot_id,uid)<=time.time()
            await self.s.api('answerPreCheckoutQuery',{'pre_checkout_query_id':pre['id'],'ok':ok,**({} if ok else {'error_message':'Este pago no corresponde a tu cuenta o ya tienes una suscripción activa.'})})
            return True
        message=update.get('message',{})
        refund=message.get('refunded_payment')
        if refund:
            if refund.get('currency')=='XTR' and refund.get('total_amount',0)>0 and refund.get('telegram_payment_charge_id'):
                uid=await self.repo.refund_stars(self.s.bot_id,refund.get('invoice_payload',''),refund['telegram_payment_charge_id'])
                from .moderation import authorized
                if uid and not authorized(uid):await self.s.tell(uid,'Bot fuera de servicio')
            return True
        from .subscription_actions import menu,cancel
        query=update.get('callback_query',{})
        if query.get('data') in ('membership:cancel','membership:confirm'):
            uid=query['from']['id']
            await self.s.api('answerCallbackQuery',{'callback_query_id':query['id']})
            if query.get('message',{}).get('chat',{}).get('id')!=uid:return True
            if query['data']=='membership:cancel':
                await self.s.api('sendMessage',{'chat_id':uid,'text':'¿Quieres cancelar la renovación de tu membresía? Conservarás el acceso hasta terminar el período pagado.',
                    'reply_markup':{'inline_keyboard':[[{'text':'CONFIRMAR CANCELACIÓN','callback_data':'membership:confirm','style':'danger'}]]}})
            else:
                try:result=await cancel(self.s,uid);await self.s.tell(uid,result['message'])
                except Exception:await self.s.tell(uid,'No se pudo confirmar la cancelación. Tu suscripción no figura como cancelada; inténtalo de nuevo más tarde.')
            return True
        if message.get('chat',{}).get('type')=='private':
            uid=message['from']['id'];command=message.get('text','').split(' ',1)[0]
            if command=='/cancelar_membresia':
                await menu(self.s,uid);return True
            if command in ('/start','/suscripcion') and await self.repo.member_until(self.s.bot_id,uid)>time.time():
                await menu(self.s,uid);return True
        if message.get('successful_payment'):
            payment=message['successful_payment'];uid=message['from']['id'];until=payment.get('subscription_expiration_date',0)
            if message.get('chat',{}).get('type')!='private' or payment.get('currency')!='XTR' or payment.get('total_amount')!=STARS or not payment.get('is_recurring') or not isinstance(until,int):return True
            if until<=time.time():return True
            fresh=await self.repo.grant_stars(self.s.bot_id,uid,payment.get('invoice_payload',''),payment['telegram_payment_charge_id'],until)
            if fresh:await self.s.tell(uid,'✅ SUSCRIPCIÓN ACTIVA\nTu acceso mensual está desbloqueado. Vuelve a enviar tus enlaces.\nPuedes gestionar o cancelar la renovación desde Telegram → Ajustes → Mis estrellas → Suscripciones.')
            return True
        query=update.get('callback_query',{})
        if query.get('data')=='pay:methods':
            uid=query['from']['id']
            await self.s.api('answerCallbackQuery',{'callback_query_id':query['id']})
            message=query.get('message',{})
            if message.get('chat',{}).get('id')!=uid:return True
            await self.prompt(uid,message.get('message_id'))
            return True
        if query.get('data')=='pay:stripe':
            uid=query['from']['id']
            await self.s.api('answerCallbackQuery',{'callback_query_id':query['id']})
            if query.get('message',{}).get('chat',{}).get('id')!=uid:return True
            if await self.repo.is_blocked(self.s.bot_id,uid):
                await self.s.tell(uid,'Bot fuera de servicio');return True
            if await self.repo.member_until(self.s.bot_id,uid)>time.time():
                await self.s.tell(uid,'✅ Tu suscripción está activa. Vuelve a enviar tus enlaces.');return True
            intent=await self.repo.payment_intent(self.s.bot_id,uid,'stripe')
            link=os.environ.get('DESCARGAS_PAYMENT_LINK','')
            if not link:await self.s.tell(uid,'El pago con tarjeta no está disponible en este momento.');return True
            url=link+('&' if '?' in link else '?')+urlencode({'client_reference_id':intent['token']})
            payment_message_id=query.get('message',{}).get('message_id')
            await self.s.api('editMessageText' if payment_message_id else 'sendMessage',{'chat_id':uid,**({'message_id':payment_message_id} if payment_message_id else {}),'text':'💳𝐒𝐔𝐒𝐂𝐑𝐈𝐏𝐂𝐈𝐎𝐍 𝐌𝐄𝐍𝐒𝐔𝐀𝐋\n\n𝐏𝐫𝐞𝐜𝐢𝐨: $6.99 USD/mes\n\nAl completar tu pago de forma segura, tu suscripción se activará inmediatamente.',
                'reply_markup':{'inline_keyboard':[[{'text':'💳 CONTINUAR AL PAGO','url':url,'style':'success'}],[{'text':'⬅️ CAMBIAR MÉTODO DE PAGO','callback_data':'pay:methods','style':'danger'}]]}})
            return True
        if message.get('chat',{}).get('type')=='private' and message.get('text','').strip()=='/suscripcion':
            uid=message['from']['id'];until=await self.repo.member_until(self.s.bot_id,uid)
            if until<=time.time():await self.prompt(uid)
            else:
                text='✅ Suscripción activa hasta '+datetime.fromtimestamp(until,timezone.utc).strftime('%d/%m/%Y %H:%M UTC')+'.\nStars: gestiona la renovación desde Ajustes → Mis estrellas → Suscripciones.'
                portal=os.environ.get('DESCARGAS_BILLING_PORTAL','')
                await self.s.api('sendMessage',{'chat_id':uid,'text':text,**({'reply_markup':{'inline_keyboard':[[{'text':'💳 GESTIONAR SUSCRIPCIÓN','url':portal,'style':'primary'}]]}} if portal else {})})
            return True
        return False
    async def stripe(self,event):
        if event.get('livemode') is not True:return None
        obj=event.get('data',{}).get('object',{});kind=event.get('type','')
        price=os.environ.get('DESCARGAS_STRIPE_PRICE_ID','');link=os.environ.get('DESCARGAS_STRIPE_LINK_ID','')
        if not price or not link:return None
        if kind=='charge.dispute.created':
            charge=object_id(obj.get('charge'))
            if charge:await self.repo.pool.execute('INSERT INTO descarga_disputes(id,charge) VALUES($1,$2) ON CONFLICT DO NOTHING',obj['id'],charge)
            return None
        if kind in ('checkout.session.completed','checkout.session.async_payment_succeeded'):
            if obj.get('mode')!='subscription' or object_id(obj.get('payment_link'))!=link or obj.get('payment_status')!='paid' or obj.get('currency')!='usd' or obj.get('amount_total')!=699:return None
            sub=object_id(obj.get('subscription'));token=obj.get('client_reference_id','')
            if not sub or not token.startswith('d_'):return None
            uid=await self.repo.stripe_event(event,checkout=(token,sub,next_month(obj.get('created',event['created']))),require_group=False)
            if uid:
                await self.repo.queue_buyer(object_id(obj.get('invoice')) or obj['id'],self.s.bot_id,uid,{'session_id':obj['id'],'invoice_id':object_id(obj.get('invoice')),'subscription_id':sub,'customer_id':object_id(obj.get('customer')),'name':(obj.get('customer_details') or {}).get('name'),'email':(obj.get('customer_details') or {}).get('email'),'billing_address':(obj.get('customer_details') or {}).get('address'),'paid_at':event['created']})

            return uid
        if kind in ('invoice.paid','invoice.payment_failed'):
            lines=[line for line in obj.get('lines',{}).get('data',[]) if line_price(line)==price]
            sub=invoice_subscription(obj)
            if not lines or not sub:return None
            if kind=='invoice.payment_failed':return await self.repo.stripe_event(event,state=(sub,'past_due'))
            if obj.get('status')!='paid' or obj.get('currency')!='usd' or obj.get('amount_paid',0)<699:return None
            until=max(line.get('period',{}).get('end',0) for line in lines)
            if not until:return None
            uid=await self.repo.stripe_event(event,period=(sub,obj['id'],until))
            if uid:await self.repo.queue_buyer(obj['id'],self.s.bot_id,uid,{'invoice_id':obj['id'],'subscription_id':sub,'customer_id':object_id(obj.get('customer')),'name':obj.get('customer_name'),'email':obj.get('customer_email'),'billing_address':obj.get('customer_address'),'paid_at':event['created']})
            return uid
        if kind.startswith('customer.subscription.'):
            if not any(object_id(i.get('price'))==price for i in obj.get('items',{}).get('data',[])):return None
            return await self.repo.stripe_event(event,state=(obj['id'],obj.get('status','canceled')))

    async def group_update(self,update):
        # The former confirmation group is now an audit destination only.
        return False
    async def maintenance(self):
        from .buyer_profiles import enrich_pending
        while not self.s.stopping:
            try:
                await enrich_pending(self.s)
                from .payment_notices import send_pending
                await send_pending(self.s)
                from .subscription_actions import disputes
                await disputes(self.s)
            except asyncio.CancelledError:raise
            except Exception:pass
            await asyncio.sleep(15)

def mount_payments(app,service):
    async def webhook(request):
        if not service.ready:return web.Response(status=503)
        raw=await request.read()
        try:event=verified_event(raw,request.headers.get('Stripe-Signature',''),os.environ.get('DESCARGAS_STRIPE_WEBHOOK_SECRET',''))
        except (ValueError,TypeError):return web.Response(status=400,text='Invalid event')
        uid=await service.billing.stripe(event)
        return web.json_response({'received':True})
    app.router.add_post('/payments/stripe',webhook)
