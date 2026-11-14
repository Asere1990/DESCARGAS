"""User-owned favorites and last-viewed positions for Web Galerías.

No media copies. Every read follows the SAME folder visibility policy as
the ordinary gallery, and every write is scoped to the signed Telegram uid.
"""
import time
from aiohttp import web

SCHEMA="""
CREATE TABLE IF NOT EXISTS wg_view_progress(
 uid BIGINT NOT NULL,
 folder BIGINT NOT NULL REFERENCES wg_folders(id) ON DELETE CASCADE,
 media BIGINT NOT NULL REFERENCES wg_media(id) ON DELETE CASCADE,
 updated DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(uid,folder));
CREATE INDEX IF NOT EXISTS wg_view_progress_recent
 ON wg_view_progress(uid,updated DESC);
CREATE TABLE IF NOT EXISTS wg_favorites(
 uid BIGINT NOT NULL,
 media BIGINT NOT NULL REFERENCES wg_media(id) ON DELETE CASCADE,
 created DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(uid,media));
-- These tables are server-managed only. Telegram initData is verified by the
-- Web API, not interpreted as a Supabase database role.
ALTER TABLE wg_view_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE wg_favorites ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON wg_view_progress,wg_favorites FROM PUBLIC;
"""


def _visible_clause():
    # Keep in sync with GalleryCore.folder_list / personal.items. No
    # favorites or progress from hidden, deleted or unauthorized galleries.
    return """f.active AND m.state IN ('preview','ready')
       AND ($2 OR COALESCE(p.mode,'all')='all'
            OR EXISTS(SELECT 1 FROM wg_user_folders uf
                      WHERE uf.uid=$1 AND uf.folder=f.id))"""


async def collections(g,uid):
    from .permissions import is_admin
    db=g.db()
    admin=is_admin(uid)
    rows=await db.fetch("""
        SELECT r.folder,r.media,r.updated,
          COALESCE(NULLIF(f.public_name,''),'Galería '||f.id::text) AS name
        FROM wg_view_progress r
        JOIN wg_media m ON m.id=r.media
        JOIN wg_folders origin ON origin.id=m.folder AND origin.active
        LEFT JOIN wg_folder_sources link ON link.source_folder=m.folder
        JOIN wg_folders f ON f.id=COALESCE(link.parent_folder,m.folder)
                           AND r.folder=f.id
        LEFT JOIN wg_user_prefs p ON p.uid=$1
        WHERE r.uid=$1 AND """+_visible_clause()+"""
        ORDER BY r.updated DESC LIMIT 100
    """,uid,admin)
    count=await db.fetchval("""
        SELECT COUNT(*)::int
        FROM wg_favorites fav
        JOIN wg_media m ON m.id=fav.media
        JOIN wg_folders origin ON origin.id=m.folder AND origin.active
        LEFT JOIN wg_folder_sources link ON link.source_folder=m.folder
        JOIN wg_folders f ON f.id=COALESCE(link.parent_folder,m.folder)
        LEFT JOIN wg_user_prefs p ON p.uid=$1
        WHERE fav.uid=$1 AND """+_visible_clause(),uid,admin)
    return {"resume":[dict(r) for r in rows],
            "favorites_count":int(count or 0)}


async def save_progress(g,uid,data):
    from .core import identifier
    target=identifier(data.get("media"))
    row=await g.db().fetchrow("""
       SELECT COALESCE(s.parent_folder,m.folder) AS folder,
              m.folder AS source_folder
       FROM wg_media m
       LEFT JOIN wg_folder_sources s ON s.source_folder=m.folder
       WHERE m.id=$1 AND m.state IN ('preview','ready')""",target)
    if not row or not await g.permitted(uid,row.get("source_folder",row["folder"])):
        raise web.HTTPForbidden(text="Archivo no disponible en tus galerías.")
    await g.db().execute("""
      INSERT INTO wg_view_progress(uid,folder,media,updated)
      VALUES($1,$2,$3,$4)
      ON CONFLICT(uid,folder) DO UPDATE SET
        media=excluded.media,updated=excluded.updated
    """,uid,row["folder"],target,time.time())
    return {"ok":True,"folder":row["folder"],"media":target}


async def set_favorite(g,uid,data):
    from .core import identifier
    target=identifier(data.get("media"))
    favorite=data.get("favorite")
    if type(favorite) is not bool:
        raise web.HTTPBadRequest(text="Acción de favorito inválida.")
    if not favorite:
        # Ownership is sufficient to remove even if the gallery was later
        # removed from the user's permissions. No other user's row touched.
        await g.db().execute(
            "DELETE FROM wg_favorites WHERE uid=$1 AND media=$2",uid,target)
        return {"ok":True,"favorite":False,"media":target}
    row=await g.db().fetchrow("""
       SELECT COALESCE(s.parent_folder,m.folder) AS folder,
              m.folder AS source_folder
       FROM wg_media m
       LEFT JOIN wg_folder_sources s ON s.source_folder=m.folder
       WHERE m.id=$1 AND m.state IN ('preview','ready')""",target)
    if not row or not await g.permitted(uid,row.get("source_folder",row["folder"])):
        raise web.HTTPForbidden(text="Archivo no disponible en tus galerías.")
    await g.db().execute("""
       INSERT INTO wg_favorites(uid,media,created)
       VALUES($1,$2,$3) ON CONFLICT(uid,media) DO NOTHING""",
       uid,target,time.time())
    return {"ok":True,"favorite":True,"media":target}


async def items(g,uid,data):
    from .core import identifier,normal
    from .personal import media_filter
    from .permissions import is_admin
    from .downloads import temporary_link
    cursor=identifier(data.get("cursor",0),0)
    query=data.get("query","")
    if not isinstance(query,str) or len(query)>100:
        raise web.HTTPBadRequest(text="Búsqueda incorrecta.")
    query=normal(query.strip())[:100]
    kind=media_filter(data.get("kind","all"))
    rows=await g.db().fetch("""
      SELECT m.id,m.folder,m.kind,m.mime,m.filename,m.date,
        m.width,m.height,m.size,m.state,m.thumb_key
      FROM wg_favorites fav
      JOIN wg_media m ON m.id=fav.media
      JOIN wg_folders origin ON origin.id=m.folder AND origin.active
      LEFT JOIN wg_folder_sources link ON link.source_folder=m.folder
      JOIN wg_folders f ON f.id=COALESCE(link.parent_folder,m.folder)
      LEFT JOIN wg_user_prefs p ON p.uid=$1
      WHERE fav.uid=$1 AND """+_visible_clause()+"""
        AND ($3::text='all' OR m.kind=$3)
        AND ($4::text='' OR to_tsvector('simple',m.search_text)
             @@ plainto_tsquery('simple',$4))
        AND m.id<$5
      ORDER BY m.id DESC LIMIT 61
    """,uid,is_admin(uid),kind,query,cursor or 2**63-1)
    result=[]
    for row in rows[:60]:
        x=dict(row)
        x["available"]=row["state"]=="ready"
        x["favorite"]=True
        x["thumb"]=("/web-galerias/media/"+str(row["id"])+"/thumb?t="+
            temporary_link(g,uid,row["id"],"thumb")) if row["thumb_key"] else None
        x["view"]=("/web-galerias/media/"+str(row["id"])+"/view?t="+
            temporary_link(g,uid,row["id"],"view")) if x["available"] else None
        x.pop("state",None)
        x.pop("thumb_key",None)
        result.append(x)
    return {"items":result,"next":rows[59]["id"] if len(rows)>60 else None}
