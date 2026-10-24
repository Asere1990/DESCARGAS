"""Editable Mini App copy and logos; code defaults reconcile only at web startup."""
import hashlib
import html
import io
import json
from pathlib import Path
import re
import time
from PIL import Image

FILE=Path(__file__).with_name('miniapp_texts.json')
WEB=Path(__file__).with_name('web')
MODES={'cuenta':'CREAR CUENTA','creadores':'PROTECCIÓN A CREADORES'}
CATEGORIES={'cabecera':'Cabecera y nombre','titulos':'Títulos y recuadros','botones':'Botones','campos':'Campos y etiquetas','avisos':'Avisos y mensajes','popups':'Popups y confirmaciones','menus':'Menús','desplegables':'Textos desplegables','paises':'Nombres de países','servidor':'Respuestas y errores'}
SCHEMA='''
CREATE TABLE IF NOT EXISTS descarga_miniapp_copy(
 bot_id BIGINT NOT NULL,mode TEXT NOT NULL,key TEXT NOT NULL,
 source_text TEXT NOT NULL,value TEXT NOT NULL,updated DOUBLE PRECISION NOT NULL,
 actor BIGINT NOT NULL DEFAULT 0,PRIMARY KEY(bot_id,mode,key));
CREATE TABLE IF NOT EXISTS descarga_miniapp_logo(
 bot_id BIGINT NOT NULL,mode TEXT NOT NULL,source_hash TEXT NOT NULL,
 data BYTEA NOT NULL,mime TEXT NOT NULL,updated DOUBLE PRECISION NOT NULL,
 actor BIGINT NOT NULL DEFAULT 0,PRIMARY KEY(bot_id,mode));
CREATE TABLE IF NOT EXISTS descarga_miniapp_edits(
 bot_id BIGINT NOT NULL,update_id BIGINT NOT NULL,PRIMARY KEY(bot_id,update_id));
'''

def definitions():
    data=json.loads(FILE.read_text())
    if set(data)!=set(MODES):raise ValueError('Catálogo de miniapps no válido.')
    for entries in data.values():
        for key,item in entries.items():
            if not re.fullmatch(r'[a-zA-Z0-9_.-]+',key) or not isinstance(item.get('text'),str) or item.get('category') not in CATEGORIES:raise ValueError('Campo de miniapp no válido.')
    return data

def image_data(data):
    if len(data)>5*1024*1024:raise ValueError('El logo debe ocupar como máximo 5 MB.')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width>4096 or image.height>4096:raise ValueError('El logo debe medir como máximo 4096 × 4096.')
            mime={'JPEG':'image/jpeg','PNG':'image/png','WEBP':'image/webp','GIF':'image/gif'}.get(image.format)
            if not mime:raise ValueError('Usa una imagen PNG, JPG, WEBP o GIF.')
            image.verify()
    except ValueError:raise
    except Exception as exc:raise ValueError('No se pudo leer la imagen del logo.') from exc
    return data,mime

def source_logo(mode):return image_data((WEB/('logo-'+mode+'.jpg')).read_bytes())

async def activate(repo,bot_id):
    """Changed code wins only for changed source fields, preserving all other edits.

    This runs once at web startup, never on reads. Old draining instances cannot
    restore their defaults. The same lock serializes editor writes and deployments.
    """
    data=definitions();now=time.time()
    async with repo.pool.acquire() as conn,conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(763,$1)',bot_id%(2**31))
        await conn.executemany('''INSERT INTO descarga_miniapp_copy(bot_id,mode,key,source_text,value,updated,actor)
            VALUES($1,$2,$3,$4,$4,$5,0) ON CONFLICT(bot_id,mode,key) DO UPDATE SET
            source_text=excluded.source_text,value=excluded.value,updated=excluded.updated,actor=0
            WHERE descarga_miniapp_copy.source_text IS DISTINCT FROM excluded.source_text''',
            [(bot_id,mode,key,item['text'],now) for mode,entries in data.items() for key,item in entries.items()])
        for mode in MODES:
            content,mime=source_logo(mode);digest=hashlib.sha256(content).hexdigest()
            await conn.execute('''INSERT INTO descarga_miniapp_logo VALUES($1,$2,$3,$4,$5,$6,0)
                ON CONFLICT(bot_id,mode) DO UPDATE SET source_hash=excluded.source_hash,data=excluded.data,mime=excluded.mime,updated=excluded.updated,actor=0
                WHERE descarga_miniapp_logo.source_hash IS DISTINCT FROM excluded.source_hash''',bot_id,mode,digest,content,mime,now)

async def values(repo,bot_id,mode):
    base={key:item['text'] for key,item in definitions()[mode].items()}
    rows=await repo.pool.fetch('SELECT key,value FROM descarga_miniapp_copy WHERE bot_id=$1 AND mode=$2',bot_id,mode)
    base.update((r['key'],r['value']) for r in rows if r['key'] in base)
    return base

async def effective(service,mode):
    reader=getattr(getattr(service,'repo',None),'miniapp_copy_values',None)
    if reader and service.ready:return await reader(service.bot_id,mode)
    return {k:item['text'] for k,item in definitions()[mode].items()}

async def read_logo(repo,bot_id,mode):
    row=await repo.pool.fetchrow('SELECT data,mime FROM descarga_miniapp_logo WHERE bot_id=$1 AND mode=$2',bot_id,mode)
    return (bytes(row['data']),row['mime']) if row else source_logo(mode)

async def save(repo,bot_id,actor,mode,key,value,update_id):
    if mode not in MODES:raise ValueError('Miniapp no válida.')
    item=definitions()[mode].get(key)
    if not item:raise ValueError('El texto cambió en el código. Vuelve a abrir EDITAR MINIAPP.')
    maximum=item.get('maximum',4096)
    if len(value.encode('utf-16-le'))//2>maximum or (not value.strip() and item['text']):raise ValueError(f'Escribe entre 1 y {maximum} caracteres para este texto.')
    async with repo.pool.acquire() as conn,conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(763,$1)',bot_id%(2**31))
        if update_id is not None:
            reserved=await conn.fetchval('INSERT INTO descarga_miniapp_edits VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING 1',bot_id,update_id)
            if not reserved:return False
        updated=await conn.fetchval('UPDATE descarga_miniapp_copy SET value=$4,updated=$5,actor=$6 WHERE bot_id=$1 AND mode=$2 AND key=$3 RETURNING 1',bot_id,mode,key,value,time.time(),actor)
        if not updated:raise ValueError('El editor aún se está inicializando. Inténtalo en unos segundos.')
    return True

async def save_logo(repo,bot_id,actor,mode,data,update_id):
    if mode not in MODES:raise ValueError('Miniapp no válida.')
    content,mime=image_data(data)
    async with repo.pool.acquire() as conn,conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(763,$1)',bot_id%(2**31))
        if update_id is not None:
            reserved=await conn.fetchval('INSERT INTO descarga_miniapp_edits VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING 1',bot_id,update_id)
            if not reserved:return False
        updated=await conn.fetchval('UPDATE descarga_miniapp_logo SET data=$3,mime=$4,updated=$5,actor=$6 WHERE bot_id=$1 AND mode=$2 RETURNING 1',bot_id,mode,content,mime,time.time(),actor)
        if not updated:raise ValueError('El editor aún se está inicializando.')
    return True

def render_index(template,texts):
    template=re.sub(r'\{\{copy:([a-zA-Z0-9_.-]+)\}\}',lambda m:html.escape(texts.get(m[1],'')),template)
    pattern=r'data-copy-(placeholder|aria-label|alt)="([a-zA-Z0-9_.-]+)"\s+\1=""'
    template=re.sub(pattern,lambda m:f'{m[1]}="{html.escape(texts.get(m[2],""),quote=True)}"',template)
    payload=json.dumps(texts,ensure_ascii=False).replace('<','\\u003c').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    return template.replace('</head>','<script type="application/json" id="miniapp-copy">'+payload+'</script></head>')

def translated(message,entries,texts):
    if not isinstance(message,str):return message
    for key,item in entries.items():
        original=item.get('match')
        if not original:continue
        names=re.findall(r'\{([a-zA-Z_]+)\}',original)
        pattern=re.escape(original)
        for name in names:pattern=pattern.replace(re.escape('{'+name+'}'),f'(?P<{name}>.+?)')
        match=re.fullmatch(pattern,message,re.S)
        if match:
            value=texts.get(key,item['text'])
            return re.sub(r'\{([A-Za-z_]+)\}',lambda m:match.groupdict().get(m[1],m[0]),value)
    return message
