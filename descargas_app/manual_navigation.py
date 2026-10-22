"""Durable previous/next controls bound to the latest numbered confirmation."""
import json
import uuid
from .moderation import authorized
from .manual_types import NAMES


def key(s,actor):return f'link-download-confirmation:{s.bot_id}:{actor}'


def buttons(context):
    token=context['id'];number=context['number']
    button=lambda label,action,style:{'text':label,'style':style,'callback_data':f'manual:{action}:{token}:{number}'}
    return [[button('𝐀𝐍𝐓𝐄𝐑𝐈𝐎𝐑','zp','success'),button('𝐒𝐈𝐆𝐔𝐈𝐄𝐍𝐓𝐄','zn','success')],
        [button('𝐀𝐓𝐑Á𝐒','zb','danger')],[button('𝐌𝐄𝐍Ú 𝐏𝐑𝐈𝐍𝐂𝐈𝐏𝐀𝐋','zm','primary')]]


async def confirm(s,actor,context,count,link):
    from .download_notices import latest_confirmation
    context=dict(context,id=uuid.uuid4().hex[:16])
    label=NAMES[context['mode']].capitalize()
    text=(f'{"✅" if context.get("saved",True) else "⏭"} {label} número {context["number"]} '+
        (('guardado en ALMACEN. Descarga y envío: 100%.' if count else 'ya estaba guardado en ALMACEN. Disponible: 100%.') if context.get('saved',True) else 'omitido por una exclusión.')+
        f'\nCuenta: {context["uid"]}\nArchivos nuevos guardados: {count}.\nEnlace de origen: {link}')
    return await latest_confirmation(s,actor,text,buttons=buttons(context),context=s.repo.encode(context).hex())


async def read(s,actor):
    raw=await s.repo.setting(key(s,actor))
    state=json.loads(raw) if raw else {}
    context=s.repo.decode(bytes.fromhex(state['navigation'])) if state.get('navigation') else None
    return state,context


async def restore_notice(s,actor,message_id,detail):
    try:
        async with s.repo.pool.acquire() as conn:
            await conn.execute('SELECT pg_advisory_lock(753,hashtext($1))',key(s,actor))
            try:
                state,context=await read(s,actor)
                if not context or state.get('last')!=message_id:return
                text=f'⏸ {detail[:450]}\nÚltimo elemento confirmado: {NAMES[context["mode"]]} {context["number"]}.'
                await s.api('editMessageText',{'chat_id':actor,'message_id':message_id,'text':text,'reply_markup':{'inline_keyboard':buttons(context)}})
            finally:await conn.execute('SELECT pg_advisory_unlock(753,hashtext($1))',key(s,actor))
    except Exception:pass


async def _callback(s,actor,query):
    if not authorized(actor) or query.get('message',{}).get('chat',{}).get('id')!=actor:return
    _,action,token,number=query['data'].split(':')
    state,context=await read(s,actor)
    message_id=query['message']['message_id']
    if not context or state.get('last')!=message_id or context['id']!=token or context['number']!=int(number):raise ValueError('Esta confirmación ya cambió. Usa la última.')
    account=await s.repo.account(context['uid'])
    if not account or not account['session'] or account['generation']!=context['generation']:raise ValueError('La sesión cambió. Abre /menu telegram_id.')
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND uid=$2 AND source=$3',s.bot_id,context['uid'],context['source'])
    if not row or row['actor']!=actor or row['closed']:raise ValueError('La botonera está cerrada. Abre /menu telegram_id.')
    if action in ('zb','zm'):
        from .manual_board import open_board,render,menu
        if action=='zb':
            if row['busy']:raise ValueError('Espera a que termine la descarga actual.')
            await open_board(s,actor,context['uid'],context['source'],query=context.get('query') or None,mode=context['mode'])
            row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND uid=$2 AND source=$3',s.bot_id,context['uid'],context['source'])
        async with s.repo.pool.acquire() as conn:
            await conn.execute('SELECT pg_advisory_lock(753,hashtext($1))',key(s,actor))
            try:
                current,current_context=await read(s,actor)
                if current.get('last')!=message_id or not current_context or current_context['id']!=token:raise ValueError('La confirmación ya cambió.')
                current.pop('navigation',None)
                if action=='zb':current.pop('last',None)
                await s.repo.set_setting(key(s,actor),json.dumps(current))
                if action=='zb':
                    old=row['message_id']
                    await s.repo.pool.execute('UPDATE descarga_manual_boards SET message_id=$3 WHERE bot_id=$1 AND token=$2',s.bot_id,row['token'],message_id)
                    await render(s,dict(row,message_id=message_id))
                    if old and old!=message_id:
                        try:await s.api('deleteMessage',{'chat_id':actor,'message_id':old})
                        except Exception:pass
                else:
                    await s.api('editMessageReplyMarkup',{'chat_id':actor,'message_id':message_id,'reply_markup':{'inline_keyboard':[]}})
                    await menu(s,actor,context['uid'])
            finally:await conn.execute('SELECT pg_advisory_unlock(753,hashtext($1))',key(s,actor))
        return
    if row['busy']:raise ValueError('Espera a que termine la descarga actual; no se duplicará la orden.')
    target=context['number']+(1 if action=='zn' else -1)
    if target<1:raise ValueError('Este es el primer elemento de la lista.')
    from .manual_board import open_board,enqueue,link
    await open_board(s,actor,context['uid'],context['source'],query=context.get('query') or None,mode=context['mode'])
    row=await s.repo.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND uid=$2 AND source=$3',s.bot_id,context['uid'],context['source'])
    data=s.repo.decode(row['payload'])
    if target>len(data['entries']) and data.get('complete'):raise ValueError('No hay otro elemento en esa dirección.')
    if target<=len(data['entries']) and data['entries'][target-1]['status'] in ('saved','skipped'):
        selected=data['entries'][target-1]
        return await confirm(s,actor,dict(context,number=target,saved=selected['status']=='saved'),0,link(row['source'],selected['mid']))
    await enqueue(s,actor,row['token'],row['position'],row['message_id'],number=target,notice_id=message_id)


async def callback(s,actor,query):
    if not authorized(actor) or query.get('message',{}).get('chat',{}).get('id')!=actor:return
    async with s.repo.pool.acquire() as conn:
        locked=await conn.fetchval('SELECT pg_try_advisory_lock(755,hashtext($1))',key(s,actor))
        if not locked:raise ValueError('La pulsación anterior sigue procesándose. Espera un momento.')
        try:return await _callback(s,actor,query)
        finally:await conn.execute('SELECT pg_advisory_unlock(755,hashtext($1))',key(s,actor))
