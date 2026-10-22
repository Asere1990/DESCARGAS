"""Bounded server-filtered gallery pages; no full history scan before controls."""
import hashlib
import json
import time
from telethon import functions,types,utils
from .moderation import authorized,ensure_allowed
from .protected import require_entity
from .paid_media import purchased_items
from .link_listing import media_label

FILTERS=(types.InputMessagesFilterPhotoVideo,types.InputMessagesFilterGif,types.InputMessagesFilterDocument)


def view_key(s,uid,source,query,mode=""):
    digest=hashlib.sha256(((query or '')+('\0'+mode if mode else '')).encode()).hexdigest()[:24]
    return f'manual-view:{s.bot_id}:{uid}:{source}:{digest}'


async def switch_view(s,row,uid,source,query,mode=""):
    if not row:return None
    old=s.repo.decode(row['payload'])
    if (old.get('query') or '')==(query or '') and old.get('mode','')==mode:return row
    if row['busy']:raise ValueError('Espera a que termine la descarga antes de cambiar la búsqueda.')
    await s.repo.set_setting(view_key(s,uid,source,old.get('query'),old.get('mode','')),s.repo.encode({'data':old,'position':row['position']}).hex())
    cached=await s.repo.setting(view_key(s,uid,source,query,mode))
    current=s.repo.decode(bytes.fromhex(cached)) if cached else {'data':{'generation':old['generation'],'title':old['title'],'entries':[],'query':query or '','mode':mode},'position':0}
    # The active panel is replaced by open_board; old callbacks must be fenced.
    current['data']['force_open']=True
    return dict(row,payload=s.repo.encode(current['data']),position=current['position'],message_id=None)


async def entity_for(s,client,uid,source,generation):
    if source==uid:
        entity=await client.get_me()
        if entity.id!=uid:raise ValueError('La sesión no corresponde a esa cuenta.')
        return entity
    if not str(source).startswith('-100'):raise ValueError('Se necesita el ID -100 de un grupo o canal.')
    cache=getattr(s,'_manual_sources',None)
    if cache is None:cache={};s._manual_sources=cache
    key=(uid,generation,source)
    entity=cache.get(key)
    if entity is None:
        entity=await s.resolve(client,source)
        if len(cache)>=128:cache.pop(next(iter(cache)))
        cache[key]=entity
    if not isinstance(entity,types.Channel) or utils.get_peer_id(entity)!=source:raise ValueError('No se pudo verificar el origen.')
    require_entity(entity,uid)
    return entity


async def collect_page(s,actor,uid,source,data=None):
    from .manual_board import cooldown,entry
    from .group_download import users_busy
    await cooldown(s,uid)
    account=await s.repo.account(uid)
    if not account or not account['session']:raise ValueError('Esa cuenta no está conectada.')
    if not authorized(actor):raise ValueError('No autorizado.')
    data=data or {'generation':account['generation'],'title':str(source),'entries':[]}
    if data['generation']!=account['generation']:raise ValueError('La sesión cambió. Abre de nuevo el panel.')
    if data.get('complete'):return data
    if await users_busy(s):raise ValueError('Espera a que termine la descarga al DM y vuelve a abrir la botonera.')
    checkpoint_key=view_key(s,uid,source,data.get('query'),data.get('mode',''))+':page'
    checkpoint=await s.repo.setting(checkpoint_key)
    if checkpoint:
        cached=s.repo.decode(bytes.fromhex(checkpoint))
        if cached['generation']==data['generation']:
            for field in ('batch_remaining','return_position','number_request','number_notice','notice','progress'):
                cached.pop(field,None)
                if field in data:cached[field]=data[field]
            data=cached
    data['lazy']=True
    streams=data.setdefault('streams',{})
    existing={e.get('group') or ('single',e['mid']):e for e in data['entries']}
    parts=data.setdefault('album_parts',{})
    existing.update({int(key):value for key,value in parts.items()})
    fresh=[]
    mode=data.get('mode','')
    async with s.repo.account_lock(uid,wait=False):
        await ensure_allowed(s,uid,source)
        current=await s.repo.account(uid)
        if not current or not current['session'] or current['generation']!=data['generation']:raise ValueError('La sesión cambió.')
        client=s.client(await s.get_session(uid));client.flood_sleep_threshold=0
        try:
            await client.connect()
            if not await client.is_user_authorized():raise ValueError('Conecta de nuevo la cuenta.')
            entity=await entity_for(s,client,uid,source,data['generation'])
            data['title']='Mensajes guardados' if source==uid else ' '.join(entity.title.split())[:128]
            if not data.get('query') and 'totals' not in data:
                await s.repo.pace(f'enlace-read:{uid}',3.1)
                counters=await client(functions.messages.GetSearchCountersRequest(peer=entity,filters=[types.InputMessagesFilterPhotos(),types.InputMessagesFilterVideo(),types.InputMessagesFilterGif(),types.InputMessagesFilterDocument()]))
                data['totals']={name:counter.count for name,counter in zip(('photos','videos','gifs','documents'),counters)}
            filters=(types.InputMessagesFilterEmpty,) if data.get('query') else ((types.InputMessagesFilterPhotos,types.InputMessagesFilterDocument) if mode=='photo' else (types.InputMessagesFilterVideo,types.InputMessagesFilterDocument) if mode=='video' else FILTERS)
            for index,kind in enumerate(filters):
                state=streams.setdefault(str(index),{'offset':0,'done':False})
                if state['done']:continue
                if await users_busy(s):break
                await s.repo.pace(f'enlace-read:{uid}',3.1)
                page=[m async for m in client.iter_messages(entity,filter=kind(),search=data.get('query') or None,offset_id=state['offset'],min_id=data.get('after',0),limit=50)]
                candidates=page
                if data.get('query'):
                    # Match captions directly, or a text reply's explicit parent.
                    parent_ids=list({getattr(getattr(m,'reply_to',None),'reply_to_msg_id',None) for m in page if not media_label(m) and (not getattr(getattr(m,'reply_to',None),'reply_to_peer_id',None) or utils.get_peer_id(m.reply_to.reply_to_peer_id)==source)}-{None})
                    parents=await client.get_messages(entity,ids=parent_ids) if parent_ids else []
                    candidates=[m for m in page if media_label(m)]+[m for m in parents if m]
                for message in candidates:
                    try:paid=purchased_items(message)
                    except ValueError:continue
                    selected=paid if paid is not None else [message]
                    if mode in ('photo','video'):
                        selected=[m for m in selected if media_label(m) in (('Foto','Imagen') if mode=='photo' else ('Video',))]
                    piece=entry(message,selected)
                    if mode in ('photo','video'):
                        piece['group']=None;piece['ids']=[m.id for m in selected]
                    if not piece['elements']:continue
                    if mode=='album' and not piece['group'] and piece['elements']<2:continue
                    key=piece['group'] or ('single',message.id)
                    previous=existing.get(key)
                    if previous:
                        if message.id in previous['ids']:continue
                        # Grouped media can span pages and filter categories.
                        previous['ids']+=piece['ids'];previous['files']+=piece['files']
                        for field in ('photos','videos','gifs','elements'):previous[field]+=piece[field]
                    else:
                        existing[key]=piece
                        if mode!='album' or piece['elements']>=2:fresh.append(piece)
                        else:parts[str(key)]=piece
                    if mode=='album' and str(key) in parts and existing[key]['elements']>=2:
                        fresh.append(parts.pop(str(key)))
                if page:state['offset']=min(m.id for m in page)
                state['done']=len(page)<50
                data['entries']+=sorted(fresh,key=lambda e:e['mid'],reverse=True);fresh=[]
                await s.repo.set_setting(checkpoint_key,s.repo.encode(data).hex())
        finally:await client.disconnect()
    data['entries']+=sorted(fresh,key=lambda e:e['mid'],reverse=True)
    data['complete']=all(streams.get(str(i),{}).get('done',False) for i in range(len(filters)))
    inventory=await s.repo.pool.fetch('SELECT kind,file_id FROM descarga_export_files WHERE bot_id=$1 AND almacen=$2 AND source=$3',s.bot_id,s.almacen,source)
    saved={(r['kind'],r['file_id']) for r in inventory}
    # Full albums are verified again by the existing download path before sending.
    for e in data['entries']:
        if not e.get('group') and e['files'] and all(tuple(f) in saved for f in e['files']):e['status']='saved'
    data.pop('force_open',None)
    return data


async def add_location(s,actor,text):
    location=' '.join(text.split()).strip()
    if not location or len(location)>80 or not any(c.isalpha() for c in location):raise ValueError('Uso: /add miami (máximo 80 caracteres).')
    key=f'manual-locations:{s.bot_id}'
    # Serialise additions so simultaneous admins cannot overwrite each other.
    async with s.repo.pool.acquire() as conn:
        await conn.execute('SELECT pg_advisory_lock(754,hashtext($1))',key)
        try:
            values=json.loads(await s.repo.setting(key) or '[]')
            if location.casefold() not in {x.casefold() for x in values}:values.append(location)
            await s.repo.set_setting(key,json.dumps(values,ensure_ascii=False))
        finally:await conn.execute('SELECT pg_advisory_unlock(754,hashtext($1))',key)
    await s.tell(actor,f'✅ Ubicación guardada: {location}. Puedes usarla en BUSCAR.')


async def search_input(s,actor,text):
    key=f'manual-search:{s.bot_id}:{actor}'
    raw=await s.repo.setting(key)
    if not raw:return False
    pending=json.loads(raw)
    if pending['expires']<time.time():
        await s.repo.set_setting(key,'');return False
    text=' '.join(text.split())
    if not text or len(text)>80:raise ValueError('Escribe una ubicación de hasta 80 caracteres.')
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,pending['token'])
    if not row or row['closed'] or row['actor']!=actor or row['message_id']!=pending['message_id'] or s.repo.decode(row['payload'])['generation']!=pending['generation']:
        await s.repo.set_setting(key,'');raise ValueError('La botonera cambió. Pulsa BUSCAR otra vez.')
    from .manual_board import open_board
    await open_board(s,actor,row['uid'],row['source'],refresh=True,query=text)
    await s.repo.set_setting(key,'')
    return True
