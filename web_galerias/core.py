"""Tablas y reglas de autorización de Web Galerías. No altera datos de DESCARGAS."""
import asyncio
import json
import os
import re
import time
from contextvars import ContextVar
import unicodedata
from datetime import datetime,timezone
from pathlib import Path
from aiohttp import web
from .permissions import is_admin,pilot_allowed,is_tester,payment_test_user,free_access
from .storage import PrivateStorage
from .moderation import SCHEMA as MODERATION_SCHEMA, run_deletions
from .personal import PERSONAL_SCHEMA,folders as personal_folders,media_filter
from .covers import COVER_SCHEMA,temporary_cover
from .admin_browser import SCHEMA as BROWSER_SCHEMA,authenticate as authenticate_browser
from .user_features import SCHEMA as USER_FEATURES_SCHEMA
from .commercial import SCHEMA as COMMERCIAL_SCHEMA
from . import download_plus

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
-- Cuenta Telegram que el administrador seleccionó para ver el historial.
-- No sustituye ni modifica la sesión cifrada de DESCARGAS.
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS preferred_uid BIGINT;
-- Totales publicados por Telegram, independientes de la catalogación y de R2.
-- NULL significa todavía sin consulta satisfactoria; nunca equivale a cero.
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS telegram_photos_total BIGINT;
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS telegram_videos_total BIGINT;
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS telegram_counts_at DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS telegram_counts_next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS telegram_counts_error TEXT NOT NULL DEFAULT '';
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
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT '';
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS source_checks INTEGER NOT NULL DEFAULT 0;
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
CREATE INDEX IF NOT EXISTS wg_download_tickets_user_day
 ON wg_download_tickets(uid,day DESC,expires DESC);
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
ALTER TABLE wg_checkouts ADD COLUMN IF NOT EXISTS subscription TEXT;
-- Billing state only; scheduling cancellation preserves paid access.
ALTER TABLE wg_subscriptions
 ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE wg_subscriptions
 ADD COLUMN IF NOT EXISTS subscription_started_at DOUBLE PRECISION NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS wg_link_subscriptions(
 subscription TEXT PRIMARY KEY,
 customer TEXT NOT NULL DEFAULT '',
 price_id TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'incomplete',
 period_end DOUBLE PRECISION NOT NULL DEFAULT 0,
 paid_until DOUBLE PRECISION NOT NULL DEFAULT 0,
 changed DOUBLE PRECISION NOT NULL DEFAULT 0);
ALTER TABLE wg_link_subscriptions
 ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE wg_link_subscriptions
 ADD COLUMN IF NOT EXISTS subscription_started_at DOUBLE PRECISION NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS wg_payment_events(id TEXT PRIMARY KEY,created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS wg_activity(
 id BIGSERIAL PRIMARY KEY,actor BIGINT NOT NULL,operation TEXT NOT NULL,at DOUBLE PRECISION NOT NULL);
-- Independent customer-service mirror. No changes to DESCARGAS tables.
CREATE TABLE IF NOT EXISTS wg_bridge_snapshots(
 subscription TEXT PRIMARY KEY,last_change DOUBLE PRECISION NOT NULL DEFAULT 0,
 last_checked DOUBLE PRECISION NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS wg_access_blocks(
 uid BIGINT PRIMARY KEY,blocked BOOLEAN NOT NULL DEFAULT FALSE,
 updated DOUBLE PRECISION NOT NULL DEFAULT 0);
-- Reversible independent Web-only free grants; NULL means fall back to acceso.txt.
CREATE TABLE IF NOT EXISTS wg_free_access(
 uid BIGINT PRIMARY KEY,enabled BOOLEAN NOT NULL,
 updated DOUBLE PRECISION NOT NULL DEFAULT 0,
 changed_by BIGINT NOT NULL);
"""
SCHEMA += PERSONAL_SCHEMA
SCHEMA += COVER_SCHEMA
SCHEMA += USER_FEATURES_SCHEMA
SCHEMA += COMMERCIAL_SCHEMA
SCHEMA += download_plus.SCHEMA
from .payment_risk import SCHEMA as PAYMENT_RISK_SCHEMA
SCHEMA += PAYMENT_RISK_SCHEMA
DEFAULTS={
 "title":"Web Galerías","header_mode":"text","button_label":"ENTRAR A LA GALERÍA","button_mode":"text",
 "title_image_height":120,"button_image_height":30,
 "library_brand":"Web Galerías","library_brand_mode":"text","library_brand_image_height":60,
 "library_eyebrow":"MI COLECCIÓN","library_eyebrow_mode":"text","library_eyebrow_image_height":44,
 "library_heading":"Mis galerías","library_heading_mode":"text","library_heading_image_height":100,
 "intro_caption":"Explora nuestras galerías privadas","button_color":"#198ee8",
 "accent":"#2196f3","background":"#0b0d12","text_color":"#ffffff",
 "tile_shape":"square","daily_limit":50,"intro_label":"Descubre nuestras galerías",
 "intro_fit":"cover","intro_media":"video",
 "wm_enabled":True,"wm_line1":"Búscame en Google","wm_line2":"hoo.be/Cubanas",
 "wm_position":"center","wm_opacity":30,"wm_size":20,"wm_color":"#FFFFFF"}
ASSET_MIME={
 "logo":{"image/png","image/jpeg","image/webp"},
 "header":{"image/png","image/jpeg","image/webp"},
 "button-label-image":{"image/png","image/jpeg","image/webp"},
 "library-brand":{"image/png","image/jpeg","image/webp"},
 "library-eyebrow":{"image/png","image/jpeg","image/webp"},
 "library-heading":{"image/png","image/jpeg","image/webp"},
 "intro-photo":{"image/png","image/jpeg","image/webp","image/gif"},
 "intro":{"video/mp4","video/quicktime","video/webm"}}
MAX_ASSET={"logo":3_000_000,"header":5_000_000,
           "button-label-image":3_000_000,
           "library-brand":5_000_000,"library-eyebrow":5_000_000,
           "library-heading":5_000_000,
           "intro-photo":5_000_000,"intro":25_000_000}

# Request-scoped proof from CUSTOMER-SERVICE; never trust the bare Telegram ID.
_CUSTOMER_BOT_UID=ContextVar("wg_customer_bot_verified_uid",default=0)

def customer_bot_signed(uid):
    # Only an actual verified CUSTOMER-SERVICE Mini App signature, never a
    # supplied Telegram ID, can satisfy this request-local identity.
    return _CUSTOMER_BOT_UID.get()==int(uid) and (
        os.getenv("GALERIAS_PUBLIC","0")=="1"
        or is_admin(uid) or is_tester(uid) or payment_test_user(uid)
        or int(uid)==8129084202  # existing signed checkout tester only, never free
    )


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
        self.priority_task=None
        # Set only after this CLOUDFLARE instance owns the priority lock.
        self.priority_leader_ready=asyncio.Event()
        self.intro_poster_task=None
        self.customer_bridge_task=None
        self.moderation_task=None
        self.broadcast_task=None
        self.media_text_task=None
        self.ocr_task=None
        self.video_diagnostic_task=None
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
                    await self.s.repo.pool.execute(MODERATION_SCHEMA)
                    # Search enrichment schema/backfill is entirely detached
                    # from app startup, gallery loading, payments and R2 backups.
                    self.ready=True
                    from . import media_text,ocr_low_priority,video_diagnostic
                    self.video_diagnostic_task=asyncio.create_task(video_diagnostic.run(self))
                    self.media_text_task=asyncio.create_task(media_text.run(self))
                    if os.getenv("GALERIAS_OCR_ENABLED","0")=="1":
                        self.ocr_task=asyncio.create_task(ocr_low_priority.run(self))
                    # Broadcast schema/worker is strictly optional: failure
                    # cannot stop galleries, billing or backup.
                    try:
                        from . import broadcasts
                        await self.s.repo.pool.execute(broadcasts.SCHEMA)
                        self.broadcast_task=asyncio.create_task(broadcasts.run(self))
                    except Exception as exc:
                        log.warning("Notices setup pending (%s)", type(exc).__name__)
                    self.moderation_task=asyncio.create_task(run_deletions(self))
                    log.warning("Gallery schema ready; storage=%s",self.storage.ready)
                    # Best-effort low-priority CUSTOMER-SERVICE mirror; never
                    # participates in the user's gallery loading or R2 sync.
                    from .customer_bridge import ready as bridge_ready,run as bridge_run
                    if bridge_ready():
                        self.customer_bridge_task=asyncio.create_task(bridge_run(self))
                        from . import dupe_guard,payment_risk
                        self.dedupe_task=asyncio.create_task(dupe_guard.run(self))
                        self.risk_task=asyncio.create_task(payment_risk.run(self))
                    # A single independently verified Stripe receipt can
                    # repair a paid tester whose webhook failed before it was
                    # processed. A failure must never stop Web Galerías.
                    if os.getenv("GALERIAS_VERIFIED_PAID_RECOVERY"):
                        from .payment_link_checkout import recover_independently_verified_purchase
                        try:
                            outcome=await recover_independently_verified_purchase(self)
                            log.warning("Paid gallery receipt recovery: %s",outcome)
                        except Exception as exc:
                            log.error("Paid gallery receipt recovery failed (%s)",
                                      type(exc).__name__)
                    # Build a first-frame preview for any existing video that
                    # predates the poster feature, off the user-request path.
                    if self.storage.ready:
                        from .intro_video import backfill_poster
                        self.intro_poster_task=asyncio.create_task(backfill_poster(self))
                    if os.getenv("GALERIAS_SYNC_ENABLED","0")=="1" and self.storage.ready:
                        from .sync import run,EXTRA
                        # All priority writes are isolated to wg_* tables.
                        try:
                            from . import on_demand
                            await self.s.repo.pool.execute(EXTRA)
                            await self.s.repo.pool.execute(on_demand.SCHEMA)
                            self.priority_task=asyncio.create_task(on_demand.run(self))
                        except Exception as exc:
                            log.warning("On-demand video priority inactive (%s)",
                                        type(exc).__name__)
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
        # The disposable public bot signs its own Telegram initData. This
        # adds acceptance only; existing paid access and protected bots remain.
        public_entry=getattr(self.s,"public_entry",None)
        if public_entry:
            public_token=public_entry.authorized_token(
                self.s.token,os.getenv("GALERIAS_BOT_TOKENS",""))
            if public_token:tokens.append(public_token)
        # Tokens secundarios se utilizan solo para validar firmas; nunca toman sesiones.
        for token in os.getenv("GALERIAS_BOT_TOKENS","").split(","):
            token=token.strip()
            if re.fullmatch(r"[0-9]{5,15}:[A-Za-z0-9_-]{30,}",token):tokens.append(token)
        for token in tokens:
            if token:
                try:return validate_init_data(raw,token)
                except Exception:pass
        # Customer-Service uses its own bot token. Never copy its secret
        # into this service: have the trusted server verify initData instead.
        from .customer_bridge import ready as bridge_ready,verify_customer_bot
        if bridge_ready():
            try:
                uid=await verify_customer_bot(raw,self)
                _CUSTOMER_BOT_UID.set(uid)
                return uid
            except Exception:
                pass
        raise web.HTTPUnauthorized(text="Identidad Telegram no verificada.")

    def download_link_bot(self):
        """Optional configured original DESCARGAS bot; no guessed bot identity."""
        import re
        name=(os.getenv("GALERIAS_DESCARGAS_BOT") or
              os.getenv("GALERIAS_RETURN_BOT") or "Descargas_Robot").lstrip("@")
        return "https://t.me/"+name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}",name) else None

    async def free_granted(self,uid):
        # A dashboard grant/revocation overrides legacy acceso.txt without
        # editing the file or restarting Telegram connections.
        override=await self.db().fetchval(
            "SELECT enabled FROM wg_free_access WHERE uid=$1",uid)
        if override is not None:return bool(override)
        return free_access(uid)

    async def entitled(self,uid):
        if is_admin(uid):return True
        if await self.free_granted(uid):
            return not bool(await self.db().fetchval(
                "SELECT blocked FROM wg_access_blocks WHERE uid=$1",uid))
        if (os.getenv("GALERIAS_PUBLIC","0")!="1"
            and not payment_test_user(uid)
            and not customer_bot_signed(uid)):
            return False
        # A public launch never grants free files: only a verified paid Stripe
        # subscription or explicit free authorization passes this check.
        row=await self.db().fetchrow("""SELECT s.state,s.paid_until,s.subscription,
                   COALESCE(b.blocked,FALSE) AS blocked
                   FROM wg_subscriptions s
                   LEFT JOIN wg_access_blocks b ON b.uid=s.uid
                   WHERE s.uid=$1""",uid)
        return bool(row and not row.get("blocked",False)
                and row["state"] in ("active","trialing")
                and row["paid_until"]>time.time()
                and not await self.db().fetchval(
                    "SELECT 1 FROM wg_web_revocations WHERE subscription=$1",
                    row["subscription"])
                and not await self.db().fetchval(
                    "SELECT 1 FROM wg_web_dispute_blocks WHERE uid=$1",uid))

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
          f.complete,f.error,MAX(c.updated) AS cover_updated,
          COUNT(m.id) FILTER(WHERE m.state IN ('preview','ready') AND m.kind='photo')::int AS photos,
          COUNT(m.id) FILTER(WHERE m.state IN ('preview','ready') AND m.kind='video')::int AS videos,
          COUNT(m.id) FILTER(WHERE m.state IN ('pending','preview'))::int AS pending
        FROM wg_folders f LEFT JOIN wg_media m ON m.folder=f.id
        LEFT JOIN wg_folder_covers c ON c.folder=f.id
        LEFT JOIN wg_user_prefs p ON p.uid=$1
        WHERE f.active AND ($2 OR COALESCE(p.mode,'all')='all' OR EXISTS(
          SELECT 1 FROM wg_user_folders u WHERE u.uid=$1 AND u.folder=f.id))
        GROUP BY f.id ORDER BY f.id DESC
        """,uid,is_admin(uid))
        return [dict(r) for r in rows]

    async def bootstrap(self,uid,name="",*,track_visit=True):
        # Use exactly the same authenticated user response for the user's
        # Mini App and for an administrator inspecting this selected account.
        # Admin inspection must not alter the user's name or visitation records.
        if track_visit:
            now=time.time()
            # Record at most one daily visit without an extra database roundtrip.
            # Both writes are one statement, keeping first paint responsive.
            await self.db().execute("""
              WITH registered AS (
               INSERT INTO wg_users(uid,first_seen,last_seen,name)
               VALUES($1,$2,$2,$3)
               ON CONFLICT(uid) DO UPDATE SET
                last_seen=excluded.last_seen,name=excluded.name
               RETURNING uid
              )
              INSERT INTO wg_visit_days(uid,visit_day)
              SELECT uid,$4 FROM registered WHERE TRUE
              ON CONFLICT(uid,visit_day) DO NOTHING
            """,uid,now,str(name)[:100],utc_day())
        allowed=await self.entitled(uid)
        # This is the verified Web subscription only, not any of the user's
        # unrelated Telegram-group memberships.
        billing=None
        if allowed and callable(getattr(self.db(),"fetchrow",None)):
            billing=await self.db().fetchrow(
                "SELECT subscription,state,paid_until,cancel_at_period_end "
                "FROM wg_subscriptions WHERE uid=$1",uid)
        paid=bool(billing and billing["subscription"] and
                  billing["state"] in ("active","trialing") and
                  billing["paid_until"]>time.time())
        folders=await self.folder_list(uid) if allowed else []
        owned_folders=await personal_folders(self,uid) if allowed else []
        if folders and not is_admin(uid):
            # El estado del respaldo y sus errores solo se entregan al administrador.
            for folder in folders:
                for field in ("pending","complete","error"):
                    folder.pop(field,None)
        if folders:
            from .downloads import temporary_link
            for folder in folders:
                if folder.get("cover_updated"):
                    folder["cover"]=temporary_cover(self,uid,folder["id"])
                    folder.pop("cover_updated",None)
                    continue
                folder.pop("cover_updated",None)
                cover=await self.db().fetchval("""SELECT id FROM wg_media
                  WHERE folder=$1 AND state IN ('preview','ready') AND thumb_key IS NOT NULL
                  ORDER BY date DESC LIMIT 1""",folder["id"])
                folder["cover"]=("/web-galerias/media/"+str(cover)+"/thumb?t="+
                  temporary_link(self,uid,cover,"thumb")) if cover else None
        plan=await download_plus.tier(self,uid) if allowed else {
            "daily_limit":download_plus.BASE_LIMIT,"tier":"basic",
            "quota_unlimited":False,"plus_active":False,"linked_descargas":False}
        unlimited=bool(plan["quota_unlimited"])
        used=0 if unlimited else (await self.db().fetchval(
            "SELECT used FROM wg_quota WHERE uid=$1 AND day=$2",uid,utc_day()) or 0)
        from .permissions import payment_test_user
        from .payments import ready as stripe_ready
        checkout_enabled=(not allowed and (payment_test_user(uid)
                         or os.getenv("GALERIAS_PUBLIC","0")=="1"
                         or customer_bot_signed(uid))
                         and not await self.db().fetchval(
                            "SELECT blocked FROM wg_access_blocks WHERE uid=$1",uid))
        return {"admin":is_admin(uid),"allowed":allowed,
          "checkout_enabled":checkout_enabled,
          "checkout_ready":stripe_ready() if checkout_enabled else False,
          "pilot":os.getenv("GALERIAS_PUBLIC","0")!="1","design":await self.design(),
          "folders":folders,"personal_folders":owned_folders,
          "photos":sum(f["photos"] for f in folders),
          "videos":sum(f["videos"] for f in folders),
          "remaining":None if unlimited else max(0,plan["daily_limit"]-used),"used":used,
          "free_access":bool(allowed and await self.free_granted(uid)),
          "quota_unlimited":unlimited,"quota_daily_limit":plan["daily_limit"],
          "quota_tier":plan["tier"],"quota_linked":plan["linked_descargas"],
          "quota_plus_active":plan["plus_active"],
          "quota_plus_available":bool(allowed and download_plus.configured()),
          "subscription_paid":paid,
          "subscription_paid_until":int(billing["paid_until"]) if paid else 0,
          "subscription_cancel_scheduled":bool(paid and billing["cancel_at_period_end"]),
          "quota_plus_cancel_scheduled":bool(plan.get("plus_cancel_scheduled")),
          "quota_link_bot":GalleryCore.download_link_bot(self),
          "quota_link_miniapp":getattr(self.s,"descargas_miniapp_url",None) if allowed else None,
          "storage_ready":self.storage.ready,
          "sync_enabled":os.getenv("GALERIAS_SYNC_ENABLED","0")=="1"}

    async def log(self,uid,operation):
        await self.db().execute("INSERT INTO wg_activity(actor,operation,at) VALUES($1,$2,$3)",
                                uid,operation,time.time())

    async def list_items(self,uid,folder,query="",cursor=0,token=None,kind="all"):
        if not await self.permitted(uid,folder):raise web.HTTPForbidden(text="Carpeta no autorizada.")
        query=normal(query.strip())[:100]
        kind=media_filter(kind)
        if query:
            rows=await self.db().fetch("""SELECT id,folder,kind,mime,filename,date,width,height,size,state,thumb_key,
              EXISTS(SELECT 1 FROM wg_favorites v
                     WHERE v.uid=$5 AND v.media=wg_media.id) AS favorite
              FROM wg_media WHERE folder=$1 AND state IN ('preview','ready')
              AND ($3::text='all' OR kind=$3)
              AND to_tsvector('simple',search_text) @@ plainto_tsquery('simple',$2)
              AND id<$4 ORDER BY id DESC LIMIT 61""",folder,query,kind,cursor or 2**63-1,uid)
        else:
            rows=await self.db().fetch("""SELECT id,folder,kind,mime,filename,date,width,height,size,state,thumb_key,
              EXISTS(SELECT 1 FROM wg_favorites v
                     WHERE v.uid=$4 AND v.media=wg_media.id) AS favorite
              FROM wg_media WHERE folder=$1 AND state IN ('preview','ready')
              AND ($2::text='all' OR kind=$2)
              AND id<$3 ORDER BY id DESC LIMIT 61""",folder,kind,cursor or 2**63-1,uid)
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
