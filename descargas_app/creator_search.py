"""Consent-bound, resumable multimedia search, including both reply directions."""
import asyncio
import json
import time
import uuid
from telethon import types,utils,errors
from .creator_access import require_account,require_consent,require_source
from .account_chat import connected
from .private_chats import peer_key,title
from .link_listing import media_label


def state_key(s,actor,token=None):return f'creator-search:{s.bot_id}:{actor}'+(':'+token if token else '')
def pending_key(s,actor):return f'creator-search-input:{s.bot_id}:{actor}'
def chunks(text,limit=2800):
    part='';units=0
    for char in text:
        width=2 if ord(char)>0xFFFF else 1
        if units+width>limit:
            yield part;part='';units=0
        part+=char;units+=width
    if part:yield part

def matches(text,query):return query.casefold() in (text or '').casefold()
def reply_id(message):return getattr(getattr(message,'reply_to',None),'reply_to_msg_id',None)
def same_peer_reply(message,source):
    peer=getattr(getattr(message,'reply_to',None),'reply_to_peer_id',None)
    return peer is None or utils.get_peer_id(peer)==source


def related(message,parent,query,source):
    """Return media and exact text evidence; never infer a link by proximity."""
    own=message.message or ''
    if media_label(message):
        evidence=[]
        if matches(own,query):evidence.append(own)
        if parent and same_peer_reply(message,source) and matches(parent.message,query):evidence.append(parent.message)
        return (message,list(dict.fromkeys(evidence))) if evidence else (None,[])
    if matches(own,query) and parent and same_peer_reply(message,source) and media_label(parent):return parent,[own]
    return None,[]


async def prompt(s,actor,uid):
    await require_consent(s,actor,uid)
    await s.repo.set_setting(pending_key(s,actor),json.dumps({'uid':uid,'expires':time.time()+1800}))
    await s.tell(actor,'🔎 Escribe la palabra o frase que quieres buscar. Revisaré multimedia y sus captions/respuestas en grupos, canales, chats privados y Mensajes guardados accesibles. /menu cancela. La revisión se realiza por tandas; no identifica quién hizo reportes en Telegram.')


async def input_text(s,actor,text):
    raw=await s.repo.setting(pending_key(s,actor))
    if not raw:return False
    data=json.loads(raw)
    if data['expires']<time.time():
        await s.repo.set_setting(pending_key(s,actor),'');return False
    query=' '.join(text.split())
    if not query or len(query)>80:raise ValueError('Escribe una palabra o frase de hasta 80 caracteres.')
    uid=data['uid'];await require_consent(s,actor,uid)
    account=await s.repo.account(uid)
    state={'uid':uid,'generation':account['generation'],'query':query,'token':uuid.uuid4().hex[:16],
        'expires':time.time()+1800,'dialogs':None,'dialog':0,'offset':0,'results':[],'seen':[],'scanned':0}
    await s.repo.set_setting(state_key(s,actor,state['token']),s.repo.encode(state).hex())
    await s.repo.set_setting(state_key(s,actor),state['token'])
    await s.repo.set_setting(pending_key(s,actor),'')
    await scan(s,actor,state['token'])
    return True


async def load(s,actor,token):
    if await s.repo.setting(state_key(s,actor))!=token:raise ValueError('Esta búsqueda venció o fue sustituida.')
    raw=await s.repo.setting(state_key(s,actor,token))
    state=s.repo.decode(bytes.fromhex(raw)) if raw else None
    if not state or state['token']!=token or state['expires']<time.time():raise ValueError('Esta búsqueda venció o fue sustituida. Pulsa BUSCAR otra vez.')
    await require_consent(s,actor,state['uid'])
    account=await s.repo.account(state['uid'])
    if not account or not account['session'] or account['generation']!=state['generation']:raise ValueError('La sesión cambió. Inicia otra búsqueda.')
    return state


async def scan(s,actor,token):
    from .group_download import users_busy
    from .manual_board import cooldown
    state=await load(s,actor,token);uid=state['uid']
    await cooldown(s,uid)
    if await users_busy(s):raise ValueError('Espera a que termine la descarga al DM y pulsa CONTINUAR BÚSQUEDA.')
    # Existing account lock serializes duplicate clicks and concurrent searches.
    found=[];notice=''
    try:
        async with connected(s,uid) as client,asyncio.timeout(45):
            state=await load(s,actor,token)
            if state['dialogs'] is None:
                dialogs=[]
                async for dialog in client.iter_dialogs():
                    entity=dialog.entity
                    if len(dialogs)%50==0 and await users_busy(s):raise ValueError('Espera a que termine la descarga al DM y continúa la búsqueda.')
                    if any(getattr(entity,f,False) for f in ('left','kicked','deactivated')) or getattr(entity,'migrated_to',None):continue
                    if len(dialogs)>=5000:
                        notice='Directorio limitado a 5000 chats accesibles en esta búsqueda.';break
                    source=utils.get_peer_id(entity)
                    dialogs.append({'id':source,'hash':getattr(entity,'access_hash',0) or 0,'name':title(entity,uid,source),
                        'kind':'user' if isinstance(entity,types.User) else 'channel' if isinstance(entity,types.Channel) else 'chat'})
                if not any(d['id']==uid for d in dialogs):dialogs.insert(0,{'id':uid,'hash':0,'name':'Mensajes guardados','kind':'user'})
                state['dialogs']=dialogs
                await s.repo.set_setting(state_key(s,actor,state['token']),s.repo.encode(state).hex())
            for _ in range(8):
                await require_consent(s,actor,uid)
                if state['dialog']>=len(state['dialogs']) or await users_busy(s):break
                d=state['dialogs'][state['dialog']];source=d['id']
                peer='me' if source==uid else types.InputPeerUser(source,d['hash']) if d['kind']=='user' else types.InputPeerChannel(utils.resolve_id(source)[0],d['hash']) if d['kind']=='channel' else types.InputPeerChat(-source)
                try:await require_source(s,uid,source,client=client,actor=actor)
                except ValueError:
                    state['dialog']+=1;state['offset']=0;continue
                await s.repo.pace(f'enlace-read:{uid}',3.1)
                try:page=[m async for m in client.iter_messages(peer,offset_id=state['offset'],limit=50)]
                except (errors.ChannelPrivateError,errors.ChatAdminRequiredError,errors.PeerIdInvalidError,errors.UserIdInvalidError):
                    state['dialog']+=1;state['offset']=0
                    await s.repo.set_setting(state_key(s,actor,state['token']),s.repo.encode(state).hex())
                    continue
                parent_ids=list({reply_id(m) for m in page if reply_id(m) and same_peer_reply(m,source)})
                parents=await client.get_messages(peer,ids=parent_ids) if parent_ids else []
                by_id={m.id:m for m in parents if m}
                from .service import album
                for message in page:
                    media,evidence=related(message,by_id.get(reply_id(message)),state['query'],source)
                    if not media:continue
                    identity=f'{source}:{getattr(media,"grouped_id",None) or media.id}'
                    previous=next((r for r in state['results'] if r['identity']==identity),None)
                    if previous:
                        previous['texts']=list(dict.fromkeys(previous['texts']+evidence));continue
                    items=await album(client,peer,media)
                    if not any(media_label(m) for m in items):continue
                    if source>0 and source!=uid:
                        await s.repo.set_setting(peer_key(s,uid,state['generation'],source),s.repo.encode({'uid':uid,'source':source,'generation':state['generation'],'access_hash':d['hash']}).hex())
                    result={'identity':identity,'source':source,'mid':media.id,'name':d['name'],'texts':evidence}
                    state['results'].append(result);found.append(len(state['results'])-1)
                    # A result stores the complete album; opening it still revalidates everything.
                    from .manual_board import entry
                    result['entry']=entry(media,[m for m in items if media_label(m)])
                    if len(state['results'])>=1000:break
                state['scanned']+=len(page)
                if page:state['offset']=min(m.id for m in page)
                if len(page)<50:state['dialog']+=1;state['offset']=0
                await s.repo.set_setting(state_key(s,actor,state['token']),s.repo.encode(state).hex())
                if found or len(state['results'])>=1000:break
    except errors.FloodWaitError as exc:
        await s.repo.set_setting(f'link-list-wait:{s.bot_id}:{uid}',time.time()+max(60,exc.seconds+10))
        notice=f'Telegram pide esperar {max(60,exc.seconds+10)} segundos. El avance se conserva.'
    except TimeoutError:notice='Se alcanzó el tiempo de esta tanda. El avance se conserva.'
    await load(s,actor,token)
    await require_consent(s,actor,uid)
    await s.repo.set_setting(state_key(s,actor,state['token']),s.repo.encode(state).hex())
    complete=state['dialogs'] is not None and state['dialog']>=len(state['dialogs'])
    text=f'🔎 {state["query"]}\nCuenta: {uid}\nMensajes revisados: {state["scanned"]}\nResultados: {len(state["results"])}\n'+('Búsqueda terminada.' if complete else 'Búsqueda parcial; continúa por tandas.')
    if notice:text+='\n'+notice
    await s.tell(actor,text)
    for index in found:
        r=state['results'][index]
        for snippet in chunks('\n\n'.join(r['texts'])):
            payload={'chat_id':actor,'text':f'{index+1}. {r["name"]}\nOrigen: {r["source"]} · Mensaje: {r["mid"]}\nTexto vinculado:\n{snippet}',
                'reply_markup':{'inline_keyboard':[[{'text':'ABRIR RESULTADO','callback_data':f'global:d:{token}:{index}','style':'primary'}]]}}
            await s.api('sendMessage',payload)
    buttons=[]
    if not complete and len(state['results'])<1000:buttons.append([{'text':'CONTINUAR BÚSQUEDA','callback_data':f'global:n:{token}','style':'primary'}])
    if len(state['results'])>=1000:text='Se alcanzaron 1000 resultados. Inicia otra búsqueda más específica.'
    else:text='Los botones permiten descargar cada resultado a ALMACEN con sus textos vinculados.'
    buttons.append([{'text':'MENÚ PRINCIPAL','callback_data':f'manual:b:{uid}','style':'primary'}])
    await s.api('sendMessage',{'chat_id':actor,'text':text,'reply_markup':{'inline_keyboard':buttons}})


async def open_result(s,actor,token,index):
    state=await load(s,actor,token)
    if not 0<=index<len(state['results']):raise ValueError('Resultado no válido.')
    result=state['results'][index];uid=state['uid'];source=result['source']
    # Reuse the existing durable board and worker; the new view is a single exact result.
    from .manual_board import open_board,render
    await open_board(s,actor,uid,source)
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND uid=$2 AND source=$3',s.bot_id,uid,source)
    if row['busy']:raise ValueError('Espera a que termine la descarga del origen.')
    entry=dict(result['entry'],matched_texts=result['texts'])
    data=s.repo.decode(row['payload'])
    from .manual_gallery import view_key
    await s.repo.set_setting(view_key(s,uid,source,data.get('query'),data.get('mode','')),s.repo.encode({'data':data,'position':row['position']}).hex())
    data=dict(data,entries=[entry],complete=True,lazy=False,query='global:'+token+':'+str(index),display_query=state['query'],mode='',search_consent=True,global_result=True)
    data.pop('totals',None);data.pop('streams',None)
    new_token=uuid.uuid4().hex[:16]
    await s.repo.pool.execute('UPDATE descarga_manual_boards SET payload=$3,position=0,token=$4 WHERE bot_id=$1 AND token=$2 AND NOT busy',s.bot_id,row['token'],s.repo.encode(data),new_token)
    await render(s,dict(row,payload=s.repo.encode(data),position=0,token=new_token))


async def callback(s,actor,query):
    parts=query['data'].split(':')
    if parts[1]=='n':return await scan(s,actor,parts[2])
    if parts[1]=='d':return await open_result(s,actor,parts[2],int(parts[3]))


async def evidence(s,job,items,archive_ids=None):
    """Store exact matching text separately so captions and media are never changed."""
    if not job.get('manual_token'):return
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,job['manual_token'])
    if not row:return
    data=s.repo.decode(row['payload'])
    if not data.get('search_consent'):return
    await require_consent(s,job['actor'],job['uid'])
    ids={m.id for m in items}
    entries=[e for e in data['entries'] if ids.intersection(e['ids'])]
    texts=list(dict.fromkeys(text for e in entries for text in e.get('matched_texts',[])))
    if not texts:return
    from .manual_board import link
    import hashlib
    text='TEXTOS VINCULADOS A LA BÚSQUEDA\nCuenta: '+str(job['uid'])+'\nOrigen: '+link(job['source'],min(ids))+'\nBúsqueda: '+data.get('display_query',data['query'])+'\n\n'+'\n\n'.join(texts)
    # Reserve before publishing; an ambiguous response is never automatically repeated.
    for index,part in enumerate(chunks(text)):
        digest=hashlib.sha256((str(job['uid'])+':'+str(job['source'])+':'+text+':'+str(index)).encode()).hexdigest()
        reserved=await s.repo.pool.fetchval("INSERT INTO descarga_settings(key,value) VALUES($1,'reserved') ON CONFLICT DO NOTHING RETURNING 1",f'creator-evidence:{s.bot_id}:{digest}')
        if not reserved:continue
        await require_consent(s,job['actor'],job['uid'])
        payload={'chat_id':s.almacen,'text':part}
        if archive_ids:payload['reply_parameters']={'message_id':archive_ids[0]}
        await s.api('sendMessage',payload)
        await s.repo.set_setting(f'creator-evidence:{s.bot_id}:{digest}','sent')
