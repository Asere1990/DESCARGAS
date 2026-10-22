"""Administrator-only private gallery directory; bounded counter batches."""
import asyncio
import json
import time
import uuid
from telethon import errors,functions,types,utils
from .moderation import authorized
from .protected import require_entity,ProtectedSource
from .account_chat import connected


def peer_key(s,uid,generation,source):return f'private-gallery-peer:{s.bot_id}:{uid}:{generation}:{source}'
def scan_key(s,actor,uid):return f'private-gallery-list:{s.bot_id}:{actor}:{uid}'


def title(entity,uid,source):
    if uid==source:return 'Mensajes guardados'
    if isinstance(entity,types.User):return ' '.join(' '.join(filter(None,[entity.first_name,entity.last_name])).split())[:128] or 'Cuenta sin nombre'
    return ' '.join(entity.title.split())[:128]


async def resolve(s,client,uid,source,generation):
    raw=await s.repo.setting(peer_key(s,uid,generation,source))
    if not raw:raise ValueError('Abre CHATS PRIVADOS con esa cuenta y selecciona el chat de su lista.')
    data=s.repo.decode(bytes.fromhex(raw))
    if data['uid']!=uid or data['source']!=source or data['generation']!=generation:raise ValueError('El chat no corresponde a esta sesión.')
    entity=await client.get_entity(types.InputPeerUser(source,data['access_hash']))
    if not isinstance(entity,types.User) or entity.id!=source:raise ValueError('No se pudo verificar el chat privado.')
    require_entity(entity,uid)
    return entity


async def listing(s,actor,uid):
    if not authorized(actor):return
    from .manual_board import context_key,cooldown
    from .group_download import users_busy
    from .group_admin import clear
    await clear(s,actor)
    account=await s.repo.account(uid)
    if not account or not account['session']:raise ValueError('Esa cuenta no está conectada.')
    await cooldown(s,uid)
    if await users_busy(s):raise ValueError('Espera a que termine la descarga al DM antes de consultar los chats privados.')
    lock=f'{s.bot_id}:{actor}:{uid}'
    async with s.repo.pool.acquire() as conn:
        if not await conn.fetchval('SELECT pg_try_advisory_lock(759,hashtext($1))',lock):raise ValueError('La lista de chats privados sigue preparándose.')
        try:await _listing(s,actor,uid,account)
        finally:await conn.execute('SELECT pg_advisory_unlock(759,hashtext($1))',lock)


async def _listing(s,actor,uid,account):
    from .manual_board import context_key
    from .group_download import users_busy
    generation=account['generation'];token=uuid.uuid4().hex
    base={'uid':uid,'generation':generation,'expires':time.time()+1800,'entries':[],'private_token':token}
    await s.repo.set_setting(context_key(s,actor),json.dumps(base))
    raw=await s.repo.setting(scan_key(s,actor,uid))
    state=s.repo.decode(bytes.fromhex(raw)) if raw else None
    if not state or state['generation']!=generation or state['expires']<time.time():
        state={'generation':generation,'expires':time.time()+1800,'candidates':[],'entries':[],'position':0,'enumerated':False}
    async def current():
        if not authorized(actor):return False
        account=await s.repo.account(uid)
        if not account or not account['session'] or account['generation']!=generation:raise ValueError('La sesión cambió. Abre /menu telegram_id de nuevo.')
        raw=await s.repo.setting(context_key(s,actor))
        return bool(raw and json.loads(raw).get('private_token')==token)
    async def checkpoint():await s.repo.set_setting(scan_key(s,actor,uid),s.repo.encode(state).hex())
    await s.tell(actor,'🔎 Consultando chats privados y sus contadores de galería. Se prepara por tandas, sin leer todos los mensajes.')
    wait=None
    try:
        async with connected(s,uid) as client,asyncio.timeout(50):
            client.flood_sleep_threshold=0
            if not state['enumerated']:
                candidates=[];seen=set()
                scanned=0
                async for dialog in client.iter_dialogs():
                    scanned+=1
                    if scanned%50==1:
                        if not await current():return
                        if await users_busy(s):raise ValueError('Espera a que termine la descarga al DM y vuelve a abrir CHATS PRIVADOS.')
                    entity=dialog.entity
                    if not isinstance(entity,types.User) or entity.id in (uid,s.bot_id) or entity.id in seen:continue
                    seen.add(entity.id)
                    try:require_entity(entity,uid)
                    except ProtectedSource:continue
                    candidates.append({'id':entity.id,'name':title(entity,uid,entity.id),'access_hash':entity.access_hash or 0})
                state.update(candidates=candidates,enumerated=True)
                await checkpoint()
            for _ in range(8):
                if state['position']>=len(state['candidates']) or await users_busy(s):break
                if not await current():return
                peer=state['candidates'][state['position']]
                await s.repo.pace(f'enlace-read:{uid}',3.1)
                try:
                    counts=await client(functions.messages.GetSearchCountersRequest(peer=types.InputPeerUser(peer['id'],peer['access_hash']),filters=[types.InputMessagesFilterPhotoVideo()]))
                except (errors.PeerIdInvalidError,errors.UserIdInvalidError):
                    state['unavailable']=state.get('unavailable',0)+1
                    state['position']+=1
                    await checkpoint()
                    continue
                count=sum(c.count for c in counts)
                if count>0:
                    state['entries'].append({'id':peer['id'],'name':peer['name'],'count':count})
                    await s.repo.set_setting(peer_key(s,uid,generation,peer['id']),s.repo.encode({'uid':uid,'source':peer['id'],'generation':generation,'access_hash':peer['access_hash']}).hex())
                state['position']+=1
                await checkpoint()
    except errors.FloodWaitError as exc:
        await s.repo.set_setting(f'link-list-wait:{s.bot_id}:{uid}',time.time()+max(1,exc.seconds)+10)
        wait=f'Telegram pide esperar {exc.seconds} segundos; la consulta continuará donde quedó.'
    except TimeoutError:
        wait='La consulta alcanzó su límite de tiempo. Puedes continuar con CARGAR MÁS CHATS.'
    if not await current():return
    await checkpoint()
    base['entries']=state['entries']
    await s.repo.set_setting(context_key(s,actor),json.dumps(base))
    heading=f'𝐂𝐇𝐀𝐓𝐒 𝐏𝐑𝐈𝐕𝐀𝐃𝐎𝐒 · Cuenta {uid}\nEscribe un número para abrir su galería.\nCantidades de fotos y videos visibles en Telegram.\nRevisados: {state["position"]}/{len(state["candidates"])} chats.\n\n'
    text=heading
    for number,entry in enumerate(state['entries'],1):
        line=f'{number}. {entry["name"]} ({entry["id"]}) ({entry["count"]})\n'
        if len((text+line).encode('utf-16-le'))//2>3500:
            await s.tell(actor,text);text=heading
        text+=line
    if not state['entries']:text+='Todavía no se encontraron chats con fotos o videos.\n'
    if state.get('unavailable'):text+=f'\nChats que Telegram no permitió consultar: {state["unavailable"]}.\n'
    if wait:text+='\n'+wait
    keyboard=[]
    if not state['enumerated'] or state['position']<len(state['candidates']):keyboard.append([{'text':'𝐂𝐀𝐑𝐆𝐀𝐑 𝐌Á𝐒 𝐂𝐇𝐀𝐓𝐒','style':'primary','callback_data':f'manual:p:{uid}'}])
    keyboard.append([{'text':'𝐀𝐓𝐑Á𝐒','style':'danger','callback_data':f'manual:b:{uid}'}])
    await s.api('sendMessage',{'chat_id':actor,'text':text,'reply_markup':{'inline_keyboard':keyboard}})
