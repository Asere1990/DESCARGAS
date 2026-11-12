"""Mini App Web Galerías, montada aparte del panel y la Mini App existentes."""
import asyncio
import io
import json
import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from aiohttp import web
from PIL import Image, ImageDraw
from .core import GalleryCore,identifier
from .permissions import is_admin
from . import admin,admin_browser,downloads,payments,personal,covers,intro_video,location,user_features,download_plus

log=logging.getLogger("web-galerias")
ROOT=Path(__file__).resolve().parent

@lru_cache(maxsize=3)
def app_icon(size):
    # Icono propio y neutro para la pantalla de inicio; no contiene datos privados.
    art=Image.new("RGB",(size,size),(14,20,36))
    d=ImageDraw.Draw(art)
    radius=int(size*.19)
    d.rounded_rectangle((int(size*.05),int(size*.05),
                         int(size*.95),int(size*.95)),radius=radius,fill=(28,102,180))
    pad=int(size*.20)
    side=int(size*.24)
    gap=int(size*.07)
    for row in range(2):
        for col in range(2):
            x=pad+col*(side+gap)
            y=pad+row*(side+gap)
            d.rounded_rectangle((x,y,x+side,y+side),radius=int(size*.044),
                                fill=(245,249,255) if row==0 and col==0 else (182,222,250))
    out=io.BytesIO()
    art.save(out,format="PNG")
    return out.getvalue()
HEADERS={
 "Cache-Control":"no-store","X-Content-Type-Options":"nosniff",
 "Referrer-Policy":"no-referrer","X-Robots-Tag":"noindex, nofollow, noarchive",
 "Content-Security-Policy":"default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data: blob: https:; media-src 'self' blob: https:; connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org; base-uri 'none'; form-action 'self'"
}

class Gallery(GalleryCore):
    async def api(self,request):
        uid=await self.auth(request)
        self.db()
        if request.content_length and request.content_length>450_000:
            raise web.HTTPRequestEntityTooLarge(max_size=450_000,actual_size=request.content_length)
        data=await request.json()
        if not isinstance(data,dict):raise web.HTTPBadRequest(text="Solicitud inválida.")
        op=data.get("op")
        if op=="bootstrap":return await self.bootstrap(uid,data.get("name",""))
        if op=="presence":
            # Heartbeat only from an authenticated, entitled viewer, never
            # from a public/unverified web request or an idle home screen.
            import time
            view=data.get("view")
            if view not in ("library","album","viewer"):
                raise web.HTTPBadRequest(text="Vista no válida.")
            if not await self.entitled(uid):
                raise web.HTTPForbidden(text="Acceso no autorizado.")
            await self.db().execute("""
                INSERT INTO wg_gallery_presence(uid,updated,view)
                VALUES($1,$2,$3) ON CONFLICT(uid) DO UPDATE
                  SET updated=excluded.updated,view=excluded.view""",
                uid,time.time(),view)
            return {"ok":True}
        if op=="checkout":
            if await self.db().fetchval(
                    "SELECT blocked FROM wg_access_blocks WHERE uid=$1",uid):
                raise web.HTTPForbidden(
                    text="Cuenta restringida. Contacta con soporte.")
            return await payments.checkout(self,uid)
        if isinstance(op,str) and op.startswith("admin-"):
            return await admin.admin_op(self,uid,op,data)
        if not await self.entitled(uid):raise web.HTTPForbidden(text="Acceso no autorizado.")
        if isinstance(op,str) and op.startswith("personal-"):
            return await personal.operation(self,uid,op,data)
        if op=="offers-link":
            # Link resolution is read-only and separately HMAC-signed to the
            # real CUSTOMER-SERVICE bot; no Telegram username guessed by JS.
            import re
            from .customer_bridge import ready as bridge_ready,post_signed
            if not bridge_ready():
                raise web.HTTPServiceUnavailable(text="Nuevas Ofertas no disponible.")
            try:
                result=await post_signed("offers-link",{"telegram_id":uid},timeout=5)
                url=str(result.get("url") or "")
                if not re.fullmatch(
                    r"https://t[.]me/[A-Za-z][A-Za-z0-9_]{4,31}"
                    r"(?:/[A-Za-z0-9_]{1,64})?[?]startapp=offers",url
                ):
                    raise ValueError("Invalid offers link")
            except Exception as exc:
                log.warning("Offers Mini App link unavailable (%s)",type(exc).__name__)
                raise web.HTTPServiceUnavailable(text="No se pudo abrir Nuevas Ofertas.") from None
            return {"url":url}
        if op=="user-collections":return await user_features.collections(self,uid)
        if op=="progress-save":return await user_features.save_progress(self,uid,data)
        if op=="favorite-set":return await user_features.set_favorite(self,uid,data)
        if op=="favorites-items":return await user_features.items(self,uid,data)
        if op=="items":
            cursor=identifier(data.get("cursor",0),0)
            if not isinstance(data.get("query",""),str):raise web.HTTPBadRequest(text="Consulta incorrecta.")
            token=lambda u,i,k:downloads.temporary_link(self,u,i,k)
            if data.get("collection") is not None:
                return await personal.items(self,uid,data.get("collection"),
                    data.get("query",""),cursor,data.get("kind","all"),token)
            folder=identifier(data.get("folder"))
            return await self.list_items(uid,folder,data.get("query",""),cursor,
                                         token,data.get("kind","all"))
        if op=="locate-media":return await location.locate(self,uid,data)
        if op=="quota-plus-checkout":return await download_plus.checkout(self,uid)
        if op=="reserve":return await downloads.reserve(self,uid,data.get("ids"))
        raise web.HTTPNotFound()

def mount(app,service):
    g=Gallery(service)
    async def page(request):
        content=(ROOT/"index.html").read_text(encoding="utf-8")
        return web.Response(text=content,content_type="text/html",headers=HEADERS)
    async def static(request):
        name=request.match_info["name"]
        if name not in ("app.js","style.css","admin_tools.js"):raise web.HTTPNotFound()
        return web.FileResponse(ROOT/name,headers={
            "Cache-Control":"no-cache","X-Content-Type-Options":"nosniff"})
    async def api(request):
        try:
            result=await g.api(request)
            response=web.json_response(result,headers={"Cache-Control":"no-store"})
            if request.get("wg_admin_renew"):
                admin_browser.set_cookie(response,request["wg_admin_renew"])
            return response
        except web.HTTPException:raise
        except Exception as exc:
            log.error("Gallery API request failed (%s)",type(exc).__name__)
            raise web.HTTPServiceUnavailable(text="Operación no disponible. Inténtalo después.") from None
    async def login(request):
        g.db()
        return await admin_browser.login(g,request)
    async def logout(request):
        g.db()
        return await admin_browser.logout(g,request)
    async def manifest(request):
        return web.json_response({
            "name":"Web Galerías · Administración",
            "short_name":"Web Galerías",
            "id":"/web-galerias/admin",
            "start_url":"/web-galerias/admin",
            "scope":"/web-galerias/",
            "display":"standalone",
            "background_color":"#0b0d12",
            "theme_color":"#0b0d12",
            "icons":[
                {"src":"/web-galerias/icon/192.png","sizes":"192x192","type":"image/png"},
                {"src":"/web-galerias/icon/512.png","sizes":"512x512","type":"image/png"}]
        },content_type="application/manifest+json",
          headers={"Cache-Control":"public, max-age=600"})
    async def icon(request):
        size=int(request.match_info["size"])
        if size not in (180,192,512):raise web.HTTPNotFound()
        return web.Response(body=app_icon(size),content_type="image/png",
          headers={"Cache-Control":"public, max-age=86400",
                   "X-Content-Type-Options":"nosniff"})
    async def asset(request):
        return await admin.asset(g,request)
    async def public_promo(request):
        return await intro_video.public_promo_status(g,request)
    async def intro_video_upload(request):
        try:
            result=await intro_video.upload(g,request)
            return web.json_response(result,headers={"Cache-Control":"no-store"})
        except web.HTTPException:raise
        except Exception as exc:
            log.error("Intro video upload failed (%s)",type(exc).__name__)
            raise web.HTTPServiceUnavailable(
                text="No se pudo procesar el video. El anterior permanece intacto.") from None
    async def media(request):
        return await downloads.media(g,request)
    async def cover(request):
        return await covers.serve(g,request)
    async def personal_cover(request):
        return await personal.serve_cover(g,request)
    async def download(request):
        return await downloads.download(g,request)
    async def save_file(request):
        return await downloads.save_file(g,request)
    async def stripe(request):
        return await payments.webhook(g,request)
    app.router.add_get("/web-galerias",page)
    app.router.add_get("/web-galerias/",page)
    app.router.add_get("/web-galerias/admin",page)
    app.router.add_get("/web-galerias/admin/",page)
    app.router.add_post("/web-galerias/admin/login",login)
    app.router.add_post("/web-galerias/admin/logout",logout)
    app.router.add_get("/web-galerias/manifest.webmanifest",manifest)
    app.router.add_get("/web-galerias/icon/{size:\\d+}.png",icon)
    app.router.add_get("/web-galerias/static/{name}",static)
    app.router.add_get("/web-galerias/assets/{key}",asset)
    app.router.add_get("/web-galerias/promo.json",public_promo)
    app.router.add_post("/web-galerias/admin/intro-video",intro_video_upload)
    app.router.add_post("/web-galerias/api",api)
    app.router.add_get("/web-galerias/media/{item}/{kind:thumb|view}",media)
    app.router.add_get("/web-galerias/cover/{folder}",cover)
    app.router.add_get("/web-galerias/personal-cover/{folder}",personal_cover)
    app.router.add_get("/web-galerias/download/{ticket}",download)
    app.router.add_get("/web-galerias/file/{item}",save_file)
    app.router.add_post("/web-galerias/stripe",stripe)
    async def startup(_):
        g.task=asyncio.create_task(g.setup())
    async def cleanup(_):
        g.closed=True
        tasks=(g.sync_task,g.intro_poster_task,g.customer_bridge_task,g.moderation_task,g.task)
        for task in tasks:
            if task:task.cancel()
        await asyncio.gather(*(t for t in tasks if t),return_exceptions=True)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return g
