"""Carpetas personales de la Mini App: referencias, nunca mover originales R2."""
import time
from aiohttp import web

PERSONAL_SCHEMA="""
CREATE TABLE IF NOT EXISTS wg_personal_folders(
 id BIGSERIAL PRIMARY KEY,
 uid BIGINT NOT NULL,
 name TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS wg_personal_folders_owner ON wg_personal_folders(uid,created DESC);
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
    rows=await g.db().fetch("""SELECT p.id,p.name,
      COUNT(m.id) FILTER(WHERE m.state IN ('preview','ready') AND f.active
        AND (COALESCE(prefs.mode,'all')='all' OR EXISTS(
          SELECT 1 FROM wg_user_folders u WHERE u.uid=$1 AND u.folder=m.folder))
      )::int AS count
      FROM wg_personal_folders p
      LEFT JOIN wg_personal_items i ON i.folder=p.id AND i.uid=$1
      LEFT JOIN wg_media m ON m.id=i.media
      LEFT JOIN wg_folders f ON f.id=m.folder
      LEFT JOIN wg_user_prefs prefs ON prefs.uid=$1
      WHERE p.uid=$1
      GROUP BY p.id ORDER BY p.created DESC,p.id DESC""",uid)
    return [dict(row) for row in rows]

async def operation(g,uid,op,data):
    from .core import identifier
    db=g.db()
    if op=="personal-create":
        name=folder_name(data.get("name"))
        async with db.acquire() as c,c.transaction():
            count=await c.fetchval("SELECT count(*) FROM wg_personal_folders WHERE uid=$1",uid)
            if count>=100:raise web.HTTPForbidden(text="Máximo 100 carpetas personales.")
            folder=await c.fetchval("""INSERT INTO wg_personal_folders(uid,name,created)
              VALUES($1,$2,$3) RETURNING id""",uid,name,time.time())
        return {"ok":True,"id":folder,"name":name}
    folder=identifier(data.get("personal_folder"))
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
            return {"ok":True}
        from .permissions import is_admin
        valid=await c.fetch("""SELECT m.id FROM wg_media m
          JOIN wg_folders f ON f.id=m.folder
          LEFT JOIN wg_user_prefs p ON p.uid=$2
          WHERE m.id=ANY($1::bigint[])
           AND m.state IN ('preview','ready') AND f.active
           AND ($3 OR COALESCE(p.mode,'all')='all' OR EXISTS(
             SELECT 1 FROM wg_user_folders u WHERE u.uid=$2 AND u.folder=m.folder))
          """,ids,uid,is_admin(uid))
        if len(valid)!=len(ids):
            raise web.HTTPForbidden(text="Uno o más archivos no están autorizados.")
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
