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
        response=web.Response(text=(WEB/'index.html').read_text(),content_type='text/html')
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org; base-uri 'none'; form-action 'self'"
        response.headers['Referrer-Policy']='no-referrer'
        return response
    async def assets(request):
        name=request.match_info['name']
        if name not in ('app.js','style.css'):raise web.HTTPNotFound()
        return web.FileResponse(WEB/name,headers={'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff'})
    async def countries(request):
        return web.json_response([{'region':region,'code':phonenumbers.country_code_for_region(region)} for region in sorted(phonenumbers.SUPPORTED_REGIONS)])
    async def api(request):
        try:uid=validate_init_data(request.headers.get('X-Telegram-Init-Data',''),service.token)
        except Exception:return web.json_response({'error':'Abre esta Mini App desde @Descargas_Robot. Si ya lo hiciste, ciérrala y vuelve a abrirla.'},status=401)
        if not service.ready:return web.json_response({'error':'El servicio está terminando de configurarse. Inténtalo después.'},status=503)
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
                if operation=='admin-status':return web.json_response({'authorized':allowed},headers={'Cache-Control':'no-store'})
                if not allowed:raise web.HTTPForbidden()
                await service.admin_panel.update({'message':{'from':{'id':uid},'chat':{'id':uid,'type':'private'},'text':'/datos'}})
                return web.json_response({'ok':True},headers={'Cache-Control':'no-store'})
            if operation=='status':
                async with service.locks[uid]:result=await service.login_status(uid)
                return web.json_response(result,headers={'Cache-Control':'no-store'})
            if operation in ('membership','cancel-membership') and hasattr(service,'billing'):
                from .subscription_actions import summary,cancel
                result=await (summary(service,uid) if operation=='membership' else cancel(service,uid))
                return web.json_response(result,headers={'Cache-Control':'no-store'})
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
                return web.json_response({'phone':phone,'region':region,'national':national},headers={'Cache-Control':'no-store'})
            service.throttle(uid)
            if operation=='disconnect':
                await service.disconnect(uid)
                return web.json_response({'connected':False,'message':'Sesión cerrada y borrada.'})
            async with service.locks[uid]:
                if operation=='send-code':result=await service.send_code(uid,data)
                elif operation=='verify':result=await service.verify(uid,data)
                elif operation=='password':result=await service.verify(uid,data,password=True)
                else:raise web.HTTPNotFound()
            return web.json_response(result,headers={'Cache-Control':'no-store'})
        except web.HTTPException:raise
        except Exception as exc:return web.json_response({'error':error_message(exc)},status=400,headers={'Cache-Control':'no-store'})
    if hasattr(service,'billing'):
        from .billing import mount_payments
        mount_payments(app,service)
    if hasattr(service,'repo'):
        from .backup import mount_backup
        mount_backup(app,service)
    app.router.add_get(prefix+'/',index)
    app.router.add_get(prefix+'/healthz',health)
    app.router.add_get(prefix+'/countries',countries)
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
