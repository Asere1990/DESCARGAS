"""Additional Telegram groups/channels inside a single public Web Galerías folder.

Each source remains a normal wg_folders row with independent Telegram cursor,
peer access hash, reader account, media IDs, retry state, and R2 objects.
Only public presentation/authorization are grouped under a parent folder.
"""
import time
from aiohttp import web

SCHEMA = """
CREATE TABLE IF NOT EXISTS wg_folder_sources (
 parent_folder BIGINT NOT NULL REFERENCES wg_folders(id) ON DELETE CASCADE,
 source_folder BIGINT PRIMARY KEY REFERENCES wg_folders(id) ON DELETE CASCADE,
 created DOUBLE PRECISION NOT NULL,
 CONSTRAINT wg_folder_sources_distinct CHECK(parent_folder<>source_folder)
);
CREATE INDEX IF NOT EXISTS wg_folder_sources_parent
 ON wg_folder_sources(parent_folder,source_folder);
ALTER TABLE wg_folder_sources ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON wg_folder_sources FROM PUBLIC;
-- A public folder keeps its historical media in R2 even if its Telegram reader is gone.
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS source_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS source_access_status TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS last_source_uid BIGINT;
"""

SUM_FIELDS=(
    'scan_count','media_total','visible_photos','visible_videos',
    'photo_native','video_native','photo_files','video_files','gifs',
    'legacy_uncategorized','rechecking','ready','preview_only',
    'previews_with_images','pending','missing','missing_rechecked','skipped',
)
TOTAL_FIELDS=('telegram_photos_total','telegram_videos_total')


async def add(g,actor,data):
    """Attach a brand-new, independent source; never migrate an existing peer."""
    from .core import identifier
    from .admin import linked_source_account
    parent=identifier(data.get('folder'))
    try:
        peer=int(data.get('peer',0))
    except (TypeError,ValueError,OverflowError):
        raise web.HTTPBadRequest(text='ID de grupo o canal incorrecto.')
    if not -2**63 < peer < 0:
        raise web.HTTPBadRequest(text='Escribe el ID negativo completo del grupo o canal.')
    uid=await linked_source_account(
        g,data.get('account_uid'),data.get('account_system','descargas'))
    db=g.db()
    async with db.acquire() as conn,conn.transaction():
        # Serialise attachments to the same parent, and disallow child-of-child.
        parent_row=await conn.fetchrow("""
            SELECT f.id,f.peer,f.name,f.public_name,f.active
            FROM wg_folders f WHERE f.id=$1
              AND NOT EXISTS(SELECT 1 FROM wg_folder_sources link
                             WHERE link.source_folder=f.id)
            FOR UPDATE OF f
        """,parent)
        if not parent_row:
            raise web.HTTPNotFound(text='Carpeta principal no encontrada.')
        if not parent_row['active']:
            raise web.HTTPBadRequest(text='Activa la carpeta antes de agregar grupos.')
        if int(parent_row['peer'])==peer:
            raise web.HTTPBadRequest(text='Este grupo ya alimenta la carpeta.')
        previous=await conn.fetchrow(
            'SELECT id FROM wg_folders WHERE peer=$1',peer)
        if previous:
            attached=await conn.fetchval("""
                SELECT 1 FROM wg_folder_sources
                WHERE parent_folder=$1 AND source_folder=$2
            """,parent,int(previous['id']))
            if attached:
                raise web.HTTPConflict(text='Este grupo ya está conectado a la carpeta.')
            # Do not silently steal/copy historical messages, covers or permissions
            # from a different gallery. The existing group remains untouched.
            raise web.HTTPConflict(
                text='El grupo ya existe en otra galería. No se modificó ninguna carpeta.')
        child=await conn.fetchval("""
            INSERT INTO wg_folders(
              peer,name,public_name,active,created,preferred_uid,source_uid)
            VALUES($1,$2,NULL,TRUE,$3,$4,$4) RETURNING id
        """,peer,str(parent_row['name'])+' · '+str(peer),time.time(),uid)
        await conn.execute("""
            INSERT INTO wg_folder_sources(parent_folder,source_folder,created)
            VALUES($1,$2,$3)
        """,parent,child,time.time())
    await g.log(actor,'admin-folder-add-source')
    return {'ok':True,'folder':parent,'source_folder':child,'peer':peer,
            'account_uid':uid}


async def verified_reader(g,uid,peer):
    """Prove the replacement Telegram session can read this exact group/channel.

    Read-only: use the existing exclusive session lock and never alter linked
    accounts in DATOS/DESCARGAS. Verification happens before changing cursors.
    """
    import asyncio
    from descargas_app.account_chat import connected
    source=g.s if uid>0 else getattr(g.s,"datos",None)
    if source is None or (uid<0 and not getattr(source,"ready",False)):
        raise web.HTTPConflict(text="El sistema de esa cuenta no está disponible.")
    try:
        async with asyncio.timeout(35):
            async with connected(source,abs(uid)) as client:
                async for dialog in client.iter_dialogs():
                    if int(dialog.id)==int(peer):
                        return True
    except TimeoutError:
        raise web.HTTPConflict(text="No se pudo verificar el grupo a tiempo. Reintenta.") from None
    except ValueError as exc:
        raise web.HTTPConflict(text=str(exc)[:200]) from None
    from telethon import errors
    # Telegram RPC errors propagate to the API's normal error boundary.
    raise web.HTTPBadRequest(text="Esta cuenta no tiene acceso al mismo Group ID o Channel ID.")


def revoked_error(exc):
    """Only definite invalid-session errors, never timeouts or FloodWait."""
    from telethon import errors
    if isinstance(exc,(errors.SessionRevokedError,errors.SessionExpiredError,
                       errors.AuthKeyUnregisteredError)):
        return True
    return isinstance(exc,ValueError) and (
        "sesión fue revocada" in str(exc).lower()
        or "session_revoked" in str(exc).lower()
        or "session_expired" in str(exc).lower())


async def mark_revoked(g,uid):
    """Pause every gallery source using the revoked signed registry ID.

    Keep its cursor and account assignment for a seamless same-peer handoff.
    Do NOT delete any R2 object, media row, cover, personal item or thumbnail.
    """
    db=g.db()
    async with db.acquire() as conn,conn.transaction():
        changed=await conn.fetch("""
          UPDATE wg_folders SET source_access_status='revoked',
            last_source_uid=$1,
            error='Sesión de Telegram revocada: conecta otra cuenta del mismo grupo para conservar el avance'
          WHERE source_enabled AND source_access_status<>'revoked'
            AND (preferred_uid=$1 OR
                 (preferred_uid IS NULL AND source_uid=$1))
          RETURNING id
        """,uid)
        ids=[int(r["id"]) for r in changed]
        if ids:
            await conn.execute("""
              UPDATE wg_media SET state='paused_preview'
              WHERE folder=ANY($1::bigint[]) AND state='preview'
            """,ids)
    return ids


async def disconnect(g,actor,data,kind):
    """Disable ONLY this Telegram source, retaining ready objects and folder IDs.

    A later reconnect resets its Telegram cursor; a handoff before disconnect
    instead preserves that cursor and the previews.
    """
    from .core import identifier
    folder=identifier(data.get("folder"))
    db=g.db()
    async with db.acquire() as conn,conn.transaction():
        row=await conn.fetchrow("""
          SELECT id,source_enabled,preferred_uid,source_uid,last_source_uid
          FROM wg_folders WHERE id=$1 FOR UPDATE
        """,folder)
        if not row:
            raise web.HTTPNotFound(text="Origen de Telegram no encontrado.")
        if not row["source_enabled"]:
            return {"ok":True,"changed":False,"r2_preserved":True}
        old=row["preferred_uid"] or row["source_uid"] or row["last_source_uid"]
        await conn.execute("""
          UPDATE wg_folders SET source_enabled=FALSE,
            source_access_status='disconnected',preferred_uid=NULL,
            source_uid=NULL,last_source_uid=$2,
            error=$3
          WHERE id=$1
        """,folder,old,("Grupo/canal desconectado; archivos de Cloudflare conservados"
                         if kind=="source" else
                         "Cuenta desconectada; asigna otra para comenzar desde cero"))
        # Hide only previews without an original. Preserve their R2 thumbnail
        # keys, all 'ready' originals and every user/personal-folder reference.
        result=await conn.execute("""
          UPDATE wg_media SET state='pending',available=0,
            error='origen_desconectado'
          WHERE folder=$1 AND state IN ('preview','paused_preview')
        """,folder)
    await g.log(actor,"admin-folder-disconnect-"+kind)
    return {"ok":True,"changed":True,
            "previews_hidden":int(result.split()[-1]),"r2_preserved":True}


async def change_reader(g,actor,data,carry):
    """Validated handoff (keep progress) or reconnection (reset from zero)."""
    from .core import identifier
    from .admin import linked_source_account
    folder=identifier(data.get("folder"))
    incoming=await linked_source_account(
        g,data.get("account_uid"),data.get("account_system","descargas"))
    db=g.db()
    old=await db.fetchrow("""
      SELECT peer,source_enabled FROM wg_folders WHERE id=$1
    """,folder)
    if not old:
        raise web.HTTPNotFound(text="Grupo/canal no encontrado.")
    if bool(old["source_enabled"])!=bool(carry):
        raise web.HTTPConflict(text=(
            "La fuente fue desconectada. Usa Conectar desde cero." if carry
            else "La fuente sigue conectada. Usa Reemplazar sin reiniciar."))
    await verified_reader(g,incoming,int(old["peer"]))
    async with db.acquire() as conn,conn.transaction():
        row=await conn.fetchrow("""
          SELECT peer,source_enabled,preferred_uid,source_uid
          FROM wg_folders WHERE id=$1 FOR UPDATE
        """,folder)
        if not row or row["peer"]!=old["peer"] or bool(row["source_enabled"])!=bool(carry):
            raise web.HTTPConflict(text="El origen cambió. Actualiza el panel.")
        if carry and incoming==(row["preferred_uid"] or row["source_uid"]):
            raise web.HTTPConflict(text="Esa cuenta ya estaba asignada.")
        if carry:
            await conn.execute("""
              UPDATE wg_folders SET preferred_uid=$2,source_uid=$2,
                last_source_uid=$2,source_access_status='ok',
                last_check=0,telegram_counts_next_attempt=0,error=''
              WHERE id=$1
            """,folder,incoming)
            await conn.execute("""
              UPDATE wg_media SET state='preview',error=''
              WHERE folder=$1 AND state='paused_preview'
            """,folder)
        else:
            await conn.execute("""
              UPDATE wg_folders SET source_enabled=TRUE,preferred_uid=$2,
                source_uid=$2,last_source_uid=$2,source_access_status='ok',
                scan_cursor=0,high_water=0,complete=FALSE,
                last_check=0,error='',scan_count=0,
                telegram_photos_total=NULL,telegram_videos_total=NULL,
                telegram_counts_at=0,telegram_counts_next_attempt=0,
                telegram_counts_error=''
              WHERE id=$1
            """,folder,incoming)
            await conn.execute("""
              UPDATE wg_media SET state='pending',available=0,
                error='revalidar_con_nueva_cuenta'
              WHERE folder=$1 AND state IN ('preview','paused_preview')
            """,folder)
        # A previous failure for this account must not block the new request.
        await conn.execute("""
          DELETE FROM wg_discovery WHERE folder=$1 AND uid=$2
        """,folder,incoming)
    await g.log(actor,"admin-folder-handoff" if carry else "admin-folder-reconnect")
    return {"ok":True,"folder":folder,"account_uid":incoming,
            "progress_preserved":bool(carry),"r2_preserved":True}


def combine_admin_rows(rows,links):
    """Aggregate counts without touching per-source synchronization state."""
    records={int(r['id']):dict(r) for r in rows}
    children={}
    for link in links:
        parent=int(link['parent_folder']);child=int(link['source_folder'])
        if parent in records and child in records:
            children.setdefault(parent,[]).append(child)
    child_ids={child for family in children.values() for child in family}
    result=[]
    for fid,source in records.items():
        if fid in child_ids:
            continue
        root=dict(source)
        family=[source]+[records[child] for child in children.get(fid,())]
        root['sources']=[
            {'id':int(s['id']),'peer':int(s['peer']),
             'account_uid':s.get('preferred_uid') or s.get('source_uid'),
             'last_account_uid':s.get('last_source_uid'),
             'source_enabled':s.get('source_enabled',True),
             'access_status':s.get('source_access_status') or 'unknown',
             'scan_count':int(s.get('scan_count') or 0),
             'error':s.get('error') or '',
             'complete':bool(s.get('complete'))}
            for s in family]
        for key in SUM_FIELDS:
            root[key]=sum(int(s.get(key) or 0) for s in family)
        for key in TOTAL_FIELDS:
            values=[s.get(key) for s in family]
            root[key]=sum(int(x) for x in values) if all(x is not None for x in values) else None
        root['complete']=all(bool(s.get('complete')) for s in family)
        root['last_check']=max(float(s.get('last_check') or 0) for s in family)
        root['telegram_counts_at']=min(
            (float(s.get('telegram_counts_at') or 0) for s in family),default=0)
        root['automatic_cover']=max(
            (int(s.get('automatic_cover') or 0) for s in family),default=0) or None
        reasons=[f"{s['peer']}: {s['error']}" for s in family if s.get('error')]
        root['error']=' | '.join(reasons)[:1000]
        root['telegram_counts_error']=' | '.join(
            f"{s['peer']}: {s['telegram_counts_error']}" for s in family
            if s.get('telegram_counts_error'))[:1000]
        result.append(root)
    return result


def public_sources_sql(field='folder',placeholder='$1'):
    """Use indexed per-source folder IDs; media row IDs remain globally unique."""
    return (f"(({field}={placeholder} OR {field} IN ("
            f"SELECT source_folder FROM wg_folder_sources "
            f"WHERE parent_folder={placeholder})) AND EXISTS ("
            f"SELECT 1 FROM wg_folders origin "
            f"WHERE origin.id={field} AND origin.active))")
