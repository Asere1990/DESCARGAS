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
