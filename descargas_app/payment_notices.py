"""Private payment audit notices, one claim per verified Stripe invoice."""
import html
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from babel import Locale

LOG=logging.getLogger(__name__)

def receipt_text(uid,data,user):
    esc=lambda value:html.escape(str(value or 'Sin datos'))
    person=lambda value:f'<a href="tg://user?id={int(uid)}">{esc(value)}</a>'
    name=' '.join(user.get(k,'') for k in ('first_name','last_name')).strip() or str(uid)
    address=data.get('billing_address') or {}
    city=', '.join(str(address[k]) for k in ('city','state') if address.get(k)) or 'Sin datos'
    country=address.get('country') or ''
    country=Locale('es').territories.get(country,country or 'Sin datos')
    dt=datetime.fromtimestamp(data['paid_at'],ZoneInfo('America/New_York'))
    username='@'+user['username'] if user.get('username') else 'Sin username'
    return ('✅𝐂𝐎𝐍𝐅𝐈𝐑𝐌𝐀𝐂𝐈𝐎 𝐃𝐄 𝐏𝐀𝐆𝐎\n\n'
        f'🪪 𝐒𝐓𝐑𝐈𝐏𝐄: {person(data.get("name"))}\n'
        f'👤 𝐍𝐎𝐌𝐁𝐑𝐄: {person(name)}\n'
        f'🔗 𝐔𝐒𝐔𝐀𝐑𝐈𝐎: {esc(username)}\n'
        f'📧 𝐄𝐌𝐀𝐈𝐋𝐒: {esc(data.get("email"))}\n'
        f'📅 𝐅𝐄𝐂𝐇𝐀: {dt:%d-%m-%Y}\n'
        f'⏰ 𝐇𝐎𝐑𝐀: {dt:%H:%M:%S}\n'
        f'#️⃣ 𝐓𝐄𝐋𝐄𝐆𝐑𝐀𝐌_𝐈𝐃: {int(uid)}\n'
        f'🏙️ 𝐂𝐈𝐔𝐃𝐀𝐃: {esc(city)}\n'
        f'🗺️ 𝐏𝐀𝐈𝐒: {esc(country)} (facturación)')

async def send_pending(service):
    group=int(os.environ.get('DESCARGAS_PAYMENT_GROUP','0'))
    if not group:return
    pool=service.repo.pool
    rows=await pool.fetch("""SELECT n.invoice,n.uid,n.created,p.payload FROM descarga_payment_notices n
        JOIN descarga_buyer_profiles p USING(invoice) WHERE n.bot_id=$1 AND n.state='pending'
        AND n.available<=$2 ORDER BY n.created LIMIT 5""",service.bot_id,time.time())
    for row in rows:
        try:
            data=service.repo.decode(row['payload'])
            data.setdefault('paid_at',row['created'])
            user=await service.api('getChat',{'chat_id':row['uid']})
            text=receipt_text(row['uid'],data,user)
        except Exception:
            await pool.execute('UPDATE descarga_payment_notices SET available=$2 WHERE invoice=$1',row['invoice'],time.time()+60)
            continue
        claimed=await pool.fetchval("UPDATE descarga_payment_notices SET state='sending' WHERE invoice=$1 AND state='pending' RETURNING invoice",row['invoice'])
        if not claimed:continue
        try:
            await service.api('sendMessage',{'chat_id':group,'text':text,'parse_mode':'HTML','link_preview_options':{'is_disabled':True}})
        except Exception:
            # A transport failure can occur after delivery; do not blindly duplicate.
            LOG.exception('Payment audit delivery requires review for invoice %s',row['invoice'])
            await pool.execute("UPDATE descarga_payment_notices SET state='review' WHERE invoice=$1",row['invoice'])
        else:
            await pool.execute("UPDATE descarga_payment_notices SET state='sent' WHERE invoice=$1",row['invoice'])
