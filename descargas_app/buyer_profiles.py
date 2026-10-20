"""Enrich only verified DESCARGAS invoices; store a whitelisted encrypted summary."""
import os,time
from stripe import StripeClient
from stripe._http_client import HTTPXClient
from .billing import object_id

async def enrich_pending(service):
    key=os.environ.get('STRIPE_KEY','').strip()
    if not key:return
    rows=await service.repo.pool.fetch("SELECT invoice,bot_id,uid,payload FROM descarga_buyer_profiles WHERE bot_id=$1 AND state='pending' AND available<=$2 LIMIT 2",service.bot_id,time.time())
    if not rows:return
    http=HTTPXClient(timeout=20)
    client=StripeClient(key,stripe_version='2026-08-26.dahlia',max_network_retries=1,http_client=http)
    try:
        await enrich_rows(service,client,rows)
    finally:
        await http.close_async()

async def enrich_rows(service,client,rows):
    for row in rows:
        data=service.repo.decode(row['payload'])
        try:
            invoice=data.get('invoice_id')
            if not invoice:raise ValueError('Invoice not available')
            payments=await client.v1.invoice_payments.list_async(params={'invoice':invoice,'status':'paid','limit':10})
            charge=None
            for payment in payments.get('data',[]):
                details=payment.get('payment',{})
                charge_id=object_id(details.get('charge'))
                if details.get('payment_intent'):
                    intent=await client.v1.payment_intents.retrieve_async(object_id(details['payment_intent']),params={'expand':['latest_charge']})
                    charge=intent.get('latest_charge')
                    if isinstance(charge,str):charge_id=charge;charge=None
                if charge_id and not charge:charge=await client.v1.charges.retrieve_async(charge_id)
                if charge:break
            if not charge or not charge.get('paid'):raise ValueError('Paid charge not available')
            if object_id(charge.get('customer'))!=data.get('customer_id'):raise ValueError('Customer mismatch')
            billing=charge.get('billing_details') or {};method=charge.get('payment_method_details') or {};card=method.get('card') or {}
            data.update({'name':billing.get('name') or data.get('name'),'email':billing.get('email') or data.get('email'),
                'billing_address':billing.get('address') or data.get('billing_address'),'charge_id':charge['id'],
                'card_fingerprint':card.get('fingerprint'),'card_brand':card.get('brand'),'card_funding':card.get('funding'),
                'card_last4':card.get('last4'),'payment_method_type':method.get('type'),'wallet_type':(card.get('wallet') or {}).get('type')})
            await service.repo.pool.execute("UPDATE descarga_buyer_profiles SET payload=$2,state='complete' WHERE invoice=$1",row['invoice'],service.repo.encode(data))
        except Exception:
            await service.repo.pool.execute('UPDATE descarga_buyer_profiles SET available=$2 WHERE invoice=$1',row['invoice'],time.time()+300)
