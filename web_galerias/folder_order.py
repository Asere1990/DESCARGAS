"""Durable, isolated folder ordering for the gallery and private folders.

Users keep their own sequence; the administrator's sequence (uid=0) is the
default for anyone without a personal arrangement. No media is moved or deleted.
"""
from aiohttp import web

SCHEMA="""
CREATE TABLE IF NOT EXISTS wg_folder_order (
 uid BIGINT NOT NULL,
 scope TEXT NOT NULL CHECK(scope IN ('public','personal')),
 folder BIGINT NOT NULL,
 position INTEGER NOT NULL,
 PRIMARY KEY(uid,scope,folder)
);
CREATE INDEX IF NOT EXISTS wg_folder_order_read
 ON wg_folder_order(scope,uid,position);
ALTER TABLE wg_folder_order ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON wg_folder_order FROM PUBLIC;
"""


async def ordered(g,uid,scope,items):
    """Sort a bounded folder list using private preference then global default."""
    if not items:
        return items
    owners=[uid] if scope=="personal" or uid==0 else [uid,0]
    rows=await g.db().fetch("""
      SELECT uid,folder,position FROM wg_folder_order
      WHERE scope=$1 AND uid=ANY($2::bigint[])
    """,scope,owners)
    ranks={(int(r["uid"]),int(r["folder"])):int(r["position"]) for r in rows}
    def key(item):
        fid=int(item["id"])
        if (uid,fid) in ranks:
            return (0,ranks[(uid,fid)],-fid)
        if scope=="public" and (0,fid) in ranks:
            return (1,ranks[(0,fid)],-fid)
        return (2,0,-fid)
    return sorted(items,key=key)


async def save(g,uid,scope,ids,global_order=False):
    """Replace the exact visible sequence, never alter other users' settings."""
    from .core import identifier
    from .permissions import is_admin
    if scope not in ("public","personal") or global_order and (
            scope!="public" or not is_admin(uid)):
        raise web.HTTPForbidden(text="Orden de carpetas no autorizado.")
    if not isinstance(ids,list) or not 1<=len(ids)<=300:
        raise web.HTTPBadRequest(text="Selecciona una lista de carpetas válida.")
    try:
        numbers=[identifier(value) for value in ids]
    except (ValueError,TypeError):
        raise web.HTTPBadRequest(text="Hay un identificador de carpeta incorrecto.") from None
    if len(numbers)!=len(set(numbers)):
        raise web.HTTPBadRequest(text="No repitas una carpeta en el orden.")
    if global_order:
        rows=await g.db().fetch("""
          SELECT f.id FROM wg_folders f
          WHERE NOT EXISTS(SELECT 1 FROM wg_folder_sources x
                           WHERE x.source_folder=f.id)
        """)
        owner=0
    elif scope=="public":
        rows=await g.folder_list(uid)
        owner=uid
    else:
        rows=await g.db().fetch(
            "SELECT id FROM wg_personal_folders WHERE uid=$1",uid)
        owner=uid
    allowed={int(row["id"]) for row in rows}
    if set(numbers)!=allowed:
        raise web.HTTPConflict(
            text="Las carpetas disponibles cambiaron. Actualiza y vuelve a ordenarlas.")
    async with g.db().acquire() as conn,conn.transaction():
        await conn.execute(
            "DELETE FROM wg_folder_order WHERE uid=$1 AND scope=$2",owner,scope)
        await conn.executemany("""
          INSERT INTO wg_folder_order(uid,scope,folder,position)
          VALUES($1,$2,$3,$4)
        """,[(owner,scope,fid,i) for i,fid in enumerate(numbers)])
    return {"ok":True,"order":numbers}
