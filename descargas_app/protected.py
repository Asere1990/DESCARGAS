"""Local source deny list, enforced before queueing and before each send."""
from pathlib import Path

FILE=Path(__file__).resolve().parent.parent/'protegidos'
MESSAGE='Este grupo esta protegido por Telegram, no se puede descargar contenido de ahi. Busca otro grupo, canal o chat privado'
class ProtectedSource(ValueError):pass

def sources():
    try:
        values={token for line in FILE.read_text().splitlines() for token in line.split('#',1)[0].replace(',',' ').split()}
    except FileNotFoundError:return set()
    return {int(v) for v in values if v.startswith('-') and v[1:].isdigit()}

def require_source(source):
    if isinstance(source,int) and source in sources():raise ProtectedSource(MESSAGE)
