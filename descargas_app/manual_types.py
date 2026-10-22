"""Administrator gallery categories and explicit numbered selections."""
import json
import time
from copy import copy
from .link_listing import media_label
from .moderation import authorized

MODES={'p':'photo','v':'video','a':'album'}
NAMES={'photo':'foto','video':'video','album':'álbum'}


async def typed_action(s,actor,query,action,token,position):
    from .manual_board import open_board,enqueue
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,token)
    if not row or row['actor']!=actor or row['closed'] or row['message_id']!=query['message']['message_id'] or row['position']!=position:raise ValueError('Abre la botonera actual.')
    if row['busy']:raise ValueError('Espera a que termine la descarga actual.')
    mode=MODES[action[-1]];data=s.repo.decode(row['payload'])
    if action.startswith('n'):
        await s.repo.set_setting(f'manual-number:{s.bot_id}:{actor}',json.dumps({'token':token,'message_id':row['message_id'],'generation':data['generation'],'mode':mode,'expires':time.time()+1800}))
        return await s.tell(actor,f'Escribe el número de {NAMES[mode]} que quieres descargar. Ejemplo: 53. Es la posición en la lista del bot de ese tipo, no el ID del mensaje. Si faltan páginas, se cargarán sin descargar los elementos anteriores.')
    await open_board(s,actor,row['uid'],row['source'],query=data.get('query') or None,mode=mode)
    current=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND uid=$2 AND source=$3',s.bot_id,row['uid'],row['source'])
    await enqueue(s,actor,current['token'],current['position'],current['message_id'],batch=10)


async def number_input(s,actor,text):
    if not authorized(actor):return False
    key=f'manual-number:{s.bot_id}:{actor}';raw=await s.repo.setting(key)
    if not raw:return False
    pending=json.loads(raw)
    if pending['expires']<time.time():
        await s.repo.set_setting(key,'');return False
    if not text.isdecimal() or not 1<=int(text)<=1000000:raise ValueError('Escribe un número entre 1 y 1000000.')
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,pending['token'])
    if not row or row['closed'] or row['actor']!=actor or row['message_id']!=pending['message_id'] or s.repo.decode(row['payload'])['generation']!=pending['generation']:raise ValueError('La botonera cambió. Pulsa NÚMERO otra vez.')
    from .manual_board import open_board,enqueue
    data=s.repo.decode(row['payload'])
    await open_board(s,actor,row['uid'],row['source'],query=data.get('query') or None,mode=pending['mode'])
    current=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND uid=$2 AND source=$3',s.bot_id,row['uid'],row['source'])
    await enqueue(s,actor,current['token'],current['position'],current['message_id'],number=int(text))
    await s.repo.set_setting(key,'')
    return True


async def select_items(s,job,key,items):
    """Selection is fenced by the active durable board, never by callback hints."""
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,job['manual_token'])
    if not row or not row['busy'] or row['uid']!=job['uid'] or row['source']!=job['source']:raise ValueError('La selección ya no está activa.')
    data=s.repo.decode(row['payload']);position=row['position']
    if position>=len(data['entries']) or data['entries'][position]['mid']!=job['retry_message']:raise ValueError('La selección cambió.')
    mode=data.get('mode','')
    if mode=='album' and len(items)<2:raise ValueError('El álbum ya no contiene dos o más elementos disponibles.')
    if mode in ('photo','video'):
        candidates=items
        allowed=('Foto','Imagen') if mode=='photo' else ('Video',)
        items=[m for m in candidates if m.id in data['entries'][position]['ids'] and media_label(m) in allowed]
        if not items:raise ValueError('El elemento solicitado ya no está disponible en esa categoría.')
        # A native album often stores its shared caption on its first item only.
        caption=next((m for m in candidates if getattr(m,'message',None)),None)
        if caption:
            items=[copy(m) if not getattr(m,'message',None) else m for m in items]
            for m in items:
                if not getattr(m,'message',None):m.message=caption.message;m.entities=getattr(caption,'entities',None)
        key=(job['source'],job['retry_message'])
    return key,items
