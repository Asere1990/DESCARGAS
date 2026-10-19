import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(raw, token, now=None):
    if not raw or len(raw)>8192 or not token:
        raise ValueError('Abre la Mini App desde @Descargas_Robot.')
    pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True)
    if len({key for key,_ in pairs}) != len(pairs):
        raise ValueError('Autorización inválida.')
    values=dict(pairs)
    supplied=values.pop('hash','')
    secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    check='\n'.join(f'{key}={value}' for key,value in sorted(values.items()))
    expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied,expected):
        raise ValueError('Autorización inválida.')
    age=(time.time() if now is None else now)-int(values.get('auth_date','0'))
    if age < -30 or age > 3600:
        raise ValueError('Cierra y vuelve a abrir la Mini App.')
    user=json.loads(values['user'])
    uid=user.get('id')
    if type(uid) is not int or uid<=0:
        raise ValueError('Usuario inválido.')
    return uid
