"""Servidor independiente Web Galerías para Render.
Nunca hace polling, deleteWebhook, setMyCommands ni modifica sesiones.
Comparte datos solo mediante credenciales explícitas y tareas de lectura.
"""
import asyncio
import os
import logging
import secrets
from aiohttp import web
from web_galerias.storage import PrivateStorage
from cryptography.fernet import Fernet
import asyncpg
from descargas_app.service import Service
from descargas_app.pg_store import Repository
from web_galerias.datos_source import DatosReader


class GalleryRepository(Repository):
    """Reuse source locks and read methods, but NEVER migrate DESCARGAS tables."""
    async def start(self):
        self.pool=await asyncpg.create_pool(self.url,min_size=1,max_size=self.pool_size,
           command_timeout=20,max_inactive_connection_lifetime=60)
        try:
            # Only a read-only existence check against the original account table.
            async with self.pool.acquire() as conn:
                await conn.fetchval("SELECT count(*) FROM descarga_accounts WHERE FALSE")
        except Exception:
            await self.pool.close()
            self.pool=None
            raise
from web_galerias.app import mount
from web_galerias.public_entry import PublicGalleryEntry

class IsolatedGalleryService:
    def __init__(self):
        self.token=os.getenv("GALERIAS_AUTH_BOT_TOKEN","")
        self.url=os.getenv("RENDER_EXTERNAL_URL","https://localhost").rstrip("/")
        self.descargas_miniapp_url=None
        # Optional disposable bot: only one Render token, never a polling client.
        self.public_entry=PublicGalleryEntry()
        try:
            self.cipher=Fernet(os.environ["DESCARGAS_SESSION_KEY"].encode()) if os.getenv("DESCARGAS_SESSION_KEY") else None
        except (ValueError,TypeError):
            logging.getLogger("web-galerias").error("Invalid session cipher configuration")
            self.cipher=None
        # La sincronización puede usar hasta cuatro cuentas en paralelo:
        # 1 conexión líder + 2 por cuenta + conexiones de la web.
        self.repo=GalleryRepository(os.getenv("DATABASE_URL",""),self.cipher,pool_size=11) if self.cipher else None
        self.api_id=os.getenv("ID","")
        self.api_hash=os.getenv("HASH","")
        self.ready=False
        self.datos=DatosReader()  # Optional, independent, read-only source

    async def start(self,app):
        app["r2_verify_task"]=asyncio.create_task(verify_r2())
        required={
          "DESCARGAS_SESSION_KEY":bool(self.cipher),
          "DATABASE_URL":bool(self.repo and self.repo.url),
        }
        missing=[name for name,ok in required.items() if not ok]
        if missing:
            logging.getLogger("web-galerias").warning(
                "Gallery private setup pending: %s",",".join(missing))
            return
        try:
            await self.repo.start()
            # DESCARGAS keeps this encrypted in its own database. Read it without
            # requesting a second Telegram token, ID or HASH from the operator.
            raw=await self.repo.setting("runtime_config")
            if raw:
                snapshot=self.repo.decode(raw.encode("utf-8"))
                if not isinstance(snapshot,dict):
                    raise ValueError("Invalid encrypted bot runtime configuration")
                original_token=str(snapshot.get("token") or "")
                self.token=self.token or original_token
                self.api_id=self.api_id or str(snapshot.get("api_id") or "")
                self.api_hash=self.api_hash or str(snapshot.get("api_hash") or "")
                # Reuse the authentic DESCARGAS Mini App URL from the encrypted
                # runtime snapshot; no extra query during gallery user login.
                if original_token and self.token==original_token:
                    from urllib.parse import urlsplit
                    candidate=str(snapshot.get("url") or "").strip()
                    try: parsed=urlsplit(candidate)
                    except ValueError: parsed=None
                    if (parsed and parsed.scheme=="https" and parsed.netloc
                            and not parsed.username and not parsed.password
                            and not parsed.query and not parsed.fragment
                            and parsed.path in ("", "/", "/descargas", "/descargas/")):
                        self.descargas_miniapp_url=candidate
        except Exception as exc:
            logging.getLogger("web-galerias").error(
                "Gallery private database/configuration unavailable (%s)",type(exc).__name__)
            return
        if not self.token:
            logging.getLogger("web-galerias").warning(
                "Gallery private setup pending: GALERIAS_AUTH_BOT_TOKEN or encrypted runtime_config")
            return
        self.ready=True
        await self.datos.start()  # Failure must not affect the original DESCARGAS source.
        logging.getLogger("web-galerias").warning(
           "Gallery private database and Telegram signature verification ready")

    async def close(self,app):
        task=app.get("r2_verify_task")
        if task:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        self.ready=False
        await self.datos.close()
        if self.repo:await self.repo.close()

    async def get_session(self,uid):
        row=await self.repo.account(uid)
        return self.cipher.decrypt(bytes(row["session"])).decode() if row and row["session"] else ""

    def client(self,session):
        # Retain the exact client fingerprint/connection policy used by DESCARGAS.
        # No polling, no session mutation, no login or logout.
        if not self.api_id.isdigit() or not self.api_hash:
            raise ValueError("Credenciales de lectura Telegram no configuradas.")
        return Service.client(self,session)



async def verify_r2():
    """Verificación segura y mínima del bucket privado R2; sin Telegram ni sesiones."""
    storage=PrivateStorage()
    log=logging.getLogger("cloudflare-r2")
    if not storage.ready:
        log.warning("R2 check: missing configuration")
        return
    def check():
        key="healthcheck/"+secrets.token_hex(16)+".txt"
        payload=b"web-galerias-r2-healthcheck-v1"
        client=storage._client()
        uploaded=False
        try:
            client.put_object(Bucket=storage.bucket,Key=key,Body=payload,ContentType="text/plain")
            uploaded=True
            response=client.get_object(Bucket=storage.bucket,Key=key)
            try:
                data=response["Body"].read()
            finally:
                response["Body"].close()
            if data != payload:
                raise ValueError("R2 returned different bytes")
        finally:
            if uploaded:
                client.delete_object(Bucket=storage.bucket,Key=key)
    try:
        await asyncio.wait_for(asyncio.to_thread(check),timeout=45)
        log.warning("R2 check: write/read/delete OK")
    except Exception as exc:
        log.warning("R2 check: failed (%s)",type(exc).__name__)

service=IsolatedGalleryService()
app=web.Application(client_max_size=512*1024)
app.on_startup.append(service.start)
mount(app,service)
app.on_cleanup.append(service.close)

async def health(request):
    return web.json_response({"ready":service.ready},status=200 if service.ready else 503)
app.router.add_get("/readyz",health)

if __name__=="__main__":
    web.run_app(app,host="0.0.0.0",port=int(os.getenv("PORT","10000")),access_log=None)
