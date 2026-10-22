"""Persistent, administrator-owned one-link-at-a-time archive controls."""
import asyncio
import json
import re
import time
import uuid
from telethon import types,utils,errors
from .moderation import authorized,ensure_allowed
from .protected import require_entity
from .paid_media import purchased_items
from .link_listing import media_label

SCHEMA='''
CREATE TABLE IF NOT EXISTS descarga_manual_boards(
 bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,source BIGINT NOT NULL,
 actor BIGINT NOT NULL,token TEXT NOT NULL UNIQUE,payload BYTEA NOT NULL,
 position INTEGER NOT NULL DEFAULT 0,message_id BIGINT,busy BOOLEAN NOT NULL DEFAULT FALSE,
 closed BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(bot_id,uid,source));
ALTER TABLE descarga_group_exports ADD COLUMN IF NOT EXISTS manual_token TEXT;
'''


def link(source,mid):return f'https://t.me/c/{str(source)[4:]}/{mid}'
def context_key(s,actor):return f'manual-selection:{s.bot_id}:{actor}'
def last_key(s,actor,uid):return f'manual-last:{s.bot_id}:{actor}:{uid}'


def entry(message,items):
    result={'mid':message.id,'ids':list(dict.fromkeys(m.id for m in items)),
        'photos':0,'videos':0,'gifs':0,'elements':0,'files':[],'status':'pending'}
    for item in items:
        label=media_label(item)
        if not label:continue
        result['elements']+=1
        result['gifs' if label=='GIF' else 'photos' if label in ('Foto','Imagen') else 'videos']+=1
        media=getattr(item,'photo',None) or getattr(item,'document',None)
        result['files'].append(['photo' if getattr(item,'photo',None) else 'document',media.id])
    return result


def advance(data,position):
    while position<len(data['entries']) and data['entries'][position]['status'] in ('saved','skipped'):
        position+=1
    return position


def presentation(row,data):
    entries=data['entries'];position=row['position']
    saved=[(i,e) for i,e in enumerate(entries,1) if e['status']=='saved']
    text=(f'{data["title"]}\nID: {row["source"]}\nCuenta: {row["uid"]}\n\n'
        f'Total fotos: {sum(e["photos"] for e in entries)}\n'
        f'Total videos: {sum(e["videos"] for e in entries)}\n'
        f'Total GIF: {sum(e["gifs"] for e in entries)}\n'
        f'Total de enlaces: {len(entries)}\n'
        f'Fotos descargadas: {sum(e["photos"] for _,e in saved)}\n'
        f'Videos descargados: {sum(e["videos"] for _,e in saved)}\n'
        f'Enlaces descargados: {len(saved)}\n'
        f'Último enlace descargado: {data.get("last_downloaded",max((i for i,_ in saved),default=0))}\n')
    if position<len(entries):
        current=entries[position]
        text+=f'\n{position+1}: {link(row["source"],current["mid"])} ({current["elements"]} elementos)'
    else:text+='\n✅ No quedan enlaces pendientes en esta lista.'
    if data.get('notice'):text+='\n\n'+data['notice'][:500]
    if row['busy']:text+='\n\n⏳ Descargando el enlace seleccionado…'
    buttons=[]
    if position<len(entries):
        buttons.append([{'text':'⏳ DESCARGANDO' if row['busy'] else '𝐃𝐄𝐒𝐂𝐀𝐑𝐆𝐀𝐑','style':'success',
            'callback_data':f'manual:{"w" if row["busy"] else "d"}:{row["token"]}:{position}'}])
    buttons.append([{'text':'𝐀𝐓𝐑Á𝐒','style':'danger','callback_data':f'manual:b:{row["uid"]}'},
        {'text':'𝐂𝐄𝐑𝐑𝐀𝐑','style':'danger','callback_data':f'manual:c:{row["token"]}:{position}'}])
    return text,buttons


async def _render(s,row,new=False):
    data=s.repo.decode(row['payload']);text,buttons=presentation(row,data)
    if row['closed']:return
    if row['message_id'] and not new:
        try:
            await s.api('editMessageText',{'chat_id':row['actor'],'message_id':row['message_id'],'text':text,
                'reply_markup':{'inline_keyboard':buttons},'link_preview_options':{'is_disabled':True}})
            return
        except Exception as exc:
            if 'not modified' in str(exc).lower():return
            # Preserve durable progress even if Telegram cannot edit the old panel.
    sent=await s.api('sendMessage',{'chat_id':row['actor'],'text':text,'reply_markup':{'inline_keyboard':buttons},'link_preview_options':{'is_disabled':True}})
    await s.repo.pool.execute('UPDATE descarga_manual_boards SET message_id=$3 WHERE bot_id=$1 AND token=$2',s.bot_id,row['token'],sent['message_id'])


async def render(s,row,new=False):
    # Serialize Telegram edits and re-read committed state, avoiding a fast worker
    # being overwritten by a delayed "downloading" edit from the click handler.
    async with s.repo.pool.acquire() as conn:
        await conn.execute('SELECT pg_advisory_lock(752,hashtext($1))',row['token'])
        try:
            current=await conn.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,row['token'])
            if current:await _render(s,current,new)
        finally:
            await conn.execute('SELECT pg_advisory_unlock(752,hashtext($1))',row['token'])


async def cooldown(s,uid):
    until=max(float(await s.repo.setting(f'link-list-wait:{s.bot_id}:{uid}') or 0),
        await s.repo.pool.fetchval('SELECT max(resume_at) FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2',s.bot_id,uid) or 0)
    if until>time.time():raise ValueError(f'Telegram pide esperar {int(until-time.time())+1} segundos.')


async def collect(s,actor,uid,source):
    """Metadata only, releasing the account between bounded history pages."""
    if not str(source).startswith('-100'):raise ValueError('Se necesita el ID -100 de un supergrupo o canal.')
    await cooldown(s,uid)
    account=await s.repo.account(uid)
    if not account or not account['session']:raise ValueError('Esa cuenta no está conectada.')
    data={'title':str(source),'generation':account['generation'],'entries':[]}
    grouped={};cursor=0
    inventory=await s.repo.pool.fetch('SELECT kind,file_id FROM descarga_export_files WHERE bot_id=$1 AND almacen=$2 AND source=$3',s.bot_id,s.almacen,source)
    saved={(r['kind'],r['file_id']) for r in inventory}
    while not s.stopping:
        async with s.repo.account_lock(uid,wait=False):
            current=await s.repo.account(uid)
            if not authorized(actor) or not current or not current['session'] or current['generation']!=data['generation']:
                raise ValueError('La cuenta o la autorización cambió; vuelve a consultar.')
            await ensure_allowed(s,uid,source)
            client=s.client(await s.get_session(uid));client.flood_sleep_threshold=0
            try:
                await client.connect()
                if not await client.is_user_authorized():raise ValueError('Conecta de nuevo esa cuenta.')
                entity=await s.resolve(client,source)
                if not isinstance(entity,types.Channel) or utils.get_peer_id(entity)!=source:raise ValueError('No se pudo verificar el origen.')
                require_entity(entity,uid)
                data['title']=' '.join(entity.title.split())[:128]
                await s.repo.pace(f'enlace-read:{uid}',3.1)
                page=[m async for m in client.iter_messages(entity,min_id=cursor,reverse=True,limit=100)]
            finally:await client.disconnect()
        for message in page:
            cursor=max(cursor,message.id)
            try:items=purchased_items(message)
            except ValueError:continue
            piece=entry(message,items if items is not None else [message])
            if not piece['elements']:continue
            key=getattr(message,'grouped_id',None) or ('single',message.id)
            existing=grouped.get(key)
            if existing is None:
                grouped[key]=piece;data['entries'].append(piece)
            else:
                existing['ids']+=piece['ids'];existing['files']+=piece['files']
                for field in ('photos','videos','gifs','elements'):existing[field]+=piece[field]
        if len(page)<100:break
    if s.stopping:raise ValueError('El servicio se está reiniciando. Vuelve a abrir el panel.')
    for e in data['entries']:
        if e['files'] and all(tuple(f) in saved for f in e['files']):e['status']='saved'
    return data


async def open_board(s,actor,uid,source,refresh=False):
    if not authorized(actor):return
    if not s.almacen or source==s.almacen:raise ValueError('Elige un origen diferente a ALMACEN.')
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND uid=$2 AND source=$3',s.bot_id,uid,source)
    account=await s.repo.account(uid)
    if not account or not account['session']:raise ValueError('Esa cuenta no está conectada.')
    finished=row and row['position']>=len(s.repo.decode(row['payload'])['entries'])
    if row and s.repo.decode(row['payload'])['generation']==account['generation'] and (row['busy'] or not (refresh or finished)):
        await s.repo.pool.execute('UPDATE descarga_manual_boards SET actor=$4,closed=FALSE WHERE bot_id=$1 AND uid=$2 AND source=$3',s.bot_id,uid,source,actor)
        row=dict(row,actor=actor,closed=False)
        if row['busy']:
            job=await s.repo.pool.fetchrow('SELECT state,manual_token FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2 AND source=$3',s.bot_id,uid,source)
            if not job or job['state']!='running' or job['manual_token']!=row['token']:
                await s.repo.pool.execute('UPDATE descarga_manual_boards SET busy=FALSE WHERE bot_id=$1 AND token=$2',s.bot_id,row['token'])
                row['busy']=False
    else:
        await s.tell(actor,'🔎 Preparando la botonera y contando la multimedia del historial accesible…')
        try:data=await collect(s,actor,uid,source)
        except errors.FloodWaitError as exc:
            await s.repo.set_setting(f'link-list-wait:{s.bot_id}:{uid}',time.time()+max(1,exc.seconds)+10)
            raise ValueError(f'Telegram pide esperar {exc.seconds} segundos. Después repite /enlace {uid} {source}. No se cambió el avance guardado.') from exc
        if row:
            skipped={e['mid']:e for e in s.repo.decode(row['payload'])['entries'] if e['status']=='skipped'}
            for e in data['entries']:
                if e['mid'] in skipped and e['files']==skipped[e['mid']]['files']:e['status']='skipped'
        token=uuid.uuid4().hex[:16]
        row=await s.repo.pool.fetchrow('''INSERT INTO descarga_manual_boards(bot_id,uid,source,actor,token,payload,position)
            VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(bot_id,uid,source) DO UPDATE
            SET actor=excluded.actor,token=excluded.token,payload=excluded.payload,position=excluded.position,message_id=NULL,closed=FALSE,busy=FALSE
            WHERE NOT descarga_manual_boards.busy RETURNING *''',s.bot_id,uid,source,actor,token,s.repo.encode(data),advance(data,0))
        if not row:raise ValueError('Hay una descarga en curso. Espera antes de cambiar la sesión del panel.')
    if not await s.repo.setting(last_key(s,actor,uid)):
        await s.repo.set_setting(last_key(s,actor,uid),source)
    await s.repo.set_setting(context_key(s,actor),'')
    await render(s,row,new=True)


async def open_command(s,actor,arg):
    match=re.fullmatch(r'([1-9][0-9]{0,18})\s+(-[1-9][0-9]{0,18})\s*',arg)
    if not match or int(match[1])>=2**63 or int(match[2])<=-(2**63):
        return await s.tell(actor,'Uso: /enlace telegram_id group_id (también canal_id).')
    await open_board(s,actor,int(match[1]),int(match[2]),refresh=True)


async def menu(s,actor,uid):
    if not authorized(actor):return
    account=await s.repo.account(uid)
    if not account or not account['session']:raise ValueError('Esa cuenta no está conectada.')
    await s.repo.set_setting(context_key(s,actor),'')
    await s.api('sendMessage',{'chat_id':actor,'text':f'𝐌𝐄𝐍Ú 𝐃𝐄 𝐃𝐄𝐒𝐂𝐀𝐑𝐆𝐀𝐒\nCuenta: {uid}',
        'reply_markup':{'inline_keyboard':[
            [{'text':'𝐆𝐑𝐔𝐏𝐎𝐒','style':'primary','callback_data':f'manual:g:{uid}'},
             {'text':'𝐂𝐀𝐍𝐀𝐋𝐄𝐒','style':'primary','callback_data':f'manual:h:{uid}'}],
            [{'text':'Ú𝐋𝐓𝐈𝐌𝐀 𝐃𝐄𝐒𝐂𝐀𝐑𝐆𝐀','style':'success','callback_data':f'manual:l:{uid}'}]]}})


async def destinations(s,actor,uid,channels):
    from .account_directory import destinations as existing
    from .account_chat import selection_key
    # Reuse the existing protected, generation-bound dialog snapshot.
    key=selection_key(s,actor,uid)
    account=await s.repo.account(uid)
    cached=await s.repo.setting(key)
    data=json.loads(cached) if cached else {}
    if not account or not account['session']:raise ValueError('Esa cuenta no está conectada.')
    if data.get('expires',0)<=time.time() or data.get('generation')!=account['generation'] or not data.get('snapshot'):
        await cooldown(s,uid)
        # Same loader, suppressing its old textual command listing only.
        class Snapshot:
            def __getattr__(self,name):return getattr(s,name)
            async def tell(self,*args,**kwargs):pass
        await existing(Snapshot(),actor,str(uid))
        cached=await s.repo.setting(key);data=json.loads(cached) if cached else {}
    if not data.get('snapshot') or data.get('expires',0)<=time.time() or data.get('generation')!=account['generation']:raise ValueError('No se pudo obtener la lista actual. Inténtalo de nuevo.')
    entries=s.repo.decode(bytes.fromhex(data['snapshot']))['entries']
    entries=[e for e in entries if (e['kind']=='Canal')==channels and e['id']!=s.almacen]
    await s.repo.set_setting(context_key(s,actor),json.dumps({'uid':uid,'entries':entries,'generation':account['generation'],'expires':time.time()+1800}))
    header=f'{"𝐂𝐀𝐍𝐀𝐋𝐄𝐒" if channels else "𝐆𝐑𝐔𝐏𝐎𝐒"} · Cuenta {uid}\nEscribe el número para abrir la descarga.\n\n'
    text=header
    for i,e in enumerate(entries,1):
        line=f'{i}. {e["name"]}\nID: {e["id"]}\n'
        if len((text+line).encode('utf-16-le'))//2>3500:
            await s.tell(actor,text);text=header
        text+=line
    await s.api('sendMessage',{'chat_id':actor,'text':text if entries else header+'No hay destinos de este tipo.',
        'reply_markup':{'inline_keyboard':[[{'text':'𝐀𝐓𝐑Á𝐒','style':'danger','callback_data':f'manual:b:{uid}'}]]}})


async def select_number(s,actor,text):
    saved=await s.repo.setting(context_key(s,actor))
    if not saved:return False
    data=json.loads(saved)
    if data['expires']<time.time():return False
    account=await s.repo.account(data['uid'])
    if not account or not account['session'] or account['generation']!=data['generation']:
        raise ValueError('La sesión cambió. Abre de nuevo /menu telegram_id.')
    index=int(text)-1
    if not 0<=index<len(data['entries']):
        await s.tell(actor,'Ese número no está en la lista.');return True
    await open_board(s,actor,data['uid'],data['entries'][index]['id'])
    return True


async def enqueue(s,actor,token,position,message_id):
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,token)
    if not row or row['actor']!=actor or row['message_id']!=message_id or row['closed']:raise ValueError('Esta botonera ya no está activa. Abre /menu telegram_id.')
    if not s.almacen or row['source']==s.almacen:raise ValueError('El origen no puede ser ALMACEN.')
    await cooldown(s,row['uid'])
    account=await s.repo.account(row['uid'])
    if not account or not account['session'] or account['generation']!=s.repo.decode(row['payload'])['generation']:raise ValueError('La sesión cambió. Abre de nuevo /enlace.')
    async with s.repo.pool.acquire() as conn,conn.transaction():
        locked=await conn.fetchval('SELECT pg_try_advisory_xact_lock(751,hashtext($1))',f'export:{s.bot_id}')
        if not locked:raise ValueError('Hay un envío en curso. Espera a que termine.')
        job=await conn.fetchrow('SELECT * FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2 AND source=$3 FOR UPDATE',s.bot_id,row['uid'],row['source'])
        row=await conn.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2 FOR UPDATE',s.bot_id,token)
        if row['busy'] or row['position']!=position or row['actor']!=actor or row['message_id']!=message_id or row['closed']:raise ValueError('Ese enlace ya cambió o está descargándose. Usa la botonera actual.')
        data=s.repo.decode(row['payload'])
        if position>=len(data['entries']):raise ValueError('Esta lista ya terminó.')
        if job and job['state']=='running':raise ValueError('Ya hay una descarga activa en ese origen. Espera o usa /stop.')
        mid=data['entries'][position]['mid']
        await conn.execute('''INSERT INTO descarga_group_exports(bot_id,uid,source,actor,generation,state,phase,retry_message,manual_token)
            VALUES($1,$2,$3,$4,$5,'running','retry',$6,$7) ON CONFLICT(bot_id,uid,source) DO UPDATE SET
            actor=excluded.actor,generation=excluded.generation,state='running',phase='retry',retry_message=excluded.retry_message,
            manual_token=excluded.manual_token,sending=FALSE,scan=descarga_group_exports.scan+1,
            retry_return_state=CASE WHEN descarga_group_exports.state='done' THEN 'done' ELSE 'paused' END''',
            s.bot_id,row['uid'],row['source'],actor,account['generation'],mid,token)
        data['notice']=''
        await conn.execute('UPDATE descarga_manual_boards SET busy=TRUE,payload=$3 WHERE bot_id=$1 AND token=$2',s.bot_id,token,s.repo.encode(data))
    await s.repo.set_setting(last_key(s,actor,row['uid']),row['source'])
    await render(s,dict(row,busy=True,payload=s.repo.encode(data)))


async def callback(s,actor,query):
    if not authorized(actor) or query.get('message',{}).get('chat',{}).get('id')!=actor:return
    parts=query['data'].split(':');action=parts[1]
    if action in ('g','h','l','b'):
        uid=int(parts[2])
        if action=='b':return await menu(s,actor,uid)
        if action in ('g','h'):return await destinations(s,actor,uid,action=='h')
        source=await s.repo.setting(last_key(s,actor,uid))
        if not source:return await s.tell(actor,'Todavía no hay una descarga abierta para esa cuenta. Elige GRUPOS o CANALES.')
        return await open_board(s,actor,uid,int(source))
    token=parts[2];position=int(parts[3])
    if action=='d':return await enqueue(s,actor,token,position,query['message']['message_id'])
    if action=='w':return await s.tell(actor,'El enlace sigue descargándose. La botonera se actualizará al terminar.')
    if action=='c':
        row=await s.repo.pool.fetchrow('UPDATE descarga_manual_boards SET closed=TRUE WHERE bot_id=$1 AND token=$2 AND actor=$3 AND message_id=$4 RETURNING uid',s.bot_id,token,actor,query['message']['message_id'])
        if row:
            async with s.repo.pool.acquire() as conn:
                await conn.execute('SELECT pg_advisory_lock(752,hashtext($1))',token)
                try:
                    await s.api('editMessageText',{'chat_id':actor,'message_id':query['message']['message_id'],'text':'Botonera cerrada. El avance está guardado. Si hay un envío en curso, terminará.'})
                finally:await conn.execute('SELECT pg_advisory_unlock(752,hashtext($1))',token)
        await s.repo.set_setting(context_key(s,actor),'')


async def complete(s,job,items,count):
    """Commit export completion and UI progress together, so crashes can replay safely."""
    from .export_inventory import stored
    saved=await stored(s,job['source'],items)
    token=job['manual_token'];updated=None
    async with s.repo.pool.acquire() as conn,conn.transaction():
        current=await conn.fetchrow('SELECT * FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2 AND source=$3 FOR UPDATE',s.bot_id,job['uid'],job['source'])
        if not current or current['scan']!=job.get('scan',0) or current['manual_token']!=token:return
        row=await conn.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2 FOR UPDATE',s.bot_id,token)
        if row:
            data=s.repo.decode(row['payload']);pos=row['position']
            if pos<len(data['entries']) and data['entries'][pos]['mid']==job['retry_message']:
                data['entries'][pos]['status']='saved' if saved else 'skipped'
                if saved:data['last_downloaded']=pos+1
                data['notice']=f'Último enlace procesado: {pos+1}. '+('Guardado en ALMACEN.' if saved else 'Omitido por una exclusión; no se cuenta como descargado.')
                pos=advance(data,pos+1)
                await conn.execute('UPDATE descarga_manual_boards SET payload=$3,position=$4,busy=FALSE WHERE bot_id=$1 AND token=$2',s.bot_id,token,s.repo.encode(data),pos)
                updated=dict(row,payload=s.repo.encode(data),position=pos,busy=False)
        await conn.execute("UPDATE descarga_group_exports SET state=retry_return_state,phase='download',retry_message=0,sending=FALSE,manual_token=NULL,total=total+$5 WHERE bot_id=$1 AND uid=$2 AND source=$3 AND scan=$4",s.bot_id,job['uid'],job['source'],job.get('scan',0),count)
    if updated:
        try:await render(s,updated)
        except Exception:pass # State remains durable; /menu reopens it.


async def failed(s,job,detail):
    if not job.get('manual_token'):return
    token=job['manual_token']
    async with s.repo.pool.acquire() as conn,conn.transaction():
        row=await conn.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2 FOR UPDATE',s.bot_id,token)
        if not row:return
        data=s.repo.decode(row['payload']);data['notice']='⏸ '+detail[:450]+' No se avanzó al siguiente enlace.'
        await conn.execute('UPDATE descarga_manual_boards SET busy=FALSE,payload=$3 WHERE bot_id=$1 AND token=$2',s.bot_id,token,s.repo.encode(data))
    try:await render(s,dict(row,busy=False,payload=s.repo.encode(data)))
    except Exception:pass
