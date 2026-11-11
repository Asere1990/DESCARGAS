"""Acciones administrativas y editor visual de Web Galerías, solo admins.txt."""
import asyncio
import base64
import json
import re
import time
from aiohttp import web
from .permissions import is_admin,is_tester
from .core import DEFAULTS,ASSET_MIME,MAX_ASSET,identifier
from .covers import ALLOWED_MIME,MAX_UPLOAD,optimized_cover,temporary_cover

TEXTS={"title","button_label","intro_caption","intro_label"}
WATERMARK_TEXTS={"wm_line1","wm_line2"}
WATERMARK_KEYS={"wm_enabled","wm_line1","wm_line2","wm_position","wm_opacity","wm_size","wm_color"}
COLORS={"button_color","accent","background","text_color","wm_color"}
CHOICES={"header_mode":{"text","image"},"tile_shape":{"square","rectangle"},
         "wm_position":{"center","top","bottom"}}

def cleaned_design(value):
    if not isinstance(value,dict) or set(value)-set(DEFAULTS):
        raise web.HTTPBadRequest(text="Configuración del editor incorrecta.")
    result=dict(DEFAULTS)
    for key,val in value.items():
        if key=="daily_limit":
            if val!=100:raise web.HTTPBadRequest(text="El límite diario está fijado en 100.")
        elif key in TEXTS:
            if not isinstance(val,str) or len(val)>160 or any(ord(c)<32 for c in val):
                raise web.HTTPBadRequest(text="Texto inválido.")
            result[key]=val
        elif key in WATERMARK_TEXTS:
            if not isinstance(val,str) or len(val)>90 or any(ord(c)<32 for c in val):
                raise web.HTTPBadRequest(text="Texto de marca de agua inválido.")
            result[key]=val
        elif key=="wm_enabled":
            if type(val) is not bool:
                raise web.HTTPBadRequest(text="Estado de marca de agua inválido.")
            result[key]=val
        elif key in ("wm_opacity","wm_size"):
            limits={"wm_opacity":(0,100),"wm_size":(12,42)}
            lo,hi=limits[key]
            if type(val) is not int or not lo<=val<=hi:
                raise web.HTTPBadRequest(text="Valor de marca de agua inválido.")
            result[key]=val
        elif key in COLORS:
            if not isinstance(val,str) or not re.fullmatch(r"#[0-9a-fA-F]{6}",val):
                raise web.HTTPBadRequest(text="Color inválido.")
            result[key]=val
        elif key in CHOICES:
            if not isinstance(val,str) or val not in CHOICES[key]:
                raise web.HTTPBadRequest(text="Opción inválida.")
            result[key]=val
    return result

async def admin_op(g,uid,op,data):
    if not is_admin(uid):raise web.HTTPForbidden(text="Solo administradores.")
    db=g.db()
    if op=="admin-overview":
        folders=await db.fetch("""SELECT f.id,f.peer,f.name,
            COALESCE(f.public_name,'') AS public_name,
            f.active,f.error,f.complete,f.scan_count,
            f.last_check,f.source_uid,MAX(c.updated) AS cover_updated,
            MAX(m.id) FILTER(WHERE m.state IN ('ready','preview')
              AND m.thumb_key IS NOT NULL) AS automatic_cover,
            COUNT(m.id)::int AS media_total,
            COUNT(m.id) FILTER(WHERE m.source_type='photo' OR
              (m.source_type='' AND m.kind='photo'))::int AS photo_native,
            COUNT(m.id) FILTER(WHERE m.source_type='video' OR
              (m.source_type='' AND m.kind='video'))::int AS video_native,
            COUNT(m.id) FILTER(WHERE m.source_type='photo_file')::int AS photo_files,
            COUNT(m.id) FILTER(WHERE m.source_type='video_file')::int AS video_files,
            COUNT(m.id) FILTER(WHERE m.source_type='gif')::int AS gifs,
            COUNT(m.id) FILTER(WHERE m.source_type='')::int AS legacy_uncategorized,
            COUNT(m.id) FILTER(WHERE m.state='pending' AND
              m.error='en_revalidacion')::int AS rechecking,
            COUNT(m.id) FILTER(WHERE m.state='ready')::int AS ready,
            COUNT(m.id) FILTER(WHERE m.state IN ('pending','preview'))::int AS pending,
            COUNT(m.id) FILTER(WHERE m.state='missing')::int AS missing,
            COUNT(m.id) FILTER(WHERE m.state='missing' AND
              m.error IN ('message_absent_after_rechecks','media_removed_after_rechecks'))::int
              AS missing_rechecked,
            COUNT(m.id) FILTER(WHERE m.state='skipped')::int AS skipped
            FROM wg_folders f LEFT JOIN wg_media m ON m.folder=f.id
            LEFT JOIN wg_folder_covers c ON c.folder=f.id
            GROUP BY f.id ORDER BY f.id DESC""")
        reasons=await db.fetch("""SELECT folder,error,COUNT(*)::int AS amount
          FROM wg_media WHERE state IN ('missing','skipped')
          GROUP BY folder,error ORDER BY folder,amount DESC""")
        reason_map={}
        for entry in reasons:
            reason_map.setdefault(entry["folder"],[]).append({
              "code":entry["error"] or "sin_motivo","count":entry["amount"]})
        users=await db.fetch("""SELECT u.uid,u.name,u.first_seen,u.last_seen,
            s.state,s.paid_until,p.mode
            FROM wg_users u LEFT JOIN wg_subscriptions s ON s.uid=u.uid
            LEFT JOIN wg_user_prefs p ON p.uid=u.uid
            ORDER BY u.last_seen DESC LIMIT 500""")
        remote=await db.fetchval("SELECT value FROM wg_config WHERE key='backup_status'")
        if isinstance(remote,str):
            remote=json.loads(remote)
        if not isinstance(remote,dict):
            remote={}
        running=bool(remote.get("enabled") and time.time()-float(remote.get("updated") or 0)<150)
        result_folders=[]
        for item in folders:
            folder=dict(item)
            updated=folder.pop("cover_updated",None)
            folder["has_custom_cover"]=bool(updated)
            if updated:
                folder["cover"]=temporary_cover(g,uid,folder["id"])
            elif folder.get("automatic_cover"):
                from .downloads import temporary_link
                photo_id=folder["automatic_cover"]
                folder["cover"]=("/web-galerias/media/"+str(photo_id)+"/thumb?t="+
                  temporary_link(g,uid,photo_id,"thumb"))
            else:
                folder["cover"]=None
            folder.pop("automatic_cover",None)
            folder["missing_reasons"]=reason_map.get(folder["id"],[])
            result_folders.append(folder)
        return {"folders":result_folders,
            "users":[{**dict(x),"admin":is_admin(x["uid"]),"tester":is_tester(x["uid"])} for x in users],
            "storage_ready":g.storage.ready or bool(remote.get("storage") and running),
            "sync_enabled":running,"sync_phase":remote.get("phase") if running else "detenido",
            "sync_folder":remote.get("folder") if running else None,
            "last_error":g.last_error}
    if op=="admin-missing-samples":
        folder=identifier(data.get("folder"))
        exists=await db.fetchval("SELECT 1 FROM wg_folders WHERE id=$1",folder)
        if not exists:raise web.HTTPNotFound(text="Galería no encontrada.")
        rows=await db.fetch("""SELECT message,kind,mime,source_type,error
          FROM wg_media WHERE folder=$1 AND state='missing'
          ORDER BY id DESC LIMIT 10""",folder)
        return {"items":[dict(row) for row in rows]}
    if op=="admin-recheck-missing":
        # La base de datos externa permanece privada. Volver a consultar
        # diez incidencias desde el sincronizador normal, sin conexiones
        # extra, ni borrar nada de R2.
        folder=identifier(data.get("folder"))
        found=await db.fetchval("SELECT 1 FROM wg_folders WHERE id=$1",folder)
        if not found:raise web.HTTPNotFound(text="Galería no encontrada.")
        changed=await db.fetch("""WITH candidate AS (
           SELECT id FROM wg_media WHERE folder=$1 AND state='missing'
           ORDER BY CASE WHEN error='no_disponible_en_origen'
              THEN 0 ELSE 1 END,id DESC
           FOR UPDATE SKIP LOCKED LIMIT 10)
           UPDATE wg_media AS m SET state='pending',source_checks=0,
             attempts=0,available=0,error='en_revalidacion'
           FROM candidate c WHERE m.id=c.id RETURNING m.id""",folder)
        await g.log(uid,op)
        return {"ok":True,"scheduled":len(changed)}
    if op=="admin-folder":
        try:peer=int(data.get("peer",0))
        except (ValueError,TypeError,OverflowError):raise web.HTTPBadRequest(text="ID incorrecto.")
        if not -2**63<peer<0:raise web.HTTPBadRequest(text="Escribe el ID completo negativo del grupo o canal.")
        name=str(data.get("name","")).strip()
        if not name or len(name)>100:raise web.HTTPBadRequest(text="Nombre interno incorrecto.")
        public_name=data.get("public_name")
        if public_name is not None:
            if not isinstance(public_name,str) or not public_name.strip() or len(public_name.strip())>100:
                raise web.HTTPBadRequest(text="Nombre visible incorrecto.")
            public_name=public_name.strip()
        row=await db.fetchrow("""INSERT INTO wg_folders(peer,name,public_name,created)
          VALUES($1,$2,$3,$4)
          ON CONFLICT(peer) DO UPDATE SET name=excluded.name,
            public_name=COALESCE(excluded.public_name,wg_folders.public_name),
            active=TRUE
          RETURNING id""",peer,name,public_name,time.time())
        # Reactivación conserva todas las copias ready en R2 y los cursores.
        # Solo los archivos antes no disponibles pueden volver a intentarse.
        await db.execute("""UPDATE wg_media SET state='pending',
            available=0,error='' WHERE folder=$1 AND state='missing'""",row["id"])
        await g.log(uid,op)
        return {"ok":True,"id":row["id"]}
    if op=="admin-folder-cover-remove":
        folder=identifier(data.get("folder"))
        found=await db.fetchval("SELECT 1 FROM wg_folders WHERE id=$1",folder)
        if not found:raise web.HTTPNotFound(text="Galería no encontrada.")
        await db.execute("DELETE FROM wg_folder_covers WHERE folder=$1",folder)
        await g.log(uid,op)
        return {"ok":True}
    if op=="admin-folder-update":
        folder=identifier(data.get("folder"))
        field=data.get("field","name")
        if field not in ("name","public_name"):
            raise web.HTTPBadRequest(text="Tipo de nombre incorrecto.")
        name=data.get("name")
        if not isinstance(name,str) or not name.strip() or len(name.strip())>100:
            raise web.HTTPBadRequest(text="Nombre inválido.")
        if field=="name":
            await db.execute("UPDATE wg_folders SET name=$2 WHERE id=$1",folder,name.strip())
        else:
            await db.execute("UPDATE wg_folders SET public_name=$2 WHERE id=$1",
                             folder,name.strip())
        await g.log(uid,op)
        return {"ok":True}
    if op=="admin-folder-remove":
        folder=identifier(data.get("folder"))
        # Desactivar en galería, nunca abandonar, borrar o denunciar grupos de Telegram.
        await db.execute("UPDATE wg_folders SET active=FALSE WHERE id=$1",folder)
        await g.log(uid,op)
        return {"ok":True}
    if op=="admin-user-access":
        target=identifier(data.get("user"))
        mode=await db.fetchval("SELECT mode FROM wg_user_prefs WHERE uid=$1",target) or "all"
        rows=await db.fetch("SELECT folder FROM wg_user_folders WHERE uid=$1",target)
        return {"mode":mode,"folders":[x["folder"] for x in rows]}
    if op=="admin-access":
        target=identifier(data.get("user"))
        mode=data.get("mode");folders=data.get("folders",[])
        if mode not in ("all","selected") or not isinstance(folders,list) or len(folders)>300:
            raise web.HTTPBadRequest(text="Permisos de carpetas incorrectos.")
        ids=list(dict.fromkeys(identifier(f) for f in folders))
        async with db.acquire() as c,c.transaction():
            await c.execute("""INSERT INTO wg_user_prefs(uid,mode) VALUES($1,$2)
                ON CONFLICT(uid) DO UPDATE SET mode=excluded.mode""",target,mode)
            await c.execute("DELETE FROM wg_user_folders WHERE uid=$1",target)
            if mode=="selected" and ids:
                valid=await c.fetch("SELECT id FROM wg_folders WHERE id=ANY($1::bigint[])",ids)
                await c.executemany("INSERT INTO wg_user_folders(uid,folder) VALUES($1,$2)",
                                     [(target,r["id"]) for r in valid])
        await g.log(uid,op)
        return {"ok":True}
    if op in ("admin-design","admin-watermark"):
        current=await g.design()
        if op=="admin-watermark":
            changes=data.get("watermark")
            if not isinstance(changes,dict) or set(changes)!=WATERMARK_KEYS:
                raise web.HTTPBadRequest(text="Datos de marca de agua inválidos.")
            # Solo actualiza la marca de agua: mantiene colores, textos y demás diseño.
            design=cleaned_design({**current,**changes})
        else:
            changes=data.get("design")
            if not isinstance(changes,dict):
                raise web.HTTPBadRequest(text="Configuración del editor incorrecta.")
            # Una versión anterior del editor no debe borrar la marca de agua guardada.
            saved={k:current.get(k,DEFAULTS[k]) for k in WATERMARK_KEYS}
            design=cleaned_design({**saved,**changes})
        await db.execute("""INSERT INTO wg_config(key,value) VALUES('design',$1::jsonb)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",json.dumps(design))
        await g.log(uid,op)
        return {"ok":True,"design":design}
    if op=="admin-upload":
        return await upload(g,uid,data)
    if op=="admin-sync-reset":
        folder=identifier(data.get("folder"))
        await db.execute("UPDATE wg_folders SET scan_cursor=0,complete=FALSE,error='' WHERE id=$1",folder)
        await g.log(uid,op)
        return {"ok":True}
    raise web.HTTPNotFound()

async def upload(g,uid,data):
    """Subida a trozos de 192KiB para no cambiar el límite HTTP de DESCARGAS."""
    key=data.get("key");mime=data.get("mime")
    is_cover=key=="folder-cover"
    folder=None
    if is_cover:
        folder=identifier(data.get("folder"))
        if mime not in ALLOWED_MIME:
            raise web.HTTPBadRequest(text="Formato de portada no admitido.")
        exists=await g.db().fetchval("SELECT 1 FROM wg_folders WHERE id=$1",folder)
        if not exists:raise web.HTTPNotFound(text="Galería no encontrada.")
        key="folder-cover:"+str(folder)
    elif key not in ASSET_MIME or mime not in ASSET_MIME[key]:
        raise web.HTTPBadRequest(text="Formato de archivo no admitido.")
    upload=str(data.get("upload",""))
    if not re.fullmatch("[a-f0-9]{32}",upload):raise web.HTTPBadRequest(text="Identificador de subida incorrecto.")
    part=identifier(data.get("part"),0);total=identifier(data.get("total"))
    if total>130 or part>=total:raise web.HTTPBadRequest(text="Cantidad de bloques inválida.")
    try:body=base64.b64decode(data.get("data",""),validate=True)
    except Exception:raise web.HTTPBadRequest(text="Bloque inválido.") from None
    if not body or len(body)>192*1024:raise web.HTTPBadRequest(text="Bloque demasiado grande.")
    db=g.db()
    async with db.acquire() as c,c.transaction():
        await c.execute("""INSERT INTO wg_upload_parts(upload,uid,key,mime,part,total,body,created)
          VALUES($1,$2,$3,$4,$5,$6,$7,$8)
          ON CONFLICT(upload,part) DO NOTHING""",upload,uid,key,mime,part,total,body,time.time())
        rows=await c.fetch("""SELECT part,total,key,mime,body FROM wg_upload_parts
          WHERE upload=$1 AND uid=$2 ORDER BY part""",upload,uid)
        if len(rows)!=total:return {"ok":True,"received":len(rows),"total":total}
        if any(x["part"]!=i or x["total"]!=total or x["key"]!=key or x["mime"]!=mime
               for i,x in enumerate(rows)):raise web.HTTPBadRequest(text="Los bloques no coinciden.")
        blob=b"".join(bytes(x["body"]) for x in rows)
        if len(blob)>(MAX_UPLOAD if is_cover else MAX_ASSET[key]):
            raise web.HTTPBadRequest(text="Archivo demasiado grande.")
        if is_cover:
            # Nunca alterar el origen o el contenido en R2. Guardar portada aparte.
            image=await asyncio.to_thread(optimized_cover,blob,mime)
            await c.execute("""INSERT INTO wg_folder_covers(folder,mime,body,updated)
              VALUES($1,'image/jpeg',$2,$3)
              ON CONFLICT(folder) DO UPDATE SET mime=excluded.mime,
               body=excluded.body,updated=excluded.updated""",folder,image,time.time())
        else:
            if mime=="image/png" and not blob.startswith(b"\x89PNG\r\n\x1a\n"):
                raise web.HTTPBadRequest(text="Formato PNG inválido.")
            if mime=="image/jpeg" and not blob.startswith(b"\xff\xd8\xff"):
                raise web.HTTPBadRequest(text="Formato JPEG inválido.")
            if mime=="image/webp" and not (blob.startswith(b"RIFF") and blob[8:12]==b"WEBP"):
                raise web.HTTPBadRequest(text="Formato WEBP inválido.")
            if mime.startswith("video/") and not (
                b"ftyp" in blob[:64] or blob.startswith(b"\x1a\x45\xdf\xa3")):
                raise web.HTTPBadRequest(text="Formato de video inválido.")
            await c.execute("""INSERT INTO wg_assets(key,mime,body,updated) VALUES($1,$2,$3,$4)
              ON CONFLICT(key) DO UPDATE SET mime=excluded.mime,body=excluded.body,updated=excluded.updated""",
              key,mime,blob,time.time())
        await c.execute("DELETE FROM wg_upload_parts WHERE upload=$1",upload)
    await g.log(uid,"admin-folder-cover" if is_cover else "admin-asset-"+key)
    return {"ok":True,"complete":True,"folder":folder} if is_cover else {"ok":True,"complete":True}

async def asset(g,request):
    key=request.match_info["key"]
    if key not in ASSET_MIME:raise web.HTTPNotFound()
    row=await g.db().fetchrow("SELECT mime,body FROM wg_assets WHERE key=$1",key)
    if not row:raise web.HTTPNotFound()
    body=bytes(row["body"])
    headers={"Accept-Ranges":"bytes","Cache-Control":"public, max-age=300",
             "X-Content-Type-Options":"nosniff"}
    rng=request.headers.get("Range","")
    if rng:
        m=re.fullmatch(r"bytes=(\d*)-(\d*)",rng)
        if not m:raise web.HTTPRequestRangeNotSatisfiable()
        start=int(m.group(1) or 0)
        end=min(int(m.group(2) or len(body)-1),len(body)-1)
        if start>end or start>=len(body):raise web.HTTPRequestRangeNotSatisfiable()
        headers["Content-Range"]=f"bytes {start}-{end}/{len(body)}"
        return web.Response(status=206,body=body[start:end+1],
                            content_type=row["mime"],headers=headers)
    return web.Response(body=body,content_type=row["mime"],headers=headers)
