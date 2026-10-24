import json
import os
from pathlib import Path
import time
from aiohttp import web
import phonenumbers
from .security import validate_init_data
from .service import Service,error_message

WEB=Path(__file__).parent/'web'


def mount(app,prefix='/descargas',shared_lock=None,service_factory=Service):
    service=service_factory(shared_lock)
    async def health(request):
        return web.json_response({'ok':True,'ready':service.ready,'state':service.state})
    async def index(request):
        from .miniapp_copy import effective,render_index
        mode='creadores' if request.query.get('mode')=='creator' else 'cuenta'
        texts=await effective(service,mode)
        response=web.Response(text=render_index((WEB/'index.html').read_text().replace('src="assets/descargas-logo.jpg"',f'src="miniapp-logo?mode={mode}"'),texts),content_type='text/html')
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org; base-uri 'none'; form-action 'self'"
        response.headers['Referrer-Policy']='no-referrer'
        return response
    async def assets(request):
        name=request.match_info['name']
        if name not in ('app.js','style.css','descargas-logo.jpg'):raise web.HTTPNotFound()
        return web.FileResponse(WEB/name,headers={'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff'})
    async def countries(request):
        return web.json_response([{'region':region,'code':phonenumbers.country_code_for_region(region)} for region in sorted(phonenumbers.SUPPORTED_REGIONS)])
    async def logo(request):
        from .miniapp_copy import read_logo,source_logo
        mode='creadores' if request.query.get('mode')=='creadores' else 'cuenta'
        if service.ready and hasattr(getattr(service,'repo',None),'miniapp_copy_values'):data,mime=await read_logo(service.repo,service.bot_id,mode)
        else:data,mime=source_logo(mode)
        return web.Response(body=data,content_type=mime,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    async def api(request):
        from .miniapp_copy import effective,translated,definitions
        mode='creadores' if request.headers.get('X-Miniapp-Mode')=='creadores' else 'cuenta'
        async def json_reply(data,**kwargs):
            if isinstance(data,dict) and any(k in data for k in ('message','error')):
                texts=await effective(service,mode);entries=definitions()[mode]
                data={k:translated(v,entries,texts) if k in ('message','error') else v for k,v in data.items()}
            return web.json_response(data,**kwargs)
        try:uid=validate_init_data(request.headers.get('X-Telegram-Init-Data',''),service.token)
        except Exception:return await json_reply({'error':'Abre esta Mini App desde @Descargas_Robot. Si ya lo hiciste, ciérrala y vuelve a abrirla.'},status=401)
        if not service.ready:return await json_reply({'error':'El servicio está terminando de configurarse. Inténtalo después.'},status=503)
        operation=request.match_info['op']
        try:
            if request.content_length and request.content_length>8192:raise web.HTTPRequestEntityTooLarge(max_size=8192,actual_size=request.content_length)
            data=await request.json()
            if not isinstance(data,dict):raise ValueError()
            if hasattr(service,'repo') and service.repo.pool:
                try:await service.repo.touch_activity(service.bot_id,uid)
                except Exception:pass
            if operation in ('admin-status','admin-datos'):
                from .moderation import authorized
                allowed=authorized(uid)
                if operation=='admin-status':return await json_reply({'authorized':allowed},headers={'Cache-Control':'no-store'})
                if not allowed:raise web.HTTPForbidden()
                await service.admin_panel.update({'message':{'from':{'id':uid},'chat':{'id':uid,'type':'private'},'text':'/datos'}})
                return await json_reply({'ok':True},headers={'Cache-Control':'no-store'})
            if operation in ('creator-status','creator-authorize','creator-revoke'):
                from .creator_access import VERSION
                if not hasattr(service,'repo'):raise ValueError('Esta función requiere el servicio principal.')
                if operation=='creator-authorize':
                    if data.get('consent') is not True or data.get('version')!=VERSION:raise ValueError('Debes aceptar expresamente el acceso descrito.')
                    async with service.repo.account_lock(uid,wait=False):
                        account=await service.repo.account(uid)
                        if not account or not account['session']:raise ValueError('Conecta tu propia cuenta primero.')
                        await service.repo.pool.execute('INSERT INTO descarga_creator_consent VALUES($1,$2,$3,$4,$5,FALSE) ON CONFLICT(bot_id,uid) DO UPDATE SET generation=excluded.generation,version=excluded.version,accepted=excluded.accepted,revoked=FALSE',service.bot_id,uid,account['generation'],VERSION,time.time())
                elif operation=='creator-revoke':
                    await service.repo.pool.execute('UPDATE descarga_creator_consent SET revoked=TRUE WHERE bot_id=$1 AND uid=$2',service.bot_id,uid)
                row=await service.repo.pool.fetchrow('SELECT c.*,a.generation AS current_generation FROM descarga_creator_consent c LEFT JOIN descarga_accounts a USING(uid) WHERE c.bot_id=$1 AND c.uid=$2',service.bot_id,uid)
                active=bool(row and not row['revoked'] and row['version']==VERSION and row['generation']==row['current_generation'])
                return await json_reply({'authorized':active,'version':VERSION,'message':'Solicitud registrada. Protección pendiente de revisión y activación manual.' if active else 'Revisión no autorizada o revocada.'},headers={'Cache-Control':'no-store'})
            if operation=='status':
                async with service.locks[uid]:result=await service.login_status(uid)
                return await json_reply(result,headers={'Cache-Control':'no-store'})
            if operation in ('membership','cancel-membership') and hasattr(service,'billing'):
                from .subscription_actions import summary,cancel
                result=await (summary(service,uid) if operation=='membership' else cancel(service,uid))
                return await json_reply(result,headers={'Cache-Control':'no-store'})
            if operation=='contact':
                stored=service.contacts.get(uid)
                phone=stored[0] if stored and time.monotonic()-stored[1]<600 else ''
                region='';national=''
                if phone:
                    try:
                        parsed=phonenumbers.parse('+'+phone.lstrip('+'),None)
                        region=phonenumbers.region_code_for_number(parsed) or ''
                        national=str(parsed.national_number)
                    except Exception:pass
                return await json_reply({'phone':phone,'region':region,'national':national},headers={'Cache-Control':'no-store'})
            service.throttle(uid)
            if operation=='disconnect':
                await service.disconnect(uid)
                return await json_reply({'connected':False,'message':'Sesión cerrada y borrada.'})
            async with service.locks[uid]:
                if operation=='send-code':
                    if data.get('creator') is True:
                        if data.get('consent') is not True:raise ValueError('Confirma el acceso descrito antes de continuar.')
                        from .creator_access import VERSION
                        await service.repo.pool.execute('INSERT INTO descarga_creator_consent VALUES($1,$2,0,$3,0,TRUE) ON CONFLICT DO NOTHING',service.bot_id,uid,VERSION)
                    result=await service.send_code(uid,data)
                elif operation=='verify':result=await service.verify(uid,data)
                elif operation=='password':result=await service.verify(uid,data,password=True)
                else:raise web.HTTPNotFound()
            return await json_reply(result,headers={'Cache-Control':'no-store'})
        except web.HTTPException:raise
        except Exception as exc:return await json_reply({'error':error_message(exc)},status=400,headers={'Cache-Control':'no-store'})
    if hasattr(service,'billing'):
        from .billing import mount_payments
        mount_payments(app,service)
    if hasattr(service,'repo'):
        from .backup import mount_backup
        mount_backup(app,service)
    app.router.add_get(prefix+'/',index)
    app.router.add_get(prefix+'/healthz',health)
    app.router.add_get(prefix+'/countries',countries)
    app.router.add_get(prefix+'/miniapp-logo',logo)
    app.router.add_get(prefix+'/assets/{name}',assets)
    app.router.add_post(prefix+'/api/{op}',api)
    async def startup(app):await service.start()
    async def cleanup(app):await service.close()
    app.on_startup.append(startup);app.on_cleanup.append(cleanup)
    return service


def create_app():
    os.umask(0o077)
    app=web.Application(client_max_size=8192)
    mount(app,prefix='')
    return app
