"""Private owner-controlled copy editor. It never opens Telegram user sessions."""
import asyncio
import html
import json
import re
import time
import uuid
from .creator_access import police
from .miniapp_copy import MODES,CATEGORIES,definitions,values,save,save_logo


def key(s,actor):return f'miniapp-editor:{s.bot_id}:{actor}'
async def clear(s,actor):await s.repo.set_setting(key(s,actor),'')
async def state(s,actor):
    raw=await s.repo.setting(key(s,actor))
    try:data=json.loads(raw) if isinstance(raw,str) else None
    except ValueError:return None
    return data if isinstance(data,dict) and data.get('kind')=='miniapp-copy' and data.get('expires',0)>time.time() else None

async def put(s,actor,data):
    data=dict(data,kind='miniapp-copy',expires=time.time()+1800,token=uuid.uuid4().hex[:12])
    await s.repo.set_setting(key(s,actor),json.dumps(data));return data

def button(data,label,action,style='primary'):
    return {'text':label,'callback_data':f'copy:{data["token"]}:{action}','style':style}

async def choose_app(panel,actor):
    if not police(actor):return
    from .admin_home import clear as clear_accounts
    await clear_accounts(panel.s,actor)
    from .creator_search import pending_key
    await panel.s.repo.set_setting(pending_key(panel.s,actor),'')
    data=await put(panel.s,actor,{'stage':'app'})
    await panel.send(actor,'<b>EDITAR MINIAPP</b>\nSelecciona cuál quieres editar.',[
        [button(data,'CREAR CUENTA','app:cuenta')],
        [button(data,'PROTECCIÓN A CREADORES','app:creadores')],
        [button(data,'CERRAR','close','danger')]])

async def categories(panel,actor,mode):
    if mode not in MODES:raise ValueError('Miniapp no válida.')
    data=await put(panel.s,actor,{'mode':mode,'stage':'categories'})
    available={item['category'] for item in definitions()[mode].values()}
    rows=[[button(data,label,'cat:'+cat)] for cat,label in CATEGORIES.items() if cat in available]
    rows+=[[button(data,'🖼 CAMBIAR LOGO','logo')],[button(data,'ATRÁS','apps','danger')]]
    await panel.send(actor,'<b>'+html.escape(MODES[mode])+'</b>\nSelecciona el grupo de textos o cambia el logo. Solo se modifica el contenido, conservando tamaños y formatos.',rows)

async def listing(panel,actor,mode,category,page=0):
    if category not in CATEGORIES:raise ValueError('Categoría no válida.')
    entries=definitions()[mode];keys=[k for k,v in entries.items() if v['category']==category]
    count=len(keys);page=max(0,min(page,max(0,(count-1)//30)))
    data=await put(panel.s,actor,{'mode':mode,'category':category,'keys':keys,'stage':'list','page':page})
    current=await values(panel.s.repo,panel.s.bot_id,mode)
    lines=[]
    for index in range(page*30,min((page+1)*30,count)):
        k=keys[index];label=entries[k]['label'];text=current[k]
        preview=text[:160]+('…' if len(text)>160 else '')
        lines.append(f'<b>{index+1}. {html.escape(label)}</b>\n{html.escape(preview or "(vacío)")}')
    await panel.pages(actor,'<b>'+html.escape(MODES[mode]+' · '+CATEGORIES[category])+'</b>',lines)
    navigation=[]
    if page:navigation.append(button(data,'ANTERIOR','page:'+str(page-1)))
    if (page+1)*30<count:navigation.append(button(data,'SIGUIENTE','page:'+str(page+1)))
    rows=([navigation] if navigation else [])+[[button(data,'ATRÁS','categories','danger')]]
    await panel.send(actor,f'Escribe el <b>número</b> del texto que quieres editar. Total: {count}.\n/menu sale del editor.',rows)

async def photo_bytes(s,message):
    photos=message.get('photo') or []
    media=max(photos,key=lambda p:(p.get('width',0)*p.get('height',0),p.get('file_size',0))) if photos else message.get('document')
    if not media:raise ValueError('Envía el logo como foto o como archivo PNG, JPG, WEBP o GIF.')
    if media.get('file_size',0)>5*1024*1024:raise ValueError('El logo debe ocupar como máximo 5 MB.')
    info=await s.api('getFile',{'file_id':media['file_id']});path=info.get('file_path','')
    if not re.fullmatch(r'[A-Za-z0-9_./-]+',path) or '..' in path.split('/'):raise ValueError('No se pudo recuperar el archivo.')
    data=bytearray()
    async with s.http.get(f'https://api.telegram.org/file/bot{s.token}/{path}') as response:
        if response.status!=200:raise ValueError('No se pudo descargar el logo. Vuelve a enviarlo.')
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data)>5*1024*1024:raise ValueError('El logo debe ocupar como máximo 5 MB.')
    return bytes(data)

async def handle(panel,update):
    query=update.get('callback_query',{});message=query.get('message',{}) if query else update.get('message',{})
    actor=(query.get('from',{}) if query else message.get('from',{})).get('id')
    text=message.get('text','') if not query else query.get('data','')
    if not actor or message.get('chat',{}).get('type')!='private' or message.get('chat',{}).get('id')!=actor:return False
    is_callback=bool(query and text.startswith('copy:'))
    # No policy files or database writes are consulted for ordinary users.
    if not police(actor):
        if is_callback:
            await panel.s.api('answerCallbackQuery',{'callback_query_id':query['id'],'text':'Solo police.txt puede editar las miniapps.'});return True
        return False
    data=await state(panel.s,actor)
    if query and not is_callback and data:
        await clear(panel.s,actor);return False
    if not query and text.strip() in ('/menu','/cancelar'):
        if data:await clear(panel.s,actor)
        if text.strip()=='/cancelar' and data:
            await panel.send(actor,'Edición cancelada.');return True
        return False
    if not is_callback and (query or not data or data.get('stage') not in ('list','value','logo')):return False
    if is_callback:await panel.s.api('answerCallbackQuery',{'callback_query_id':query['id']})
    # Existing task limits and bounded SQL pool isolate the editor from downloading.
    if len(panel.tasks)>=20:
        await panel.send(actor,'Hay varias consultas en curso. Inténtalo en unos segundos.');return True
    task=asyncio.create_task(run(panel,actor,data,text,message,update.get('update_id'),is_callback));panel.tasks.add(task);task.add_done_callback(panel.tasks.discard)
    return True

async def run(panel,actor,data,text,message,update_id,is_callback):
    locks=getattr(panel,'_copy_locks',None)
    if locks is None:locks={};panel._copy_locks=locks
    lock=locks.setdefault(actor,asyncio.Lock())
    async with panel.slots,lock:
        try:
            if not police(actor):return
            # A second edit/old button cannot reuse a consumed conversation step.
            current=await state(panel.s,actor)
            if text=='copy:open':return await choose_app(panel,actor)
            if not current or not data or current['token']!=data['token']:raise ValueError('Este editor cambió o venció. Abre EDITAR MINIAPP otra vez.')
            if is_callback:
                parts=text.split(':',2)
                if len(parts)!=3 or parts[1]!=data['token']:raise ValueError('Ese botón ya venció. Usa el último menú del editor.')
                action=parts[2]
                if action=='close':
                    await clear(panel.s,actor);return await panel.send(actor,'Editor cerrado.')
                if action=='apps':return await choose_app(panel,actor)
                if action.startswith('app:'):return await categories(panel,actor,action.split(':')[1])
                if action=='categories':return await categories(panel,actor,data['mode'])
                if action.startswith('cat:'):return await listing(panel,actor,data['mode'],action.split(':')[1])
                if action.startswith('page:'):return await listing(panel,actor,data['mode'],data['category'],int(action.split(':')[1]))
                if action=='logo':
                    data=await put(panel.s,actor,dict(data,stage='logo'))
                    return await panel.send(actor,'Envía el nuevo <b>logo</b> como foto o archivo PNG/JPG/WEBP/GIF (máximo 5 MB). Se conserva el tamaño mostrado en la Mini App.',[[button(data,'ATRÁS','categories','danger')]])
                return
            if data['stage']=='list':
                if not text.strip().isdecimal():raise ValueError('Escribe el número del texto mostrado en la lista.')
                number=int(text.strip())
                if not 1<=number<=len(data['keys']):raise ValueError('Ese número no está en esta categoría.')
                field=data['keys'][number-1];item=definitions()[data['mode']][field]
                value=(await values(panel.s.repo,panel.s.bot_id,data['mode']))[field]
                data=await put(panel.s,actor,dict(data,stage='value',field=field))
                for offset in range(0,len(value),1200):await panel.send(actor,'<b>TEXTO ACTUAL</b>\n'+html.escape(value[offset:offset+1200]))
                return await panel.send(actor,'Escribe el <b>nuevo texto</b>. Se conserva el formato y tamaño.\nLos marcadores como {until} son datos variables. Usa /vacio únicamente para campos que ya permiten estar vacíos.',[[button(data,'ATRÁS','cat:'+data['category'],'danger')]])
            if data['stage']=='value':
                if not isinstance(text,str) or not text:raise ValueError('Envía el nuevo texto como mensaje.')
                value='' if text.strip()=='/vacio' else text
                changed=await save(panel.s.repo,panel.s.bot_id,actor,data['mode'],data['field'],value,update_id)
                await clear(panel.s,actor)
                await panel.send(actor,'✅ Texto guardado. Se verá al reabrir la Mini App.' if changed else 'Ese cambio ya se procesó. Se muestra el texto actual.')
                return await listing(panel,actor,data['mode'],data['category'],data.get('page',0))
            if data['stage']=='logo':
                content=await photo_bytes(panel.s,message)
                if not police(actor):return
                await save_logo(panel.s.repo,panel.s.bot_id,actor,data['mode'],content,update_id)
                await clear(panel.s,actor)
                await panel.send(actor,'✅ Logo guardado. Se verá al reabrir la Mini App.')
                return await categories(panel,actor,data['mode'])
        except asyncio.CancelledError:raise
        except ValueError as exc:await panel.send(actor,html.escape(str(exc)))
        except Exception:await panel.send(actor,'No se pudo completar la edición. Inténtalo de nuevo; no se anunció ningún cambio como guardado.')
