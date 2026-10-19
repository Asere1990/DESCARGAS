import os
from aiohttp import web
from descargas_app import mount
from descargas_app.scalable import ScaleService
os.umask(0o077)
os.environ['DESCARGAS_ROLE']='web'
app=web.Application(client_max_size=8192)
service=mount(app,prefix='',service_factory=ScaleService)
# Readiness is separate from the legacy informational health endpoint.
async def ready(request):
    return web.json_response({'ready':service.ready},status=200 if service.ready else 503)
app.router.add_get('/readyz',ready)
if __name__=='__main__':
    web.run_app(app,host='0.0.0.0',port=int(os.environ.get('PORT','10000')),access_log=None)
