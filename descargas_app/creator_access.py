"""Administrative privacy boundaries, independent of normal user downloads."""
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from telethon import types

ROOT=Path(__file__).resolve().parent.parent
ACTOR=ContextVar('creator_review_actor',default=None)
DENIED='Acceso restringido. Esta cuenta u origen está protegido.'
SCHEMA="""
CREATE TABLE IF NOT EXISTS descarga_creator_consent(
 bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,generation BIGINT NOT NULL,
 version TEXT NOT NULL,accepted DOUBLE PRECISION NOT NULL,revoked BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(bot_id,uid));
CREATE TABLE IF NOT EXISTS descarga_creator_owners(
 bot_id BIGINT NOT NULL,source BIGINT NOT NULL,owner BIGINT NOT NULL,
 PRIMARY KEY(bot_id,source,owner));
"""
VERSION='creator-review-v1'

def ids(name):
    # Missing or unreadable policy files deny administrative review, never open it.
    try:text=(ROOT/name).read_text()
    except OSError:raise ValueError('No se pudo leer la política de acceso. Revisión administrativa deshabilitada.')
    result=set()
    for line in text.splitlines():
        for token in line.split('#',1)[0].replace(',',' ').split():
            if not token.isdecimal() or not 0<int(token)<2**63:
                raise ValueError('La política de acceso contiene un ID no válido.')
            result.add(int(token))
    from .policy_editor import extra
    return result | extra(name)

def police(actor):return actor in ids('admins.txt')
def reviewer(actor):return actor not in ids('admins.txt') and actor in ids('permisos.txt')

def protected_ids(actor=None):return set() if police(ACTOR.get() if actor is None else actor) else ids('cuentas.txt') | ids('admins.txt')
def require_id(uid,actor=None):
    actor=ACTOR.get() if actor is None else actor
    if actor is not None and uid in protected_ids(actor):raise ValueError(DENIED)

async def registrations(s):
    reader=getattr(s.repo,"creator_ids",None)
    return await reader(s.bot_id) if reader else []

async def require_account(s,uid,actor=None):
    actor=ACTOR.get() if actor is None else actor
    if actor is None:return
    require_id(uid,actor)
    reader=getattr(s.repo,'creator_registration',None)
    row=await reader(s.bot_id,uid) if reader else None
    if row and actor!=uid:
        if not police(actor):raise ValueError(DENIED)
        if row['revoked'] or row['version']!=VERSION or row['generation']!=row['current_generation']:raise ValueError('La revisión de esta cuenta no está autorizada o fue revocada.')

async def require_source(s,uid,source,client=None,entity=None,actor=None):
    actor=ACTOR.get() if actor is None else actor
    if actor is None:return
    await require_account(s,uid,actor)
    if police(actor):return
    from .protected import sources,bots
    blocked_bots=bots()
    if source in sources() or str(source) in blocked_bots:raise ValueError(DENIED)
    if entity is not None and getattr(entity,'bot',False) and (getattr(entity,'username',None) or '').lower() in blocked_bots:raise ValueError(DENIED)
    protected=ids('cuentas.txt') | ids('admins.txt')
    protected.update(await registrations(s))
    if source>0:
        if source in protected or source in ids('admins.txt'):raise ValueError(DENIED)
        return
    if not protected:return
    if await s.repo.pool.fetchval('SELECT 1 FROM descarga_creator_owners WHERE bot_id=$1 AND source=$2 AND owner=ANY($3::bigint[])',s.bot_id,source,list(protected)):raise ValueError(DENIED)
    if client is None:
        known=await s.repo.pool.fetchval('SELECT 1 FROM descarga_creator_owners WHERE bot_id=$1 AND source=$2',s.bot_id,source)
        if known:return
        raise ValueError('Abre el origen para verificar su propietario antes de continuar.')
    if entity is None:entity=await client.get_entity(source)
    # Telegram exposes the current owner, not the identity of an original creator after transfer.
    try:
        if getattr(entity,'creator',False):owner=uid
        else:
            owners=await client.get_participants(entity,filter=types.ChannelParticipantsAdmins())
            owner=next((u.id for u in owners if isinstance(getattr(u,'participant',None),(types.ChannelParticipantCreator,types.ChatParticipantCreator))),None)
        if owner is None:raise ValueError('No se pudo verificar el propietario del origen.')
    except Exception as exc:
        if isinstance(exc,ValueError):raise
        raise ValueError('No se pudo verificar el propietario del origen.') from exc
    await s.repo.pool.execute('INSERT INTO descarga_creator_owners VALUES($1,$2,$3) ON CONFLICT DO NOTHING',s.bot_id,source,owner)
    if owner in protected:raise ValueError(DENIED)

def scoped(kind):
    def decorate(fn):
        @wraps(fn)
        async def call(*args,**kwargs):
            value=args[1] if len(args)>1 else kwargs.get({'job':'job','message':'message','actor':'uid'}[kind])
            actor=value.get('actor') if kind=='job' else value.get('from',{}).get('id') if kind=='message' else value
            token=ACTOR.set(actor)
            try:return await fn(*args,**kwargs)
            finally:ACTOR.reset(token)
        return call
    return decorate

async def require_consent(s,actor,uid):
    await require_account(s,uid,actor)
    if actor==uid:return
    row=await s.repo.pool.fetchrow('SELECT c.*,a.generation AS current_generation,a.session IS NOT NULL AS connected FROM descarga_creator_consent c JOIN descarga_accounts a USING(uid) WHERE c.bot_id=$1 AND c.uid=$2',s.bot_id,uid)
    if not row or row['revoked'] or row['version']!=VERSION or not row['connected'] or row['generation']!=row['current_generation']:
        raise ValueError('Esta cuenta debe autorizar la revisión desde PROTECCIÓN A CREADORES antes de usar la búsqueda global.')


async def policy_tag(s,actor):
    import hashlib
    from .protected import sources,bots
    values=sorted(ids('cuentas.txt') | ids('admins.txt') | set(await registrations(s)))
    return hashlib.sha256((str(police(actor))+':'+','.join(map(str,values))+':'+str(sorted(sources()))+':'+str(sorted(bots()))).encode()).hexdigest()

async def validate_target(s,actor,uid,source):
    await require_account(s,uid,actor)
    if police(actor):return
    from .account_chat import connected
    async with connected(s,uid) as client:
        await require_source(s,uid,source,client=client,actor=actor)


async def visible_sources(s,actor,rows):
    actor=ACTOR.get() if ACTOR.get() is not None else actor
    if police(actor):return rows
    from .protected import sources,bots
    blocked=sources() | {int(v) for v in bots() if v.isdecimal()}
    rows=[r for r in rows if r['source'] not in blocked and r['source'] not in ids('admins.txt')]
    protected=ids('cuentas.txt') | ids('admins.txt') | set(await registrations(s))
    if not protected:return rows
    hidden=await s.repo.pool.fetch('SELECT DISTINCT source FROM descarga_creator_owners WHERE bot_id=$1 AND owner=ANY($2::bigint[])',s.bot_id,list(protected))
    hidden={r['source'] for r in hidden}
    known=await s.repo.pool.fetch('SELECT DISTINCT source FROM descarga_creator_owners WHERE bot_id=$1',s.bot_id)
    known={r['source'] for r in known}
    return [r for r in rows if r['source'] in known and r['source'] not in hidden and r['source'] not in protected]
