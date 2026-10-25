"""Fast administrator entry menu and snapshot-bound account number selection."""
import asyncio
import json
import time
import uuid
from .moderation import authorized


def key(s,actor):return f'admin-account-choice:{s.bot_id}:{actor}'


async def clear(s,actor):await s.repo.set_setting(key(s,actor),'')


async def home(panel,actor):
    from .miniapp_editor import clear as clear_editor
    await clear_editor(panel.s,actor)
    from .creator_search import pending_key
    await panel.s.repo.set_setting(pending_key(panel.s,actor),'')
    if not authorized(actor):return
    from .manual_board import context_key
    from .group_admin import clear as clear_group
    await clear_group(panel.s,actor)
    for field in ('manual-number','manual-search','manual-anchor'):
        await panel.s.repo.set_setting(f'{field}:{panel.s.bot_id}:{actor}','')
    await panel.s.repo.set_setting(context_key(panel.s,actor),'')
    await panel.s.repo.set_setting(key(panel.s,actor),json.dumps({'home':True,'expires':time.time()+1800}))
    from .creator_access import reviewer
    if reviewer(actor):
        import html
        profile=await panel.s.api('getChat',{'chat_id':actor})
        first=profile.get('first_name') or 'Usuario'
        from .web_panel import access_link
        rows=[[{'text':'👨🏻‍💻𝐂𝐔𝐄𝐍𝐓𝐀𝐒','style':'success','url':await access_link(panel.s,actor)}]]
        return await panel.send(actor,html.escape(first)+' tienes todos los permisos del sistema.',rows)
    users=accounts='No disponible';downloads=None
    try:
        async with asyncio.timeout(1):
            row=await panel.pool.fetchrow('''SELECT
                (SELECT count(*) FROM descarga_directory WHERE bot_id=$1) AS users,
                (SELECT count(*) FROM descarga_accounts WHERE session IS NOT NULL) AS accounts''',panel.s.bot_id)
            users=row['users'];accounts=row['accounts']
    except TimeoutError:pass
    try:
        # Sum existing counters, never scan files, messages, or delivery events.
        async with asyncio.timeout(.25):
            downloads=await panel.pool.fetchval('SELECT COALESCE(sum(total),0) FROM descarga_user_totals WHERE bot_id=$1',panel.s.bot_id)
    except TimeoutError:pass
    if not authorized(actor):return
    text=f'𝐔𝐒𝐔𝐀𝐑𝐈𝐎𝐒: {users}\n𝐂𝐔𝐄𝐍𝐓𝐀𝐒: {accounts}'
    if downloads is not None:text+=f'\n𝐃𝐄𝐒𝐂𝐀𝐑𝐆𝐀𝐒: {downloads}'
    text+='\n\nUsuarios registrados en el DM; cuentas con sesión guardada. Telegram no informa si mantienen el chat abierto ni confirma aquí si revocaron la sesión.'
    if downloads is not None:text+='\nDescargas: archivos entregados al DM, incluidos los de caché.'
    rows=[[{'text':'𝐂𝐔𝐄𝐍𝐓𝐀𝐒','style':'primary','callback_data':'stats:account_menu_list'}]]
    if getattr(panel.s,'cipher',None) and getattr(panel.s,'url',None):
        from .web_panel import access_link
        rows.append([{'text':'🖥 PANEL WEB · CHATS','style':'success','url':await access_link(panel.s,actor)}])
    from .creator_access import police
    if police(actor):
        rows.extend([[{'text':label,'callback_data':'policy:'+name,'style':'primary'}] for label,name in [('PERMISOS.TXT','permisos.txt'),('PROTEGIDOS','protegidos'),('CUENTAS.TXT','cuentas.txt')]])
    if police(actor):rows.append([{'text':'✏️ EDITAR MINIAPP','style':'primary','callback_data':'copy:open'}])
    await panel.send(actor,text,rows)


async def begin(panel,actor):
    token=uuid.uuid4().hex
    await panel.s.repo.set_setting(key(panel.s,actor),json.dumps({'listing':token,'expires':time.time()+1800}))
    return token


async def remember(panel,actor,rows,token=None):
    if token:
        current=await panel.s.repo.setting(key(panel.s,actor))
        if not current or json.loads(current).get('listing')!=token:return False
    await panel.s.repo.set_setting(key(panel.s,actor),json.dumps({'expires':time.time()+1800,
        'snapshot':panel.s.repo.encode({'numbers':{str(r['number']):r['uid'] for r in rows}}).hex()}))
    return True


async def select(panel,actor,text):
    if not authorized(actor):return False
    saved=await panel.s.repo.setting(key(panel.s,actor))
    if not saved:return False
    data=json.loads(saved)
    if data.get('expires',0)<=time.time():
        await panel.send(actor,'La lista de cuentas venció. Abre /menu y pulsa CUENTAS otra vez.');return True
    if data.get('listing'):
        await panel.send(actor,'Se está preparando la lista de cuentas. Espera a que aparezca para elegir un número.');return True
    if data.get('home'):
        await panel.send(actor,'Pulsa CUENTAS para elegir una cuenta de la lista.');return True
    snapshot=panel.s.repo.decode(bytes.fromhex(data['snapshot']))
    uid=snapshot['numbers'].get(str(int(text)))
    if uid is None:
        await panel.send(actor,'Ese número no está en la página mostrada. Usa /cuentas 101-200 para consultar otro rango.');return True
    # Resolve the displayed snapshot, not a newly sorted directory that may change.
    account=await panel.s.repo.account(uid)
    if not account or not account['session']:
        await panel.send(actor,'Esa cuenta ya no tiene sesión guardada. Vuelve a abrir CUENTAS.');return True
    await clear(panel.s,actor)
    from .manual_board import menu
    await menu(panel.s,actor,uid)
    return True
