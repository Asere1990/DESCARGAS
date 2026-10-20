"""Bounded admin-only reports, isolated from download lanes and polling."""
import asyncio,html,re,time,unicodedata
import asyncpg
from babel import Locale
from .moderation import authorized

LOCALE=Locale('es')
def country_name(code):return str(LOCALE.territories.get(code,code or 'Sin datos'))
def normalize(value):return ''.join(c for c in unicodedata.normalize('NFD',value.casefold()) if not unicodedata.combining(c)).strip()
def country_code(value):
    value=normalize(value)
    for code,name in LOCALE.territories.items():
        if len(code)==2 and value in (normalize(code),normalize(str(name))):return code
    return None

def person(uid,name):return f'<a href="tg://user?id={uid}">{html.escape(name or str(uid))}</a>'

HELP='''<b>COMANDOS DE DESCARGAS</b>
/start — Abrir el bot y ver la membresía.
/estado — Comprobar la conexión.
/suscripcion — Ver o gestionar la membresía.
/cancelar_membresia — Cancelar su renovación.
/desconectar — Cerrar la sesión de Telegram.

<b>SOLO ADMINISTRADORES · privado o ALMACEN</b>
/datos — Panel de estadísticas.
/usuario o /usuarios — Los 100 usuarios con más descargas.
/grupo o /grupos — Los 20 grupos/canales con más descargas.
/telefono Cuba o /telefonos CU — Los 100 usuarios de ese país.
Después de una lista, escribe su número (ejemplo: 47) para abrir la ficha. La selección dura 15 minutos y solo sirve en ese chat para ese administrador.
/ban 1609522430 — Bloquear un usuario.
/ban -1001234567890 — Bloquear un grupo/canal.
/unban ID — Retirar un bloqueo manual.
En ALMACEN: responde al contenido con /ban o /unban para gestionar al usuario y al origen vinculados.
/pay 1609522430 o /pay -1001234567890 — Exigir membresía.
/lock ID — Entregar sin copiar a ALMACEN ni Dropbox.
/unlock ID — Volver a permitir esas copias.
/comando o /comandos — Esta ayuda.

<b>ARCHIVO protegidos</b>
Un ID de grupo/canal por línea; admite comentarios con #. No acepta descargas de esos orígenes.

Las descargas se cuentan por archivo entregado: un álbum de 10 elementos cuenta como 10. Los aciertos de caché también cuentan. Las cifras antiguas solo incluyen el historial aún disponible; los contadores nuevos no caducan.'''

class AdminPanel:
    def __init__(self,service):self.s=service;self.pool=None;self.tasks=set();self.slots=asyncio.Semaphore(2)
    async def start(self):
        self.pool=await asyncpg.create_pool(self.s.repo.url,min_size=0,max_size=2,command_timeout=8,server_settings={'statement_timeout':'5000'})
    async def close(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        if self.pool:await self.pool.close()
    async def update(self,update):
        query=update.get('callback_query',{});message=query.get('message',{}) if query else update.get('message',{})
        text=message.get('text','').strip() if not query else query.get('data','')
        match=re.fullmatch(r'/(datos|usuarios?|grupos?|comandos?|telefonos?)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?',text,re.S)
        callback=query and text.startswith('stats:')
        numeric=not query and text.isdecimal() and len(text)<=3
        if not (match or callback or numeric):return False
        actor=query.get('from',{}) if query else message.get('from',{});uid=actor.get('id');chat=message.get('chat',{});chat_id=chat.get('id')
        if not authorized(uid) or actor.get('is_bot') or message.get('sender_chat') or not (chat.get('type')=='private' or chat_id==self.s.almacen):
            if query:await self.s.api('answerCallbackQuery',{'callback_query_id':query['id'],'text':'Solo administradores autorizados.'})
            return not numeric
        if query:await self.s.api('answerCallbackQuery',{'callback_query_id':query['id']})
        if len(self.tasks)>=20:
            await self.send(chat_id,'Hay varias consultas administrativas en curso. Inténtalo en unos segundos.');return True
        op,arg=(match.group(1),match.group(2) or '') if match else (text.split(':',1)[1], '') if callback else ('selection',text)
        task=asyncio.create_task(self.run(uid,chat_id,op,arg));self.tasks.add(task);task.add_done_callback(self.tasks.discard)
        return True
    async def send(self,chat,text,buttons=None):
        await self.s.api('sendMessage',{'chat_id':chat,'text':text,'parse_mode':'HTML',**({'reply_markup':{'inline_keyboard':buttons}} if buttons else {})})
    async def run(self,uid,chat,op,arg):
        async with self.slots:
            try:
                if not authorized(uid):return
                if op=='datos':
                    await self.send(chat,'<b>📊 DATOS DE DESCARGAS</b>',[[{'text':label,'callback_data':'stats:'+value,'style':'primary'}] for label,value in [('Usuario con más descargas','topuser'),('Grupo o canal con más descargas','topgroup'),('Teléfonos · principales países','countries'),('Mis ganancias','earnings')]])
                elif op in ('comando','comandos'):await self.send(chat,HELP)
                elif op in ('usuario','usuarios','topuser','telefono','telefonos'):
                    country=country_code(arg) if op.startswith('telefono') else None
                    if op.startswith('telefono') and not country:return await self.send(chat,'Indica el país. Ejemplo: <code>/telefono Cuba</code> o <code>/telefonos ES</code>.')
                    await self.users(uid,chat,country,1 if op=='topuser' else 100)
                elif op in ('grupo','grupos','topgroup'):await self.groups(chat,1 if op=='topgroup' else 20)
                elif op=='countries':
                    rows=await self.pool.fetch('SELECT country,count(*) AS total FROM descarga_directory WHERE bot_id=$1 AND country<>$2 GROUP BY country ORDER BY total DESC,country LIMIT 5',self.s.bot_id,'ZZ')
                    await self.send(chat,'<b>🌍 PAÍSES CON MÁS USUARIOS</b>\n'+('\n'.join(f'{i}. {html.escape(country_name(r["country"]))}: {r["total"]}' for i,r in enumerate(rows,1)) or 'Sin datos todavía.')+'\n\nPaís inferido por el prefijo telefónico, no por ubicación actual.')
                elif op=='earnings':await self.earnings(chat)
                elif op=='selection':
                    row=await self.pool.fetchrow('SELECT payload FROM descarga_admin_selections WHERE bot_id=$1 AND admin=$2 AND chat=$3 AND expires>$4',self.s.bot_id,uid,chat,time.time())
                    if row:
                        ids=self.s.repo.decode(row['payload'])['ids'];number=int(arg)
                        if 1<=number<=len(ids):await self.detail(chat,ids[number-1])
                        else:await self.send(chat,'Ese número no está en tu lista. Vuelve a consultar /usuarios o /telefono país.')
            except asyncio.CancelledError:raise
            except Exception:await self.send(chat,'No se pudo completar la consulta. Inténtalo de nuevo en unos segundos.')
    async def users(self,admin,chat,country,limit):
        rows=await self.pool.fetch('''SELECT d.uid,d.name,d.country,COALESCE(t.total,0) AS total,a.profile,
            EXISTS(SELECT 1 FROM descarga_memberships m WHERE m.bot_id=d.bot_id AND m.uid=d.uid AND m.confirmed AND m.paid_until>$4 AND m.state NOT IN ('refunded','revoked','incomplete_expired')) AS paid
            FROM descarga_directory d LEFT JOIN descarga_user_totals t ON t.bot_id=d.bot_id AND t.uid=d.uid LEFT JOIN descarga_accounts a ON a.uid=d.uid
            WHERE d.bot_id=$1 AND ($2::text IS NULL OR d.country=$2) ORDER BY total DESC,d.uid LIMIT $3''',self.s.bot_id,country,limit,time.time())
        await self.pool.execute('INSERT INTO descarga_admin_selections VALUES($1,$2,$3,$4,$5) ON CONFLICT(bot_id,admin,chat) DO UPDATE SET payload=excluded.payload,expires=excluded.expires',self.s.bot_id,admin,chat,self.s.repo.encode({'ids':[r['uid'] for r in rows]}),time.time()+900)
        entries=[]
        for i,r in enumerate(rows,1):
            name=self.s.repo.decode(r['name'])['name'] if r['name'] else str(r['uid']);profile=self.s.repo.decode(r['profile']) if r['profile'] else {}
            entries.append(f'{i}. {"⭐️ " if r["paid"] else ""}{person(r["uid"],name)}\nID: <code>{r["uid"]}</code> · {html.escape(profile.get("phone") or "Sin teléfono")}\n{html.escape(country_name(r["country"]))} · Descargas: {r["total"]}')
        await self.pages(chat,'<b>USUARIOS · '+html.escape(country_name(country) if country else 'TODOS')+'</b>',entries)
    async def pages(self,chat,title,entries):
        text=title+'\n\n'
        for entry in entries:
            if len(text)+len(entry)>3400:await self.send(chat,text);text=title+'\n\n'
            text+=entry+'\n\n'
        await self.send(chat,text if entries else title+'\nSin datos todavía.')
    async def groups(self,chat,limit):
        rows=await self.pool.fetch('SELECT source,total,name FROM descarga_source_totals WHERE bot_id=$1 ORDER BY total DESC,source LIMIT $2',self.s.bot_id,limit)
        await self.pages(chat,'<b>GRUPOS Y CANALES</b>',[f'{i}. {html.escape(self.s.repo.decode(r["name"])["name"] if r["name"] else "Nombre no disponible")}\nID: <code>{r["source"]}</code> · Descargas: {r["total"]}' for i,r in enumerate(rows,1)])
    async def detail(self,chat,uid):
        row=await self.pool.fetchrow('SELECT d.name,d.country,a.profile,COALESCE(t.total,0) total FROM descarga_directory d LEFT JOIN descarga_accounts a ON a.uid=d.uid LEFT JOIN descarga_user_totals t ON t.bot_id=d.bot_id AND t.uid=d.uid WHERE d.bot_id=$1 AND d.uid=$2',self.s.bot_id,uid)
        if not row:return await self.send(chat,'Usuario no disponible.')
        profile=self.s.repo.decode(row['profile']) if row['profile'] else {};name=self.s.repo.decode(row['name'])['name'] if row['name'] else str(uid)
        entries=[person(uid,name),f'Telegram ID: <code>{uid}</code>',html.escape('Teléfono: '+str(profile.get('phone') or 'Sin datos')),html.escape('País: '+country_name(row['country'])),f'Descargas: {row["total"]}']
        buyer=await self.pool.fetchrow('SELECT payload FROM descarga_buyer_profiles WHERE bot_id=$1 AND uid=$2 ORDER BY invoice DESC LIMIT 1',self.s.bot_id,uid)
        if buyer:
            data=self.s.repo.decode(buyer['payload']);address=data.get('billing_address') or {}
            entries+=['<b>COMPRA EN STRIPE</b>',html.escape('Nombre: '+str(data.get('name') or 'Sin datos')),html.escape('Email: '+str(data.get('email') or 'Sin datos')),html.escape('Dirección: '+(', '.join(str(address.get(k)) for k in ('line1','line2','city','state','postal_code','country') if address.get(k)) or 'Sin datos'))]
        groups=await self.pool.fetch('SELECT u.source,u.total,s.name FROM descarga_user_sources u LEFT JOIN descarga_source_totals s ON s.bot_id=u.bot_id AND s.source=u.source WHERE u.bot_id=$1 AND u.uid=$2 ORDER BY u.total DESC,u.source LIMIT 100',self.s.bot_id,uid)
        entries+=['<b>ORÍGENES · hasta 100</b>']+[f'<code>{r["source"]}</code> · {r["total"]} · '+html.escape(self.s.repo.decode(r['name'])['name'] if r['name'] else 'Nombre no disponible') for r in groups]
        await self.pages(chat,'<b>FICHA DEL USUARIO</b>',entries)
    async def earnings(self,chat):
        stars=await self.pool.fetchval('SELECT count(*) FROM descarga_star_receipts WHERE bot_id=$1',self.s.bot_id)
        refunds=await self.pool.fetchval("SELECT count(*) FROM descarga_dispute_blocks WHERE bot_id=$1 AND dispute LIKE 'stars:%'",self.s.bot_id)
        invoices=await self.pool.fetchval("SELECT count(*) FROM descarga_buyer_profiles WHERE bot_id=$1 AND invoice LIKE 'in_%'",self.s.bot_id)
        await self.send(chat,f'<b>💰 MIS GANANCIAS · REGISTROS DEL BOT</b>\n\nStars recibidas: {stars*500} ⭐\nReembolsos confirmados: {refunds*500} ⭐\nStripe · cobros registrados: ${invoices*6.99:.2f} USD\n\nImportes brutos registrados, no saldo disponible ni beneficio neto. No descuentan comisiones, impuestos ni disputas de Stripe. Stars y dólares se muestran por separado. El historial previo puede estar incompleto.')
