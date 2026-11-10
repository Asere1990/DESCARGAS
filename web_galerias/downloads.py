"""Vistas con enlaces temporales y descarga diaria atómica de 100 archivos."""
import asyncio
import json
import re
import secrets
import tempfile
import time
import zipfile
from aiohttp import web
from .core import identifier,utc_day
from .storage import NotConfigured

def temporary_link(g,uid,item,kind):
    payload={"uid":uid,"item":item,"kind":kind,"until":int(time.time()+300)}
    return g.s.cipher.encrypt(json.dumps(payload,separators=(",",":")).encode()).decode()

def validate_link(g,value,kind):
    if not isinstance(value,str) or len(value)>3000:raise web.HTTPForbidden()
    try:
        p=json.loads(g.s.cipher.decrypt(value.encode()))
        if p["kind"]!=kind or p["until"]<time.time():raise ValueError()
        return p
    except Exception:raise web.HTTPForbidden(text="La dirección ha vencido.") from None

async def media(g,request):
    kind=request.match_info["kind"]
    p=validate_link(g,request.query.get("t"),kind)
    uid=identifier(p["uid"]);item=identifier(request.match_info["item"])
    if item!=p["item"]:raise web.HTTPForbidden()
    row=await g.db().fetchrow("""SELECT folder,thumb_key,file_key
       FROM wg_media WHERE id=$1 AND
        (state='ready' OR ($2='thumb' AND state='preview'))""",item,kind)
    if not row or not await g.permitted(uid,row["folder"]):raise web.HTTPForbidden()
    key=row["thumb_key"] if kind=="thumb" else row["file_key"]
    if not key:raise web.HTTPNotFound()
    try:raise web.HTTPFound(g.storage.signed_get(key,duration=180))
    except NotConfigured:raise web.HTTPServiceUnavailable(text="Almacenamiento no configurado.")

async def reserve(g,uid,ids):
    if not await g.entitled(uid):raise web.HTTPForbidden(text="Acceso no vigente.")
    if not g.storage.ready:raise web.HTTPServiceUnavailable(text="El respaldo aún no está disponible.")
    if not isinstance(ids,list) or not 1<=len(ids)<=10:
        raise web.HTTPBadRequest(text="Selecciona de 1 a 10 archivos.")
    ids=list(dict.fromkeys(identifier(x) for x in ids))
    db=g.db();day=utc_day()
    async with db.acquire() as c,c.transaction():
        rows=await c.fetch("""SELECT m.id,m.size,m.folder FROM wg_media m
           JOIN wg_folders f ON f.id=m.folder
           LEFT JOIN wg_user_prefs p ON p.uid=$2
           WHERE m.id=ANY($1::bigint[]) AND m.state='ready' AND f.active AND
           ($3 OR COALESCE(p.mode,'all')='all'
            OR EXISTS(SELECT 1 FROM wg_user_folders u WHERE u.uid=$2 AND u.folder=m.folder))
           """,ids,uid,__import__("web_galerias.permissions",fromlist=["is_admin"]).is_admin(uid))
        if len(rows)!=len(ids):raise web.HTTPForbidden(text="Uno o más archivos no están autorizados.")
        if sum(max(0,x["size"]) for x in rows)>500*1024*1024:
            raise web.HTTPBadRequest(text="El lote supera 500 MB; selecciona menos videos.")
        await c.execute("""INSERT INTO wg_quota(uid,day,used) VALUES($1,$2,0)
           ON CONFLICT DO NOTHING""",uid,day)
        used=await c.fetchval("SELECT used FROM wg_quota WHERE uid=$1 AND day=$2 FOR UPDATE",uid,day)
        if used+len(ids)>100:
            raise web.HTTPForbidden(text=f"Te quedan {max(0,100-used)} descargas hoy.")
        await c.execute("UPDATE wg_quota SET used=used+$3 WHERE uid=$1 AND day=$2",uid,day,len(ids))
        ticket=secrets.token_urlsafe(30)
        await c.execute("""INSERT INTO wg_download_tickets(token,uid,ids,day,expires)
            VALUES($1,$2,$3,$4,$5)""",ticket,uid,ids,day,time.time()+300)
    return {"url":"/web-galerias/download/"+ticket,"count":len(ids),
            "remaining":100-used-len(ids)}

async def download(g,request):
    token=request.match_info["ticket"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}",token):raise web.HTTPNotFound()
    db=g.db()
    row=await db.fetchrow("""UPDATE wg_download_tickets SET consumed=TRUE
       WHERE token=$1 AND consumed=FALSE AND expires>$2 RETURNING uid,ids,day""",
       token,time.time())
    if not row:raise web.HTTPGone(text="La descarga ha vencido o ya se utilizó.")
    uid=row["uid"];ids=row["ids"]
    data=await db.fetch("""SELECT id,folder,file_key,filename,mime FROM wg_media
          WHERE id=ANY($1::bigint[]) AND state='ready'""",ids)
    if len(data)!=len(ids) or not all(await asyncio.gather(
        *(g.permitted(uid,x["folder"]) for x in data))):
        raise web.HTTPForbidden(text="Contenido no autorizado.")
    if not g.storage.ready:raise web.HTTPServiceUnavailable()
    if len(data)==1:
        item=data[0]
        filename=item["filename"] or ("foto.jpg" if item["mime"].startswith("image") else "video.mp4")
        raise web.HTTPFound(g.storage.signed_get(item["file_key"],filename=filename))
    # Comprimir directamente desde R2 a un archivo temporal, nunca cargar todo en RAM.
    def make_zip(destination):
        with zipfile.ZipFile(destination,"w",compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
            for ix,item in enumerate(data,1):
                filename=re.sub(r"[^A-Za-z0-9._-]","_",item["filename"] or "archivo")[:90]
                with archive.open(f"{ix:02d}_{filename}","w",force_zip64=True) as target:
                    obj=g.storage._client().get_object(Bucket=g.storage.bucket,Key=item["file_key"])
                    stream=obj["Body"]
                    try:
                        for block in stream.iter_chunks(chunk_size=256*1024):
                            if block:target.write(block)
                    finally:stream.close()
    with tempfile.TemporaryFile() as tmp:
        await asyncio.to_thread(make_zip,tmp)
        tmp.seek(0)
        response=web.StreamResponse(headers={
            "Content-Type":"application/zip",
            "Content-Disposition":'attachment; filename="web-galerias.zip"',
            "Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})
        await response.prepare(request)
        while True:
            chunk=await asyncio.to_thread(tmp.read,256*1024)
            if not chunk:break
            await response.write(chunk)
        await response.write_eof()
        return response
