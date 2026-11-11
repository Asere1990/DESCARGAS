"""Carpetas personales de la Mini App: referencias, nunca mover originales R2."""
import asyncio
import base64
import binascii
import time
from aiohttp import web
from .covers import optimized_cover

PERSONAL_SCHEMA="""
CREATE TABLE IF NOT EXISTS wg_personal_folders(
 id BIGSERIAL PRIMARY KEY,
 uid BIGINT NOT NULL,
 name TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS wg_personal_folders_owner ON wg_personal_folders(uid,created DESC);
ALTER TABLE wg_personal_folders ADD COLUMN IF NOT EXISTS cover_media BIGINT;
CREATE TABLE IF NOT EXISTS wg_personal_covers(
 folder BIGINT PRIMARY KEY REFERENCES wg_personal_folders(id) ON DELETE CASCADE,
 uid BIGINT NOT NULL,mime TEXT NOT NULL DEFAULT 'image/jpeg',
 body BYTEA NOT NULL,updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS wg_personal_items(
 uid BIGINT NOT NULL,
 folder BIGINT NOT NULL REFERENCES wg_personal_folders(id) ON DELETE CASCADE,
 media BIGINT NOT NULL REFERENCES wg_media(id) ON DELETE CASCADE,
 added DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(folder,media));
CREATE INDEX IF NOT EXISTS wg_personal_items_owner ON wg_personal_items(uid,folder);
"""

def folder_name(value):
    if not isinstance(value,str):
        raise web.HTTPBadRequest(text="Nombre de carpeta inválido.")
    name=" ".join(value.strip().split())
    if not 1<=len(name)<=60 or any(ord(c)<32 for c in name):
        raise web.HTTPBadRequest(text="El nombre debe tener entre 1 y 60 caracteres.")
    return name

def media_filter(value):
    if value not in ("all","photo","video"):
        raise web.HTTPBadRequest(text="Filtro no válido.")
    return value

async def folders(g,uid):
    """Personal folders, with a default thumbnail from their first photo.

    A manually chosen photo takes precedence, followed by the first photo and
    (if the folder has no photos) by the first video thumbnail. Authorization
    for the source gallery is checked when generating and serving its link.
    """
    from .permissions import is_admin
    from .downloads import temporary_link
    rows=await g.db().fetch("""SELECT p.id,p.name,
      COUNT(m.id) FILTER(WHERE m.state IN ('preview','ready') AND f.active
        AND ($2 OR COALESCE(prefs.mode,'all')='all' OR EXISTS(
          SELECT 1 FROM wg_user_folders u WHERE u.uid=$1 AND u.folder=m.folder))
      )::int AS count,
      EXISTS(SELECT 1 FROM wg_personal_covers c
        WHERE c.folder=p.id AND c.uid=$1) AS has_upload,
      p.cover_media,
      (SELECT m2.id FROM wg_personal_items i2
         JOIN wg_media m2 ON m2.id=i2.media
         JOIN wg_folders f2 ON f2.id=m2.folder
         LEFT JOIN wg_user_prefs pref2 ON pref2.uid=$1
         WHERE i2.folder=p.id AND i2.uid=$1 AND f2.active
           AND m2.state IN ('preview','ready') AND m2.thumb_key IS NOT NULL
           AND ($2 OR COALESCE(pref2.mode,'all')='all' OR EXISTS(
             SELECT 1 FROM wg_user_folders u2 WHERE u2.uid=$1
               AND u2.folder=m2.folder))
         ORDER BY CASE WHEN p.cover_media=m2.id THEN 0
                       WHEN m2.kind='photo' THEN 1 ELSE 2 END,
                  i2.added ASC,m2.id ASC LIMIT 1) AS cover_item
      FROM wg_personal_folders p
      LEFT JOIN wg_personal_items i ON i.folder=p.id AND i.uid=$1
      LEFT JOIN wg_media m ON m.id=i.media
      LEFT JOIN wg_folders f ON f.id=m.folder
      LEFT JOIN wg_user_prefs prefs ON prefs.uid=$1
      WHERE p.uid=$1
      GROUP BY p.id ORDER BY p.created DESC,p.id DESC""",uid,is_admin(uid))
    result=[]
    for row in rows:
        item=dict(row)
        media=item.pop("cover_item",None)
        uploaded=bool(item.pop("has_upload",False))
        item["has_custom_cover"]=uploaded or bool(item.get("cover_media"))
        item.pop("cover_media",None)
        if uploaded:
            item["cover"]=("/web-galerias/personal-cover/"+str(item["id"])+"?t="+
                           temporary_link(g,uid,item["id"],"personal-cover"))
        elif media is not None:
            item["cover"]=("/web-galerias/media/"+str(media)+"/thumb?t="+
                           temporary_link(g,uid,media,"thumb"))
        else:
            item["cover"]=None
        result.append(item)
    return result


async def allowed_personal_media(connection,uid,ids):
    """Only files visible to this user may enter their personal folders."""
    from .permissions import is_admin
    rows=await connection.fetch("""SELECT m.id FROM wg_media m
       JOIN wg_folders f ON f.id=m.folder
       LEFT JOIN wg_user_prefs p ON p.uid=$2
       WHERE m.id=ANY($1::bigint[])
        AND m.state IN ('preview','ready') AND f.active
        AND ($3 OR COALESCE(p.mode,'all')='all' OR EXISTS(
          SELECT 1 FROM wg_user_folders u WHERE u.uid=$2 AND u.folder=m.folder))
       """,ids,uid,is_admin(uid))
    if len(rows)!=len(ids):
        raise web.HTTPForbidden(text="Uno o más archivos no están autorizados.")
    return rows


async def operation(g,uid,op,data):
    from .core import identifier
    db=g.db()
    if op=="personal-create":
        name=folder_name(data.get("name"))
        ids=data.get("ids",[])
        if not isinstance(ids,list) or len(ids)>10:
            raise web.HTTPBadRequest(text="Puedes agregar hasta 10 archivos por carpeta.")
        ids=list(dict.fromkeys(identifier(x) for x in ids))
        async with db.acquire() as c,c.transaction():
            count=await c.fetchval("SELECT count(*) FROM wg_personal_folders WHERE uid=$1",uid)
            if count>=100:raise web.HTTPForbidden(text="Máximo 100 carpetas personales.")
            valid=await allowed_personal_media(c,uid,ids) if ids else []
            folder=await c.fetchval("""INSERT INTO wg_personal_folders(uid,name,created)
              VALUES($1,$2,$3) RETURNING id""",uid,name,time.time())
            if valid:
                await c.executemany("""INSERT INTO wg_personal_items(uid,folder,media,added)
                  VALUES($1,$2,$3,$4) ON CONFLICT(folder,media) DO NOTHING""",
                  [(uid,folder,item["id"],time.time()) for item in valid])
        return {"ok":True,"id":folder,"name":name,"count":len(ids)}
    if op=="personal-move":
        # Move only personal-folder references, never the gallery original.
        source=identifier(data.get("personal_folder"))
        destination=identifier(data.get("target_folder"))
        ids=data.get("ids")
        if not isinstance(ids,list) or not 1<=len(ids)<=10:
            raise web.HTTPBadRequest(text="Selecciona de 1 a 10 archivos.")
        ids=list(dict.fromkeys(identifier(x) for x in ids))
        if source==destination:
            return {"ok":True,"count":len(ids)}
        async with db.acquire() as c,c.transaction():
            owned=await c.fetch("""
                SELECT id FROM wg_personal_folders WHERE uid=$1
                  AND id=ANY($2::bigint[]) ORDER BY id FOR UPDATE""",
                uid,[source,destination])
            if len(owned)!=2:
                raise web.HTTPForbidden(text="Carpeta personal no autorizada.")
            rows=await c.fetch("""
                SELECT i.media FROM wg_personal_items i
                JOIN wg_media m ON m.id=i.media
                WHERE i.uid=$1 AND i.folder=$2
                  AND i.media=ANY($3::bigint[])
                  AND m.state IN ('preview','ready')""",
                uid,source,ids)
            if len(rows)!=len(ids):
                raise web.HTTPConflict(text="La selección cambió. Actualiza tu carpeta.")
            await allowed_personal_media(c,uid,ids)
            await c.executemany("""
                INSERT INTO wg_personal_items(uid,folder,media,added)
                VALUES($1,$2,$3,$4) ON CONFLICT(folder,media) DO NOTHING""",
                [(uid,destination,media["media"],time.time()) for media in rows])
            await c.execute("""
                DELETE FROM wg_personal_items WHERE uid=$1 AND folder=$2
                  AND media=ANY($3::bigint[])""",uid,source,ids)
            await c.execute("""UPDATE wg_personal_folders SET cover_media=NULL
                WHERE uid=$1 AND id=$2 AND cover_media=ANY($3::bigint[])""",
                uid,source,ids)
        return {"ok":True,"count":len(ids)}
    folder=identifier(data.get("personal_folder"))
    if op=="personal-cover-upload":
        # Encoded image is resized on the phone and limited to 350 KB of JSON.
        # A single secure request avoids a large persistent multipart queue.
        raw=data.get("image")
        if not isinstance(raw,str) or not 4<=len(raw)<=350000:
            raise web.HTTPBadRequest(text="La portada debe ser una imagen ligera.")
        owner=await db.fetchval(
            "SELECT 1 FROM wg_personal_folders WHERE id=$1 AND uid=$2",folder,uid)
        if not owner:raise web.HTTPForbidden(text="Carpeta no autorizada.")
        try:
            binary=base64.b64decode(raw,validate=True)
        except (binascii.Error,ValueError):
            raise web.HTTPBadRequest(text="Imagen de portada inválida.") from None
        image=await asyncio.to_thread(optimized_cover,binary,"image/jpeg")
        async with db.acquire() as c,c.transaction():
            owner=await c.fetchval(
                "SELECT 1 FROM wg_personal_folders WHERE id=$1 AND uid=$2 FOR UPDATE",
                folder,uid)
            if not owner:raise web.HTTPForbidden(text="Carpeta no autorizada.")
            await c.execute("""INSERT INTO wg_personal_covers(folder,uid,mime,body,updated)
              VALUES($1,$2,'image/jpeg',$3,$4)
              ON CONFLICT(folder) DO UPDATE SET uid=excluded.uid,
                 mime=excluded.mime,body=excluded.body,updated=excluded.updated""",
              folder,uid,image,time.time())
            await c.execute("UPDATE wg_personal_folders SET cover_media=NULL WHERE id=$1 AND uid=$2",
                            folder,uid)
        return {"ok":True}
    if op=="personal-set-cover":
        media=identifier(data.get("media"))
        from .permissions import is_admin
        async with db.acquire() as c,c.transaction():
            allowed=await c.fetchval("""SELECT 1 FROM wg_personal_items i
              JOIN wg_personal_folders p ON p.id=i.folder AND p.uid=$2
              JOIN wg_media m ON m.id=i.media
              JOIN wg_folders f ON f.id=m.folder
              LEFT JOIN wg_user_prefs prefs ON prefs.uid=$2
              WHERE i.folder=$1 AND i.uid=$2 AND i.media=$3
                AND m.kind='photo' AND m.state IN ('preview','ready')
                AND m.thumb_key IS NOT NULL AND f.active
                AND ($4 OR COALESCE(prefs.mode,'all')='all' OR EXISTS(
                  SELECT 1 FROM wg_user_folders u WHERE u.uid=$2 AND u.folder=m.folder))
              """,folder,uid,media,is_admin(uid))
            if not allowed:raise web.HTTPForbidden(text="Selecciona una foto de tu carpeta.")
            await c.execute("""UPDATE wg_personal_folders SET cover_media=$3
              WHERE id=$1 AND uid=$2""",folder,uid,media)
            await c.execute("DELETE FROM wg_personal_covers WHERE folder=$1 AND uid=$2",folder,uid)
        return {"ok":True}
    if op=="personal-cover-reset":
        async with db.acquire() as c,c.transaction():
            status=await c.execute("""UPDATE wg_personal_folders SET cover_media=NULL
              WHERE id=$1 AND uid=$2""",folder,uid)
            if status!="UPDATE 1":raise web.HTTPForbidden(text="Carpeta no autorizada.")
            await c.execute("DELETE FROM wg_personal_covers WHERE folder=$1 AND uid=$2",folder,uid)
        return {"ok":True}
    if op=="personal-rename":
        name=folder_name(data.get("name"))
        result=await db.execute("""UPDATE wg_personal_folders SET name=$3
          WHERE id=$1 AND uid=$2""",folder,uid,name)
        if result!="UPDATE 1":raise web.HTTPForbidden(text="Carpeta no autorizada.")
        return {"ok":True,"name":name}
    if op=="personal-delete":
        result=await db.execute("""DELETE FROM wg_personal_folders WHERE id=$1 AND uid=$2""",folder,uid)
        if result!="DELETE 1":raise web.HTTPForbidden(text="Carpeta no autorizada.")
        return {"ok":True}
    if op not in ("personal-add","personal-remove"):
        raise web.HTTPNotFound()
    ids=data.get("ids")
    if not isinstance(ids,list) or not 1<=len(ids)<=10:
        raise web.HTTPBadRequest(text="Selecciona entre 1 y 10 archivos.")
    ids=list(dict.fromkeys(identifier(x) for x in ids))
    async with db.acquire() as c,c.transaction():
        owned=await c.fetchval("SELECT 1 FROM wg_personal_folders WHERE id=$1 AND uid=$2 FOR UPDATE",folder,uid)
        if not owned:raise web.HTTPForbidden(text="Carpeta no autorizada.")
        if op=="personal-remove":
            await c.execute("""DELETE FROM wg_personal_items WHERE folder=$1 AND uid=$2
              AND media=ANY($3::bigint[])""",folder,uid,ids)
            await c.execute("""UPDATE wg_personal_folders SET cover_media=NULL
              WHERE id=$1 AND uid=$2 AND cover_media=ANY($3::bigint[])""",folder,uid,ids)
            return {"ok":True}
        valid=await allowed_personal_media(c,uid,ids)
        await c.executemany("""INSERT INTO wg_personal_items(uid,folder,media,added)
          VALUES($1,$2,$3,$4) ON CONFLICT(folder,media) DO NOTHING""",
          [(uid,folder,item["id"],time.time()) for item in valid])
    return {"ok":True,"count":len(ids)}

async def items(g,uid,folder,query,cursor,kind,token):
    from .core import identifier,normal
    from .permissions import is_admin
    folder=identifier(folder)
    kind=media_filter(kind)
    if not isinstance(query,str):raise web.HTTPBadRequest(text="Consulta incorrecta.")
    query=normal(query.strip())[:100]
    allowed=await g.db().fetchval("SELECT 1 FROM wg_personal_folders WHERE id=$1 AND uid=$2",folder,uid)
    if not allowed:raise web.HTTPForbidden(text="Carpeta no autorizada.")
    rows=await g.db().fetch("""SELECT m.id,m.kind,m.mime,m.filename,m.date,
       m.width,m.height,m.size,m.state,m.thumb_key
       FROM wg_personal_items i
       JOIN wg_media m ON m.id=i.media
       JOIN wg_folders f ON f.id=m.folder
       LEFT JOIN wg_user_prefs p ON p.uid=$1
       WHERE i.uid=$1 AND i.folder=$2 AND f.active
        AND m.state IN ('preview','ready')
        AND ($3::text='all' OR m.kind=$3)
        AND ($4::text='' OR to_tsvector('simple',m.search_text)
            @@ plainto_tsquery('simple',$4))
        AND m.id<$5
        AND ($6 OR COALESCE(p.mode,'all')='all' OR EXISTS(
          SELECT 1 FROM wg_user_folders u WHERE u.uid=$1 AND u.folder=m.folder))
       ORDER BY m.id DESC LIMIT 61
       """,uid,folder,kind,query,cursor or 2**63-1,is_admin(uid))
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


async def serve_cover(g,request):
    """Signed, owner-only image of a personal folder; no public R2 bucket."""
    from .core import identifier
    from .downloads import validate_link
    token=validate_link(g,request.query.get("t"),"personal-cover")
    uid=identifier(token["uid"])
    folder=identifier(request.match_info["folder"])
    if folder!=token["item"] or not await g.entitled(uid):
        raise web.HTTPForbidden(text="Carpeta no autorizada.")
    row=await g.db().fetchrow("""SELECT c.mime,c.body
      FROM wg_personal_covers c
      JOIN wg_personal_folders p ON p.id=c.folder AND p.uid=$2
      WHERE c.folder=$1 AND c.uid=$2""",folder,uid)
    if not row:raise web.HTTPNotFound(text="Portada no encontrada.")
    return web.Response(body=bytes(row["body"]),content_type=row["mime"],
      headers={"Cache-Control":"private, max-age=120",
               "X-Content-Type-Options":"nosniff",
               "Referrer-Policy":"no-referrer"})
