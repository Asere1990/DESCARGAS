"""Find the original CUBANITAS gallery position for one authorized media item.

Uses the same stable ID ordering as the existing original gallery listing,
not the order of a custom personal folder or search results.
"""
from aiohttp import web


async def locate(g,uid,data):
    from .core import identifier
    from .downloads import temporary_link
    target=identifier(data.get("media"))
    db=g.db()
    record=await db.fetchrow("""
        SELECT m.id,m.folder,COALESCE(NULLIF(f.public_name,''),'Galería '||f.id::text) AS name,
               f.active
        FROM wg_media m JOIN wg_folders f ON f.id=m.folder
        WHERE m.id=$1 AND m.state IN ('preview','ready')""",target)
    if not record or not record["active"] or not await g.permitted(uid,record["folder"]):
        raise web.HTTPForbidden(text="Este archivo no está disponible en tus galerías.")
    folder=record["folder"]
    newer=await db.fetch("""
        SELECT id,kind,mime,filename,date,width,height,size,state,thumb_key,file_key
        FROM wg_media WHERE folder=$1 AND id>$2 AND state IN ('preview','ready')
        ORDER BY id ASC LIMIT 25""",folder,target)
    older=await db.fetch("""
        SELECT id,kind,mime,filename,date,width,height,size,state,thumb_key,file_key
        FROM wg_media WHERE folder=$1 AND id<=$2 AND state IN ('preview','ready')
        ORDER BY id DESC LIMIT 36""",folder,target)
    rows=list(reversed(newer))+older[:35]
    if not any(x["id"]==target for x in rows):
        raise web.HTTPNotFound(text="Este archivo ya no está disponible.")
    result=[]
    for x in rows:
        item={key:x[key] for key in
              ("id","kind","mime","filename","date","width","height","size")}
        item["available"]=x["state"]=="ready"
        item["thumb"]=("/web-galerias/media/"+str(x["id"])+"/thumb?t="+
                       temporary_link(g,uid,x["id"],"thumb")) if x["thumb_key"] else None
        item["view"]=("/web-galerias/media/"+str(x["id"])+"/view?t="+
                      temporary_link(g,uid,x["id"],"view")) if item["available"] and x["file_key"] else None
        result.append(item)
    return {"folder":{"id":folder,"name":record["name"],"personal":False},
            "focus":target,"items":result,
            "next":older[34]["id"] if len(older)>35 else None}
