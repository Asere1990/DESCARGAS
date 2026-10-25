"""Persistent additive policy lists, shared by web and workers."""
import asyncio
import html
import json
import logging
import re
import time
NAMES={'permisos.txt','cuentas.txt','protegidos'}
EXTRA={}
KEY='managed-policy-lists-v1'

def extra(name):return set(EXTRA.get(name,[]))

def parse(name,text):
    if name not in NAMES:raise ValueError('Lista no válida.')
    parts=[v.strip() for v in text.split(',')]
    if not parts or len(parts)>200 or any(not re.fullmatch(r'-?[1-9][0-9]{0,18}',v) for v in parts):raise ValueError('Envía los IDs separados por comas, sin nombres ni enlaces.')
    values={int(v) for v in parts}
    if any(abs(v)>=2**63 or (v>=0 if name=='protegidos' else v<=0) for v in values):raise ValueError('PROTEGIDOS requiere IDs negativos de grupos/canales; las otras listas requieren IDs positivos de usuarios.')
    return values

async def refresh(s):
    global EXTRA
    raw=await s.repo.setting(KEY)
    data=json.loads(raw or '{}')
    EXTRA={name:sorted(parse(name,','.join(map(str,values)))) if values else [] for name,values in data.items() if name in NAMES}

async def sync(s):
    while not s.stopping:
        try:await refresh(s)
        except asyncio.CancelledError:raise
        except Exception as exc:logging.getLogger(__name__).warning('Policy list refresh delayed (%s)',type(exc).__name__)
        await asyncio.sleep(3)

async def append(s,actor,name,text):
    from .creator_access import police
    if not police(actor):raise ValueError('Solo admins.txt puede editar estas listas.')
    values=parse(name,text)
    async with s.repo.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(753,1)')
            raw=await conn.fetchval('SELECT value FROM descarga_settings WHERE key=$1',KEY)
            data=json.loads(raw or '{}');before=set(data.get(name,[]))
            data[name]=sorted(before|values)
            await conn.execute('INSERT INTO descarga_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=excluded.value',KEY,json.dumps(data))
    await refresh(s)
    return values-before

async def handle(panel,update):
    from .creator_access import police
    q=update.get('callback_query');m=q.get('message',{}) if q else update.get('message',{})
    actor=(q.get('from',{}) if q else m.get('from',{})).get('id')
    if not actor or m.get('chat',{}).get('type')!='private' or m['chat'].get('id')!=actor:return False
    text=q.get('data','') if q else m.get('text','').strip()
    key=f'policy-input:{panel.s.bot_id}:{actor}'
    if q and text.startswith('policy:'):
        await panel.s.api('answerCallbackQuery',{'callback_query_id':q['id']})
        if not police(actor):return True
        name=text.split(':',1)[1]
        if name not in NAMES:return True
        from .miniapp_editor import clear
        await clear(panel.s,actor)
        await panel.s.repo.set_setting(key,json.dumps({'name':name,'expires':time.time()+1800}))
        await panel.send(actor,f'<b>{html.escape(name.upper())}</b>\nEnvía los IDs que quieres agregar separados por comas.\n'+('Ejemplo: -1001234567890,-1009876543210' if name=='protegidos' else 'Ejemplo: 123456789,987654321')+'\nNo se borrarán los existentes. /cancelar para salir.')
        return True
    if q:return False
    raw=await panel.s.repo.setting(key)
    if not raw:return False
    if text.startswith('/'):
        await panel.s.repo.set_setting(key,'')
        if text.split()[0]=='/cancelar':
            await panel.send(actor,'Edición cancelada.');return True
        return False
    if not police(actor):return True
    data=json.loads(raw)
    if data['expires']<time.time():
        await panel.s.repo.set_setting(key,'');await panel.send(actor,'La edición venció. Abre /menu otra vez.');return True
    try:
        values=await append(panel.s,actor,data['name'],text)
        await panel.s.repo.set_setting(key,'')
        await panel.send(actor,f'✅ {html.escape(data["name"].upper())} actualizada.\nIDs guardados: '+html.escape(', '.join(map(str,sorted(parse(data['name'],text)))))+'\nLos repetidos no se duplican. Los cambios se aplican a todo el sistema en unos segundos.')
    except ValueError as exc:await panel.send(actor,html.escape(str(exc)))
    return True
