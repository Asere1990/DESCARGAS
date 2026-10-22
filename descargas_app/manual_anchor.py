"""Administrator five-link navigation around an explicit source message."""
import json
import time
from urllib.parse import urlparse,parse_qs
from telethon import types,utils
from .moderation import authorized,ensure_allowed
from .link_listing import media_label
from .paid_media import purchased_items


def pending_key(s,actor):return f'manual-anchor:{s.bot_id}:{actor}'


def presentation(row,data):
    from .manual_board import link
    origin=int(data['query'].split(':')[1]);cursor=data.get('anchor_cursor',origin)
    text=(f'{data["title"]}\nCuenta: {row["uid"]}\nID: {row["source"]}\n\n'
        'Puedes descargar el contenido siguiente o anterior comenzando desde aquí:\n'+link(row['source'],origin)+
        '\n\nSIGUIENTE 5: hacia los mensajes más nuevos.\nANTERIOR 5: hacia los más antiguos.\nUn álbum cuenta como un enlace y se descarga completo.\n'+
        ('La primera tanda incluye el punto inicial si contiene multimedia.' if not data.get('anchor_started') else 'Último punto procesado: '+link(row['source'],cursor)))
    if data.get('progress'):text+='\n\n'+data['progress']
    if data.get('notice'):text+='\n\n'+data['notice'][:450]
    if row['busy']:text+='\n\n⏳ Procesando hasta cinco enlaces…'
    button=lambda label,action,style:{'text':label,'style':style,'callback_data':f'manual:{"w" if row["busy"] and action in ("ax","ay") else action}:{row["token"]}:{row["position"]}'}
    return text,[[button('𝐀𝐍𝐓𝐄𝐑𝐈𝐎𝐑 𝟓','ay','success'),button('𝐒𝐈𝐆𝐔𝐈𝐄𝐍𝐓𝐄 𝟓','ax','success')],
        [button('𝐀𝐓𝐑Á𝐒','az','danger')],[button('𝐌𝐄𝐍Ú 𝐏𝐑𝐈𝐍𝐂𝐈𝐏𝐀𝐋','am','primary')]]


async def input_link(s,actor,text):
    raw=await s.repo.setting(pending_key(s,actor))
    if not raw or not authorized(actor):return False
    pending=json.loads(raw)
    if pending['expires']<time.time():
        await s.repo.set_setting(pending_key(s,actor),'');return False
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,pending['token'])
    if not row or row['actor']!=actor or row['closed'] or row['message_id']!=pending['message_id'] or row['busy']:raise ValueError('Abre la botonera actual y pulsa INICIAR DESDE otra vez.')
    account=await s.repo.account(row['uid'])
    if not account or not account['session'] or account['generation']!=pending['generation']:raise ValueError('La sesión cambió. Abre /menu telegram_id.')
    from .service import parse_link
    if text.startswith('tg://openmessage?'):
        values=parse_qs(urlparse(text).query)
        try:source=int(values['user_id'][0]);mid=int(values['message_id'][0])
        except (KeyError,ValueError,IndexError):raise ValueError('Ese enlace de Mensajes guardados no es válido.')
        if source!=row['source'] or source<=0:raise ValueError('El enlace debe pertenecer al origen de esta botonera.')
    else:
        source,mid=parse_link(text.strip())
        if isinstance(source,str):
            from .manual_gallery import entity_for
            from .manual_board import cooldown
            await cooldown(s,row['uid'])
            async with s.repo.account_lock(row['uid'],wait=False):
                client=s.client(await s.get_session(row['uid']));client.flood_sleep_threshold=0
                try:
                    await client.connect()
                    if not await client.is_user_authorized():raise ValueError('Conecta de nuevo esa cuenta.')
                    entity=await entity_for(s,client,row['uid'],row['source'],account['generation'])
                    if (getattr(entity,'username',None) or '').casefold()!=source.casefold():raise ValueError('El enlace debe pertenecer al grupo o canal de esta botonera.')
                    source=utils.get_peer_id(entity)
                finally:await client.disconnect()
        if source!=row['source']:raise ValueError('El enlace debe pertenecer al grupo o canal de esta botonera.')
    if not 0<mid<2**31:raise ValueError('El número del mensaje no es válido.')
    parent=s.repo.decode(row['payload'])
    from .manual_board import open_board,render
    await open_board(s,actor,row['uid'],row['source'],query=f'anchor:{mid}',mode='anchor')
    current=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND uid=$2 AND source=$3',s.bot_id,row['uid'],row['source'])
    data=s.repo.decode(current['payload'])
    data['anchor_parent']={'query':parent.get('query') or '', 'mode':parent.get('mode','')}
    await s.repo.pool.execute('UPDATE descarga_manual_boards SET payload=$3 WHERE bot_id=$1 AND token=$2',s.bot_id,current['token'],s.repo.encode(data))
    await s.repo.set_setting(pending_key(s,actor),'')
    await render(s,current)
    return True


async def callback(s,actor,query):
    if not authorized(actor) or query.get('message',{}).get('chat',{}).get('id')!=actor:return
    async with s.repo.pool.acquire() as conn:
        locked=await conn.fetchval('SELECT pg_try_advisory_lock(756,hashtext($1))',pending_key(s,actor))
        if not locked:raise ValueError('La pulsación anterior sigue procesándose.')
        try:await _callback(s,actor,query)
        finally:await conn.execute('SELECT pg_advisory_unlock(756,hashtext($1))',pending_key(s,actor))


async def _callback(s,actor,query):
    _,action,token,position=query['data'].split(':')
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,token)
    if not row or row['actor']!=actor or row['closed'] or row['message_id']!=query['message']['message_id'] or row['position']!=int(position):raise ValueError('Esta botonera ya cambió. Abre la actual.')
    data=s.repo.decode(row['payload'])
    from .manual_board import open_board,enqueue,menu,render
    if action=='am':return await menu(s,actor,row['uid'])
    if row['busy']:raise ValueError('Espera a que termine la descarga actual.')
    for kind in ('manual-number','manual-search','manual-anchor'):
        await s.repo.set_setting(f'{kind}:{s.bot_id}:{actor}','')
    if action=='as':
        await s.repo.set_setting(pending_key(s,actor),json.dumps({'token':token,'message_id':row['message_id'],'generation':data['generation'],'expires':time.time()+1800}))
        return await s.tell(actor,'Envíame el enlace del grupo o canal desde donde quieres iniciar. Después podrás descargar los cinco siguientes o los cinco anteriores, incluyendo ese punto en la primera tanda.')
    if data.get('mode')!='anchor':raise ValueError('Pulsa INICIAR DESDE para elegir el punto inicial.')
    if action=='az':
        parent=data.get('anchor_parent') or {}
        return await open_board(s,actor,row['uid'],row['source'],query=parent.get('query') or None,mode=parent.get('mode',''))
    if action not in ('ax','ay'):return
    direction=1 if action=='ax' else -1
    origin=int(data['query'].split(':')[1]);start=data.get('anchor_cursor',origin)
    data.update(entries=[],streams={},anchor_buffer=[],complete=False,lazy=True,anchor_ready=True,
        anchor_direction=direction,anchor_start=start,anchor_inclusive=not data.get('anchor_started',False),notice='')
    data.pop('progress',None)
    async with s.repo.pool.acquire() as conn,conn.transaction():
        current=await conn.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2 FOR UPDATE',s.bot_id,token)
        if not current or current['busy'] or current['closed'] or current['position']!=int(position) or current['actor']!=actor or current['message_id']!=query['message']['message_id']:raise ValueError('La botonera cambió.')
        await conn.execute('UPDATE descarga_manual_boards SET payload=$3,position=0 WHERE bot_id=$1 AND token=$2',s.bot_id,token,s.repo.encode(data))
    await render(s,dict(row,payload=s.repo.encode(data),position=0))
    current=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,token)
    await enqueue(s,actor,token,0,current['message_id'],batch=5)


async def collect(s,actor,uid,source,data):
    """Merge filtered streams only up to a safe frontier, retaining PDF-page gaps."""
    from .manual_board import entry,cooldown
    from .manual_gallery import entity_for,FILTERS
    from .group_download import users_busy
    if not data.get('anchor_ready'):
        origin=int(data['query'].split(':')[1])
        data.update(anchor_ready=True,anchor_cursor=origin,anchor_started=False,entries=[],complete=False,lazy=True)
        return data
    if data.get('complete'):return data
    await cooldown(s,uid)
    if not authorized(actor):raise ValueError('No autorizado.')
    account=await s.repo.account(uid)
    if not account or not account['session'] or account['generation']!=data['generation']:raise ValueError('La sesión cambió.')
    if await users_busy(s):raise ValueError('Espera a que termine la descarga al DM.')
    direction=data.get('anchor_direction',1);start=data.get('anchor_start',data['anchor_cursor']);inclusive=data.get('anchor_inclusive',True)
    streams=data.setdefault('streams',{});buffer=data.setdefault('anchor_buffer',[])
    existing={e.get('group') or ('single',e['mid']):e for e in [*data['entries'],*buffer]}
    established=all(str(i) in streams and (streams[str(i)]['done'] or streams[str(i)]['offset']) for i in range(len(FILTERS)))
    active_offsets=[v['offset'] for v in streams.values() if not v['done']]
    limiting=(min(active_offsets) if direction==1 else max(active_offsets)) if established and active_offsets else None
    async with s.repo.account_lock(uid,wait=False):
        current=await s.repo.account(uid)
        if not current or not current['session'] or current['generation']!=data['generation']:raise ValueError('La sesión cambió.')
        await ensure_allowed(s,uid,source)
        client=s.client(await s.get_session(uid));client.flood_sleep_threshold=0
        try:
            await client.connect()
            if not await client.is_user_authorized():raise ValueError('Conecta de nuevo esa cuenta.')
            entity=await entity_for(s,client,uid,source,data['generation'])
            from .private_chats import title
            data['title']=title(entity,uid,source)
            for index,kind in enumerate(FILTERS):
                state=streams.setdefault(str(index),{'offset':0,'done':False})
                if state['done']:continue
                if limiting is not None and state['offset']!=limiting:continue
                if await users_busy(s):break
                await s.repo.pace(f'enlace-read:{uid}',3.1)
                if await users_busy(s):break
                arguments={'filter':kind(),'limit':50}
                if direction==1:arguments.update(reverse=True,min_id=state['offset'] or max(0,start-(1 if inclusive else 0)))
                else:arguments.update(max_id=state['offset'] or start+(1 if inclusive else 0))
                page=[m async for m in client.iter_messages(entity,**arguments)]
                for message in page:
                    if message.id in data.get('anchor_skip_ids',[]):continue
                    try:paid=purchased_items(message)
                    except ValueError:continue
                    piece=entry(message,paid if paid is not None else [message])
                    if not piece['elements']:continue
                    key=piece['group'] or ('single',piece['mid']);old=existing.get(key)
                    if old:
                        if message.id in old['ids']:continue
                        old['ids']+=piece['ids'];old['files']+=piece['files']
                        for field in ('photos','videos','gifs','elements'):old[field]+=piece[field]
                    else:existing[key]=piece;buffer.append(piece)
                if page:state['offset']=(max if direction==1 else min)(m.id for m in page)
                state['done']=len(page)<50
        finally:await client.disconnect()
    initialized=all(str(i) in streams and (streams[str(i)]['done'] or streams[str(i)]['offset']) for i in range(len(FILTERS)))
    active=[v['offset'] for v in streams.values() if not v['done']]
    frontier=(min(active) if direction==1 else max(active)) if active else None
    safe=[e for e in buffer if initialized and (frontier is None or (e['mid']<=frontier if direction==1 else e['mid']>=frontier))]
    data['entries']+=sorted(safe,key=lambda e:e['mid'],reverse=direction==-1)
    safe_ids={id(e) for e in safe};data['anchor_buffer']=[e for e in buffer if id(e) not in safe_ids]
    data['complete']=initialized and all(v['done'] for v in streams.values())
    return data
