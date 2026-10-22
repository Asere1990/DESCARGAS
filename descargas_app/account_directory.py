"""Bounded private administrator listings; no account sessions opened for profiles."""
import asyncio
import html
import json
import re
import time
from telethon import types,utils


def page_range(argument, single=False):
    argument=argument.strip()
    if not argument and not single:return 1,100
    pattern=r'([1-9][0-9]{0,8})' if single else r'([1-9][0-9]{0,8})\s*-\s*([1-9][0-9]{0,8})'
    match=re.fullmatch(pattern,argument)
    if not match:raise ValueError('Indica un número positivo.' if single else 'Indica un rango, por ejemplo 245-344 (máximo 100).')
    start=int(match[1]);end=start if single else int(match[2])
    if end<start or end-start>=100:raise ValueError('El rango debe contener entre 1 y 100 resultados, en orden ascendente.')
    return start,end


async def accounts(panel,actor,argument,single=False):
    from .admin_panel import person,country_name
    from .moderation import authorized
    try:start,end=page_range(argument,single)
    except ValueError as exc:return await panel.send(actor,str(exc))
    # Only identifiers/profiles are read, never session ciphertext. Deterministic
    # numbering by Telegram ID; reconnecting does not reorder existing accounts.
    rows=await panel.pool.fetch('''WITH connected AS (
        SELECT a.uid,a.profile,d.name,d.country,
        row_number() OVER(ORDER BY a.uid) AS number,count(*) OVER() AS total
        FROM descarga_accounts a LEFT JOIN descarga_directory d ON d.uid=a.uid AND d.bot_id=$1
        WHERE a.session IS NOT NULL)
        SELECT * FROM connected WHERE number BETWEEN $2 AND $3 ORDER BY number''',panel.s.bot_id,start,end)
    total=rows[0]['total'] if rows else await panel.pool.fetchval('SELECT count(*) FROM descarga_accounts WHERE session IS NOT NULL')
    cache=getattr(panel,'_account_names',{})
    panel._account_names=cache
    slots=asyncio.Semaphore(2)
    async def entry(row):
        uid=row['uid'];profile=panel.s.repo.decode(row['profile']) if row['profile'] else {}
        stored=panel.s.repo.decode(row['name']) if row['name'] else {}
        name=stored.get('name') or profile.get('name') or str(uid)
        username=profile.get('username');known='username' in profile
        cached=cache.get(uid)
        if cached and cached[0]>time.time():
            name,username=cached[1:];known=True
        else:
            async with slots:
                if not authorized(actor):return ''
                try:
                    # getChat uses the bot, not the user's logged-in session.
                    await panel.s.repo.pace('admin-account-profiles',0.2)
                    async with asyncio.timeout(8):info=await panel.s.api('getChat',{'chat_id':uid})
                    name=' '.join(filter(None,[info.get('first_name'),info.get('last_name')])) or name
                    username=info.get('username');known=True
                    if len(cache)>=2048:cache.pop(next(iter(cache)))
                    cache[uid]=(time.time()+1800,name,username)
                except asyncio.CancelledError:raise
                except Exception:pass
        user='@'+username if username else 'Sin username' if known else 'No disponible'
        return (f'{row["number"]}. {person(uid,name)}\nUsername: {html.escape(user)}\n'
            f'Telegram ID: <code>{uid}</code>\nTeléfono: {html.escape(profile.get("phone") or "Sin datos")}\n'
            f'País: {html.escape(country_name(profile.get("phone_country") or row["country"] or "ZZ"))}')
    entries=await asyncio.gather(*(entry(row) for row in rows))
    if not authorized(actor):return
    shown=f'{start}-{rows[-1]["number"]}' if rows else 'Sin resultados en ese rango'
    title=f'<b>CUENTAS CON SESIÓN GUARDADA</b>\nTotal: {total}\nMostrando: {shown}\nOrden: Telegram ID ascendente.'
    if end<total:title+=f'\nSiguiente: /cuentas {end+1}-{end+100}'
    title+='\nLa numeración puede cambiar si se conectan o desconectan cuentas. No verifica si Telegram revocó la sesión.'
    await panel.pages(actor,title,[e for e in entries if e])


async def destinations(service,actor,argument):
    from .moderation import authorized
    from .account_chat import connected,selection_key
    if not authorized(actor):return
    match=re.fullmatch(r'([1-9][0-9]{0,15})(?:\s+(.+))?',argument.strip())
    if not match:return await service.tell(actor,'Uso: /grupos telegram_id [101-200]. También: /canales telegram_id [101-200].')
    uid=int(match[1])
    try:
        start,end=page_range(match[2] or '')
        account=await service.repo.account(uid)
        if not account or not account['session']:raise ValueError('Esa cuenta no está conectada al bot.')
        key=selection_key(service,actor,uid)
        saved=await service.repo.setting(key)
        data=json.loads(saved) if saved else {}
        entries=None
        if data.get('roles_version')==1 and data.get('expires',0)>time.time() and data.get('generation')==account['generation'] and data.get('snapshot'):
            entries=service.repo.decode(bytes.fromhex(data['snapshot']))['entries']
        if entries is None:
            cooldown=await service.repo.pool.fetchval('SELECT max(resume_at) FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2',service.bot_id,uid) or 0
            if cooldown>time.time():raise ValueError(f'Telegram pide esperar todavía {int(cooldown-time.time())+1} segundos para consultar esa cuenta.')
            # A snapshot is published only after the entire enumeration succeeds.
            # Failed/partial enumerations never claim to be a complete total.
            entries=[];seen=set();scanned=0
            async with connected(service,uid) as client,asyncio.timeout(300):
                async for dialog in client.iter_dialogs():
                    if not authorized(actor):return
                    scanned+=1
                    if scanned%100==0:
                        await service.repo.pace(f'admin-dialogs:{uid}',3.1)
                    entity=dialog.entity
                    if not isinstance(entity,(types.Chat,types.Channel)):continue
                    if any(getattr(entity,flag,False) for flag in ('left','kicked','deactivated')) or getattr(entity,'migrated_to',None):continue
                    source=utils.get_peer_id(entity)
                    if source in seen:continue
                    seen.add(source)
                    from .group_admin import entity_snapshot
                    entries.append({'id':source,'account_rights':entity_snapshot(entity),'name':' '.join((getattr(entity,'title',None) or 'Sin nombre').split())[:128],
                        'kind':'Canal' if getattr(entity,'broadcast',False) else 'Grupo',
                        'role_icon':'⭐️' if getattr(entity,'creator',False) else '👨🏻‍💻' if getattr(entity,'admin_rights',None) else ''})
            current=await service.repo.account(uid)
            if not current or not current['session'] or current['generation']!=account['generation']:
                raise ValueError('La sesión cambió durante la consulta. Vuelve a intentarlo.')
            entries.sort(key=lambda entry:entry['id'])
            data={'ids':[e['id'] for e in entries],'expires':time.time()+1800,'generation':account['generation'],
                'roles_version':1,'snapshot':service.repo.encode({'entries':entries}).hex()}
            await service.repo.set_setting(key,json.dumps(data))
        if not authorized(actor):return
        total=len(entries);selected=entries[start-1:end]
        header=f'📋 GRUPOS Y CANALES · Cuenta {uid}\nTotal: {total}\n'
        header+=f'Mostrando: {start}-{start+len(selected)-1}\n' if selected else 'Sin resultados en ese rango.\n'
        header+='Lista guardada durante 30 minutos.\n\n'
        text=header
        for number,entry in enumerate(selected,start):
            line=f'{number}. {(entry.get("role_icon","")+" ") if entry.get("role_icon") else ""}{entry["name"]} · {entry["kind"]}\nID: {entry["id"]}\n/descargar {uid} {entry["id"]}\n\n'
            if len((text+line).encode('utf-16-le'))//2>3500:
                await service.tell(actor,text);text=header
            text+=line
        text+=f'Permisos: {uid},número de lista.\n'
        if end<total:text+=f'Siguiente: /grupos {uid} {end+1}-{end+100}\n'
        await service.tell(actor,text)
    except asyncio.CancelledError:raise
    except Exception as exc:
        from .service import error_message
        detail=str(exc) if isinstance(exc,ValueError) else error_message(exc)
        await service.tell(actor,f'No se pudo completar la lista de la cuenta {uid}. {detail}')
