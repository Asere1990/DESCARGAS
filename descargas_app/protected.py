"""Local source deny list, enforced before queueing and before each send."""
from pathlib import Path
import re
from contextvars import ContextVar

FILE=Path(__file__).resolve().parent.parent/'protegidos'
BOTS_FILE=FILE.with_name('bots_protegidos')
BOT_USERNAME=ContextVar('protected_bot_username',default=None)
MESSAGE='Este grupo esta protegido por Telegram, no se puede descargar contenido de ahi. Busca otro grupo, canal o chat privado'
BOT_MESSAGE='Este bot está protegido. No se puede descargar contenido de su chat privado.'
class ProtectedSource(ValueError):pass

def sources():
    try:
        values={token for line in FILE.read_text().splitlines() for token in line.split('#',1)[0].replace(',',' ').split()}
    except FileNotFoundError:return set()
    return {int(v) for v in values if v.startswith('-') and v[1:].isdigit()}

def require_source(source,uid=None):
    from .creator_access import ACTOR,police
    if ACTOR.get() is not None and police(ACTOR.get()):return
    from .moderation import authorized
    if authorized(uid):return
    if isinstance(source,int) and source in sources():raise ProtectedSource(MESSAGE)
    if isinstance(source,int) and source>0:
        blocked=bots()
        if str(source) in blocked or BOT_USERNAME.get() in blocked:raise ProtectedSource(BOT_MESSAGE)

def bots():
    try:
        return {str(int(token)) if token.isdecimal() else token[1:].lower() for line in BOTS_FILE.read_text().splitlines() for token in line.split('#',1)[0].replace(',',' ').split() if (token.isdecimal() and int(token)>0) or re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{3,31}',token)}
    except FileNotFoundError:return set()

def require_bot_alias(peer,uid=None):
    from .moderation import authorized
    if authorized(uid):return
    if isinstance(peer,str) and peer.lower().lstrip('@') in bots():raise ProtectedSource(BOT_MESSAGE)

def require_entity(entity,uid=None):
    BOT_USERNAME.set((getattr(entity,'username',None) or '').lower() if getattr(entity,'bot',False) else None)
    if getattr(entity,'bot',False):require_source(entity.id,uid)
