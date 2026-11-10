"""Servidor independiente Web Galerías para Render.
Nunca hace polling, deleteWebhook, setMyCommands ni modifica sesiones.
Comparte datos solo mediante credenciales explícitas y tareas de lectura.
"""
import asyncio
import os
from aiohttp import web
from cryptography.fernet import Fernet
from telethon import TelegramClient
from telethon.sessions import StringSession
from descargas_app.pg_store import Repository
from web_galerias.app import mount

class IsolatedGalleryService:
    def __init__(self):
        self.token=os.getenv("GALERIAS_AUTH_BOT_TOKEN","")
        self.url=os.getenv("RENDER_EXTERNAL_URL","https://localhost").rstrip("/")
        self.cipher=Fernet(os.environ["DESCARGAS_SESSION_KEY"].encode()) if os.getenv("DESCARGAS_SESSION_KEY") else None
        self.repo=Repository(os.getenv("DATABASE_URL",""),self.cipher,pool_size=3) if self.cipher else None
        self.api_id=os.getenv("ID","")
        self.api_hash=os.getenv("HASH","")
        self.ready=False

    async def start(self,app):
        if not self.cipher or not self.repo or not self.repo.url or not self.token:
            return  # Fail closed; UI visible but no one can access private data.
        await self.repo.start()
        self.ready=True

    async def close(self,app):
        self.ready=False
        if self.repo:await self.repo.close()

    async def get_session(self,uid):
        row=await self.repo.account(uid)
        return self.cipher.decrypt(bytes(row["session"])).decode() if row and row["session"] else ""

    def client(self,session):
        # Solo acceso autorizado: NO se guarda ni renueva ninguna StringSession.
        if not self.api_id.isdigit() or not self.api_hash:
            raise ValueError("Credenciales de lectura Telegram no configuradas.")
        return TelegramClient(StringSession(session),int(self.api_id),self.api_hash,
            receive_updates=False,flood_sleep_threshold=0,request_retries=2,
            connection_retries=1,raise_last_call_error=True,
            device_model="Web Galerías · lectura autorizada")

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
