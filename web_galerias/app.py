"""Mini App Web Galerías, montada aparte del panel y la Mini App existentes."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from aiohttp import web
from .core import GalleryCore,identifier
from .permissions import is_admin
from . import admin,downloads,payments

log=logging.getLogger("web-galerias")
ROOT=Path(__file__).resolve().parent
HEADERS={
 "Cache-Control":"no-store","X-Content-Type-Options":"nosniff",
 "Referrer-Policy":"no-referrer",
 "Content-Security-Policy":"default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data: blob: https:; media-src 'self' blob: https:; connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org; base-uri 'none'; form-action 'self'"
}

class Gallery(GalleryCore):
    async def api(self,request):
        uid=self.auth(request)
        self.db()
        if request.content_length and request.content_length>450_000:
            raise web.HTTPRequestEntityTooLarge(max_size=450_000,actual_size=request.content_length)
        data=await request.json()
        if not isinstance(data,dict):raise web.HTTPBadRequest(text="Solicitud inválida.")
        op=data.get("op")
        if op=="bootstrap":return await self.bootstrap(uid,data.get("name",""))
        if op=="checkout":return await payments.checkout(self,uid)
        if isinstance(op,str) and op.startswith("admin-"):
            return await admin.admin_op(self,uid,op,data)
        if not await self.entitled(uid):raise web.HTTPForbidden(text="Acceso no autorizado.")
        if op=="items":
            folder=identifier(data.get("folder"))
            cursor=identifier(data.get("cursor",0),0)
            if not isinstance(data.get("query",""),str):raise web.HTTPBadRequest(text="Consulta incorrecta.")
            return await self.list_items(uid,folder,data.get("query",""),cursor,
                                         lambda u,i,k:downloads.temporary_link(self,u,i,k))
        if op=="reserve":return await downloads.reserve(self,uid,data.get("ids"))
        raise web.HTTPNotFound()

def mount(app,service):
    g=Gallery(service)
    async def page(request):
        content=(ROOT/"index.html").read_text(encoding="utf-8")
        return web.Response(text=content,content_type="text/html",headers=HEADERS)
    async def static(request):
        name=request.match_info["name"]
        if name not in ("app.js","style.css"):raise web.HTTPNotFound()
        return web.FileResponse(ROOT/name,headers={
            "Cache-Control":"no-cache","X-Content-Type-Options":"nosniff"})
    async def api(request):
        try:return web.json_response(await g.api(request),headers={"Cache-Control":"no-store"})
        except web.HTTPException:raise
        except Exception as exc:
            log.error("Gallery API request failed (%s)",type(exc).__name__)
            raise web.HTTPServiceUnavailable(text="Operación no disponible. Inténtalo después.") from None
    async def asset(request):
        return await admin.asset(g,request)
    async def media(request):
        return await downloads.media(g,request)
    async def download(request):
        return await downloads.download(g,request)
    async def stripe(request):
        return await payments.webhook(g,request)
    app.router.add_get("/web-galerias",page)
    app.router.add_get("/web-galerias/",page)
    app.router.add_get("/web-galerias/static/{name}",static)
    app.router.add_get("/web-galerias/assets/{key}",asset)
    app.router.add_post("/web-galerias/api",api)
    app.router.add_get("/web-galerias/media/{item}/{kind:thumb|view}",media)
    app.router.add_get("/web-galerias/download/{ticket}",download)
    app.router.add_post("/web-galerias/stripe",stripe)
    async def startup(_):
        g.task=asyncio.create_task(g.setup())
    async def cleanup(_):
        g.closed=True
        for task in (g.sync_task,g.task):
            if task:task.cancel()
        await asyncio.gather(*(t for t in (g.sync_task,g.task) if t),return_exceptions=True)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return g
