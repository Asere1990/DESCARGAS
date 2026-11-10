"""Acceso directo de administradores a Web Galerías, sin Telegram WebApp initData.

Solo los IDs de admins.txt pueden iniciar sesión. La contraseña se configura en
CLOUDFLARE (Render Environment), nunca en GitHub ni en el navegador.
Las cookies guardan un token aleatorio HttpOnly, nunca el password ni el Telegram ID.
"""
import hashlib
import hmac
import os
import re
import secrets
import time
from aiohttp import web
from .permissions import is_admin

COOKIE_NAME="wg_admin_session"
SESSION_SECONDS=180*86400
RENEW_AFTER=24*3600
ATTEMPTS_WINDOW=900
ATTEMPTS_MAX=8

SCHEMA="""
CREATE TABLE IF NOT EXISTS wg_admin_sessions(
 token_hash TEXT PRIMARY KEY,uid BIGINT NOT NULL,
 password_version TEXT NOT NULL,
 created_at DOUBLE PRECISION NOT NULL,
 last_seen DOUBLE PRECISION NOT NULL,
 expires_at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS wg_admin_sessions_expiry ON wg_admin_sessions(expires_at);
CREATE TABLE IF NOT EXISTS wg_admin_login_attempts(
 key TEXT PRIMARY KEY,failures INTEGER NOT NULL,window_start DOUBLE PRECISION NOT NULL);
"""

def configured_password():
    # No hay contraseña predeterminada ni se acepta una contraseña vacía.
    return os.environ.get("GALERIAS_ADMIN_PASSWORD","")

def password_version():
    secret=configured_password()
    pepper=os.environ.get("DESCARGAS_SESSION_KEY","")
    if not secret or not pepper:return ""
    return hmac.new(pepper.encode(),("wg-pwa-password-v1:"+secret).encode(),"sha256").hexdigest()

def cookie_digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()

def origin_and_header(request):
    # Cabecera no enviable desde formularios cross-origin. Sin CORS habilitado.
    # La cookie además es SameSite=Strict.
    if request.headers.get("X-Gallery-Web")!="1":
        raise web.HTTPForbidden(text="Solicitud no autorizada.")
    origin=request.headers.get("Origin")
    if origin and origin != "https://"+request.host:
        raise web.HTTPForbidden(text="Origen no autorizado.")
    if request.headers.get("Sec-Fetch-Site","same-origin") not in ("same-origin","none"):
        raise web.HTTPForbidden(text="Solicitud no autorizada.")

def set_cookie(response,token):
    response.set_cookie(COOKIE_NAME,token,path="/web-galerias",
                        httponly=True,secure=True,samesite="Strict",
                        max_age=SESSION_SECONDS)

def remove_cookie(response):
    response.del_cookie(COOKIE_NAME,path="/web-galerias")

async def authenticate(g,request):
    """Solo el admin; nunca acepta la cookie como permiso para usuarios de prueba."""
    origin_and_header(request)
    value=request.cookies.get(COOKIE_NAME,"")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{35,100}",value):
        raise web.HTTPUnauthorized(text="Inicia sesión como administrador.")
    row=await g.db().fetchrow("""SELECT uid,password_version,last_seen,expires_at
      FROM wg_admin_sessions WHERE token_hash=$1""",cookie_digest(value))
    now=time.time()
    if (not row or row["expires_at"]<=now or not is_admin(row["uid"])
        or not password_version() or not hmac.compare_digest(
            str(row["password_version"]),password_version())):
        raise web.HTTPUnauthorized(text="Inicia sesión como administrador.")
    if row["last_seen"]<now-RENEW_AFTER:
        await g.db().execute("""UPDATE wg_admin_sessions SET last_seen=$2,
          expires_at=$3 WHERE token_hash=$1""",
          cookie_digest(value),now,now+SESSION_SECONDS)
        request["wg_admin_renew"]=value
    return int(row["uid"])

def login_key(request,uid):
    remote=str(request.remote or "unknown")
    return hashlib.sha256((remote+"|"+str(uid)).encode()).hexdigest()

async def login(g,request):
    origin_and_header(request)
    if request.content_length and request.content_length>4096:
        raise web.HTTPRequestEntityTooLarge(max_size=4096,actual_size=request.content_length)
    configured=configured_password()
    if not configured or not password_version():
        raise web.HTTPServiceUnavailable(
            text="Configura GALERIAS_ADMIN_PASSWORD en CLOUDFLARE → Environment.")
    try:
        data=await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Formulario incorrecto.") from None
    if not isinstance(data,dict):raise web.HTTPBadRequest(text="Formulario incorrecto.")
    uid_text=str(data.get("uid","")).strip()
    password=data.get("password")
    if not re.fullmatch(r"[1-9][0-9]{4,17}",uid_text) or not isinstance(password,str) or len(password)>256:
        raise web.HTTPUnauthorized(text="Telegram ID o contraseña incorrectos.")
    uid=int(uid_text)
    key=login_key(request,uid)
    now=time.time()
    row=await g.db().fetchrow(
        "SELECT failures,window_start FROM wg_admin_login_attempts WHERE key=$1",key)
    if row and row["failures"]>=ATTEMPTS_MAX and now-row["window_start"]<ATTEMPTS_WINDOW:
        raise web.HTTPTooManyRequests(text="Demasiados intentos. Vuelve a intentarlo más tarde.")
    correct=hmac.compare_digest(hashlib.sha256(password.encode()).digest(),
                                hashlib.sha256(configured.encode()).digest())
    if not is_admin(uid) or not correct:
        await g.db().execute("""INSERT INTO wg_admin_login_attempts(key,failures,window_start)
          VALUES($1,1,$2) ON CONFLICT(key) DO UPDATE SET
          failures=CASE WHEN wg_admin_login_attempts.window_start<$2-$3
            THEN 1 ELSE wg_admin_login_attempts.failures+1 END,
          window_start=CASE WHEN wg_admin_login_attempts.window_start<$2-$3
            THEN $2 ELSE wg_admin_login_attempts.window_start END""",
          key,now,ATTEMPTS_WINDOW)
        raise web.HTTPUnauthorized(text="Telegram ID o contraseña incorrectos.")
    await g.db().execute("DELETE FROM wg_admin_login_attempts WHERE key=$1",key)
    token=secrets.token_urlsafe(32)
    await g.db().execute("""INSERT INTO wg_admin_sessions
      (token_hash,uid,password_version,created_at,last_seen,expires_at)
      VALUES($1,$2,$3,$4,$4,$5)""",
      cookie_digest(token),uid,password_version(),now,now+SESSION_SECONDS)
    response=web.json_response({"ok":True,"admin":True},headers={"Cache-Control":"no-store"})
    set_cookie(response,token)
    return response

async def logout(g,request):
    # Logout no cambia cuentas ni tokens de Telegram. Solo invalida este dispositivo.
    await authenticate(g,request)
    value=request.cookies.get(COOKIE_NAME,"")
    await g.db().execute("DELETE FROM wg_admin_sessions WHERE token_hash=$1",
                         cookie_digest(value))
    response=web.json_response({"ok":True},headers={"Cache-Control":"no-store"})
    remove_cookie(response)
    return response
