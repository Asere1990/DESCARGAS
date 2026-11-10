"""Tablas y reglas de autorización de Web Galerías. No altera datos de DESCARGAS."""
import asyncio
import json
import os
import re
import time
import unicodedata
from datetime import datetime,timezone
from pathlib import Path
from aiohttp import web
from .permissions import is_admin,pilot_allowed
from .storage import PrivateStorage
from .admin_browser import SCHEMA as BROWSER_SCHEMA,authenticate as authenticate_browser

SCHEMA="""
CREATE TABLE IF NOT EXISTS wg_folders(
 id BIGSERIAL PRIMARY KEY,peer BIGINT NOT NULL UNIQUE,name TEXT NOT NULL,
 active BOOLEAN NOT NULL DEFAULT TRUE,created DOUBLE PRECISION NOT NULL,
 scan_cursor BIGINT NOT NULL DEFAULT 0,high_water BIGINT NOT NULL DEFAULT 0,
 complete BOOLEAN NOT NULL DEFAULT FALSE,source_uid BIGINT,
 last_check DOUBLE PRECISION NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
 scan_count BIGINT NOT NULL DEFAULT 0);
-- El nombre original permanece interno; el nombre público jamás se infiere.
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS public_name TEXT;
CREATE TABLE IF NOT EXISTS wg_media(
 id BIGSERIAL PRIMARY KEY,folder BIGINT NOT NULL REFERENCES wg_folders(id),
 message BIGINT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('photo','video')),
 mime TEXT NOT NULL DEFAULT '',filename TEXT NOT NULL DEFAULT '',
 width INTEGER NOT NULL DEFAULT 0,height INTEGER NOT NULL DEFAULT 0,
 size BIGINT NOT NULL DEFAULT 0,date DOUBLE PRECISION NOT NULL DEFAULT 0,
 label TEXT NOT NULL DEFAULT '',search_text TEXT NOT NULL DEFAULT '',
 reply_to BIGINT,grouped TEXT,thumb_key TEXT,file_key TEXT,
 state TEXT NOT NULL DEFAULT 'pending',created DOUBLE PRECISION NOT NULL DEFAULT 0,
 UNIQUE(folder,message));
CREATE INDEX IF NOT EXISTS wg_media_folder ON wg_media(folder,state,date DESC,id DESC);
CREATE INDEX IF NOT EXISTS wg_media_text ON wg_media USING GIN(to_tsvector('simple',search_text));
CREATE TABLE IF NOT EXISTS wg_user_prefs(
 uid BIGINT PRIMARY KEY,mode TEXT NOT NULL DEFAULT 'all' CHECK(mode IN ('all','selected')));
CREATE TABLE IF NOT EXISTS wg_user_folders(
 uid BIGINT NOT NULL,folder BIGINT NOT NULL REFERENCES wg_folders(id),PRIMARY KEY(uid,folder));
CREATE TABLE IF NOT EXISTS wg_users(
 uid BIGINT PRIMARY KEY,first_seen DOUBLE PRECISION NOT NULL,
 last_seen DOUBLE PRECISION NOT NULL,name TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS wg_quota(
 uid BIGINT NOT NULL,day DATE NOT NULL,used INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(uid,day));
CREATE TABLE IF NOT EXISTS wg_download_tickets(
 token TEXT PRIMARY KEY,uid BIGINT NOT NULL,ids BIGINT[] NOT NULL,
 day DATE NOT NULL,expires DOUBLE PRECISION NOT NULL,
 consumed BOOLEAN NOT NULL DEFAULT FALSE);
CREATE TABLE IF NOT EXISTS wg_config(key TEXT PRIMARY KEY,value JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS wg_assets(
 key TEXT PRIMARY KEY,mime TEXT NOT NULL,body BYTEA NOT NULL,updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS wg_upload_parts(
 upload TEXT NOT NULL,uid BIGINT NOT NULL,key TEXT NOT NULL,mime TEXT NOT NULL,
 part INTEGER NOT NULL,total INTEGER NOT NULL,body BYTEA NOT NULL,created DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(upload,part));
CREATE TABLE IF NOT EXISTS wg_subscriptions(
 uid BIGINT PRIMARY KEY,customer TEXT,subscription TEXT UNIQUE,
 state TEXT NOT NULL DEFAULT 'inactive',paid_until DOUBLE PRECISION NOT NULL DEFAULT 0,
 changed DOUBLE PRECISION NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS wg_checkouts(
 nonce TEXT PRIMARY KEY,uid BIGINT NOT NULL,session TEXT UNIQUE,created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS wg_payment_events(id TEXT PRIMARY KEY,created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS wg_activity(
 id BIGSERIAL PRIMARY KEY,actor BIGINT NOT NULL,operation TEXT NOT NULL,at DOUBLE PRECISION NOT NULL);
"""
DEFAULTS={
 "title":"Web Galerías","header_mode":"text","button_label":"ENTRAR A LA GALERÍA",
 "intro_caption":"Explora nuestras galerías privadas","button_color":"#198ee8",
 "accent":"#2196f3","background":"#0b0d12","text_color":"#ffffff",
 "tile_shape":"square","daily_limit":100,"intro_label":"Descubre nuestras galerías"}
ASSET_MIME={
 "logo":{"image/png","image/jpeg","image/webp"},
 "header":{"image/png","image/jpeg","image/webp"},
 "intro":{"video/mp4","video/quicktime","video/webm"}}
MAX_ASSET={"logo":3_000_000,"header":5_000_000,"intro":25_000_000}

def identifier(value,minimum=1):
    try:n=int(value)
    except (ValueError,TypeError,OverflowError):raise web.HTTPBadRequest(text="Identificador incorrecto.")
    if n<minimum or n>=2**63:raise web.HTTPBadRequest(text="Identificador incorrecto.")
    return n

def normal(value):
    normalized=unicodedata.normalize("NFKD",str(value))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).casefold()[:4000]

def utc_day():
    return datetime.now(timezone.utc).date()

class GalleryCore:
    def __init__(self,service):
        self.s=service
        self.storage=PrivateStorage()
        self.ready=False
        self.closed=False
        self.task=None
        self.sync_task=None
        self.last_error=""

    def db(self):
        if not self.ready or not getattr(self.s.repo,"pool",None):
            raise web.HTTPServiceUnavailable(text="Web Galerías se está configurando.")
        return self.s.repo.pool

    async def setup(self):
        import logging
        log=logging.getLogger("web-galerias")
        while not self.closed:
            if getattr(self.s,"ready",False) and getattr(self.s.repo,"pool",None):
                try:
                    await self.s.repo.pool.execute(SCHEMA)
                    await self.s.repo.pool.execute(BROWSER_SCHEMA)
                    self.ready=True
                    log.warning("Gallery schema ready; storage=%s",self.storage.ready)
                    if os.getenv("GALERIAS_SYNC_ENABLED","0")=="1" and self.storage.ready:
                        from .sync import run
                        self.sync_task=asyncio.create_task(run(self))
                    return
                except asyncio.CancelledError:raise
                except Exception as exc:
                    self.last_error=type(exc).__name__
                    log.warning("Gallery schema pending (%s)",self.last_error)
            await asyncio.sleep(5)

    async def auth(self,request):
        from descargas_app.security import validate_init_data
        raw=request.headers.get("X-Telegram-Init-Data","")
        if not raw:
            # Solo sesiones de administrador creadas tras contraseña válida.
            return await authenticate_browser(self,request)
        if len(raw)>12000:raise web.HTTPUnauthorized(text="Datos Telegram inválidos.")
        tokens=[self.s.token]
        # Tokens secundarios se utilizan solo para validar firmas; nunca toman sesiones.
        for token in os.getenv("GALERIAS_BOT_TOKENS","").split(","):
            token=token.strip()
            if re.fullmatch(r"[0-9]{5,15}:[A-Za-z0-9_-]{30,}",token):tokens.append(token)
        for token in tokens:
            if token:
                try:return validate_init_data(raw,token)
                except Exception:pass
        raise web.HTTPUnauthorized(text="Identidad Telegram no verificada.")

    async def entitled(self,uid):
        if pilot_allowed(uid):return True
        if os.getenv("GALERIAS_PUBLIC","0")!="1":return False
        row=await self.db().fetchrow("SELECT state,paid_until FROM wg_subscriptions WHERE uid=$1",uid)
        return bool(row and row["state"] in ("active","trialing") and row["paid_until"]>time.time())

    async def permitted(self,uid,folder):
        if not await self.entitled(uid):return False
        if is_admin(uid):return True
        return bool(await self.db().fetchval("""
        SELECT 1 FROM wg_folders f LEFT JOIN wg_user_prefs p ON p.uid=$2
        WHERE f.id=$1 AND f.active AND
        (COALESCE(p.mode,'all')='all' OR EXISTS(
         SELECT 1 FROM wg_user_folders u WHERE u.uid=$2 AND u.folder=f.id))
        """,folder,uid))

    async def design(self):
        raw=await self.db().fetchval("SELECT value FROM wg_config WHERE key='design'")
        if isinstance(raw,str):raw=json.loads(raw)
        return dict(DEFAULTS,**(raw or {}))

    async def folder_list(self,uid):
        rows=await self.db().fetch("""
        SELECT f.id,
          COALESCE(NULLIF(f.public_name,''),'Galería '||f.id::text) AS name,
          f.complete,f.error,
          COUNT(m.id) FILTER(WHERE m.state IN ('preview','ready') AND m.kind='photo')::int AS photos,
          COUNT(m.id) FILTER(WHERE m.state IN ('preview','ready') AND m.kind='video')::int AS videos,
          COUNT(m.id) FILTER(WHERE m.state IN ('pending','preview'))::int AS pending
        FROM wg_folders f LEFT JOIN wg_media m ON m.folder=f.id
        LEFT JOIN wg_user_prefs p ON p.uid=$1
        WHERE f.active AND ($2 OR COALESCE(p.mode,'all')='all' OR EXISTS(
          SELECT 1 FROM wg_user_folders u WHERE u.uid=$1 AND u.folder=f.id))
        GROUP BY f.id ORDER BY f.id DESC
        """,uid,is_admin(uid))
        return [dict(r) for r in rows]

    async def bootstrap(self,uid,name=""):
        now=time.time()
        await self.db().execute("""INSERT INTO wg_users(uid,first_seen,last_seen,name)
          VALUES($1,$2,$2,$3) ON CONFLICT(uid) DO UPDATE SET
          last_seen=$2,name=excluded.name""",uid,now,str(name)[:100])
        allowed=await self.entitled(uid)
        folders=await self.folder_list(uid) if allowed else []
        if folders and not is_admin(uid):
            # El estado del respaldo y sus errores solo se entregan al administrador.
            for folder in folders:
                for field in ("pending","complete","error"):
                    folder.pop(field,None)
        if folders:
            from .downloads import temporary_link
            for folder in folders:
                cover=await self.db().fetchval("""SELECT id FROM wg_media
                  WHERE folder=$1 AND state IN ('preview','ready') AND thumb_key IS NOT NULL
                  ORDER BY date DESC LIMIT 1""",folder["id"])
                folder["cover"]=("/web-galerias/media/"+str(cover)+"/thumb?t="+
                  temporary_link(self,uid,cover,"thumb")) if cover else None
        used=await self.db().fetchval("SELECT used FROM wg_quota WHERE uid=$1 AND day=$2",uid,utc_day()) or 0
        return {"admin":is_admin(uid),"allowed":allowed,
          "pilot":os.getenv("GALERIAS_PUBLIC","0")!="1","design":await self.design(),
          "folders":folders,"photos":sum(f["photos"] for f in folders),
          "videos":sum(f["videos"] for f in folders),
          "remaining":max(0,100-used),"used":used,
          "storage_ready":self.storage.ready,
          "sync_enabled":os.getenv("GALERIAS_SYNC_ENABLED","0")=="1"}

    async def log(self,uid,operation):
        await self.db().execute("INSERT INTO wg_activity(actor,operation,at) VALUES($1,$2,$3)",
                                uid,operation,time.time())

    async def list_items(self,uid,folder,query="",cursor=0,token=None):
        if not await self.permitted(uid,folder):raise web.HTTPForbidden(text="Carpeta no autorizada.")
        query=normal(query.strip())[:100]
        if query:
            rows=await self.db().fetch("""SELECT id,kind,mime,filename,date,width,height,size,state,thumb_key
              FROM wg_media WHERE folder=$1 AND state IN ('preview','ready')
              AND to_tsvector('simple',search_text) @@ plainto_tsquery('simple',$2)
              AND id<$3 ORDER BY id DESC LIMIT 61""",folder,query,cursor or 2**63-1)
        else:
            rows=await self.db().fetch("""SELECT id,kind,mime,filename,date,width,height,size,state,thumb_key
              FROM wg_media WHERE folder=$1 AND state IN ('preview','ready')
              AND id<$2 ORDER BY id DESC LIMIT 61""",folder,cursor or 2**63-1)
        result=[]
        for x in rows[:60]:
            item=dict(x)
            item["available"]=x["state"]=="ready"
            item["thumb"]=("/web-galerias/media/"+str(x["id"])+"/thumb?t="+
                           token(uid,x["id"],"thumb")) if x["thumb_key"] else None
            item["view"]=("/web-galerias/media/"+str(x["id"])+"/view?t="+
                          token(uid,x["id"],"view")) if item["available"] else None
            item.pop("thumb_key",None)
            item.pop("state",None)
            result.append(item)
        return {"items":result,"next":rows[59]["id"] if len(rows)>60 else None}
