"""Account metadata and explicit saved-session checks for the independent panel."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timezone
import hashlib
import json
import time
from babel import Locale
import phonenumbers
from telethon import errors,functions,types
from aiohttp import web
from .creator_access import ACTOR,require_account,police
from .moderation import authorized

LOCALE=Locale.parse('es')
TERMINAL_SESSION_REASONS=frozenset(('SESSION_REVOKED','SESSION_EXPIRED',
    'AuthKeyUnregisteredError','AuthKeyInvalidError','AuthKeyDuplicatedError',
    'UserDeactivatedError','UserDeactivatedBanError','Sin sesión guardada',
    'La identidad no coincide con la cuenta'))

async def identity(client):
    # get_me() suppresses UnauthorizedError, losing SESSION_REVOKED vs expiry.
    # A direct read-only query retains Telegram's exact error classification.
    users=await client(functions.users.GetUsersRequest([types.InputUserSelf()]))
    return users[0] if users else None

def metadata(profile,directory,country=''):
    phone=str(profile.get('phone') or '')
    code=profile.get('phone_country') or country or ''
    if phone:
        try:
            parsed=phonenumbers.parse('+'+phone.lstrip('+'),None)
            code=phonenumbers.region_code_for_number(parsed) or code
            phone=phonenumbers.format_number(parsed,phonenumbers.PhoneNumberFormat.INTERNATIONAL)
        except phonenumbers.NumberParseException:pass
    return {'phone':phone,'country_code':code,'country':str(LOCALE.territories.get(code,code)) if code and code!='ZZ' else ''}

def tag(row):return hashlib.md5(bytes(row['session'])).hexdigest() if row and row['session'] else ''

def review_metadata(row,actor):
    """Report review permission separately from session validity; no Telegram call."""
    if row['uid']==actor or not row.get('review_revoked'):
        return {}
    reason=('Autorización de revisión no completada' if not row.get('review_accepted')
            else 'Autorización de revisión revocada')
    return {'review_state':'not_authorized','review_reason':reason}

async def record_owner_confirmation(service,uid):
    """Record only evidence from the owner's already successful identity check.
    Caller holds the existing account lock; never connects to Telegram here.
    """
    row=await service.repo.account(uid)
    if not row or not row['session']:return
    data={'state':'active','reason':'Identidad confirmada por Telegram',
          'checked':time.time(),'generation':row['generation'],'session_tag':tag(row)}
    await service.repo.set_setting(f'panel-account-status:{service.bot_id}:{uid}',json.dumps(data))

class AccountOverview:
    def __init__(self,panel):self.p=panel;self.s=panel.s;self.slots=asyncio.Semaphore(1)
    def key(self,uid):return f'panel-account-status:{self.s.bot_id}:{uid}'
    async def cached(self,uid,generation,session_tag):
        return self.cached_value(await self.s.repo.setting(self.key(uid)),generation,session_tag)
    @staticmethod
    def cached_value(raw,generation,session_tag,*,allow_stale=False):
        # The account list can read all cached states in one indexed SQL join.
        # Require the exact session fingerprint and generation; never trust stale states.
        try:data=json.loads(raw or '{}')
        except (ValueError,TypeError):return None
        if data.get('generation')!=generation or data.get('session_tag')!=session_tag:return None
        terminal=(data.get('state') in ('revoked','expired','invalid') and
                  data.get('reason') in TERMINAL_SESSION_REASONS)
        if not data.get('checked') or (not allow_stale and not terminal and time.time()-data['checked']>300):return None
        return {k:data[k] for k in ('state','reason','checked')}
    @asynccontextmanager
    async def connection(self,actor,uid):
        marker=ACTOR.set(actor)
        try:
            async with self.p.live.borrow(actor,uid) as live:
                if live:
                    yield live[0]
                    return
            # Reuse the saved key under the same exclusive lock as workers.
            # No login, sign-in, session save, erase or key export occurs here.
            async with self.s.repo.account_lock(uid,wait=False):
                await require_account(self.s,uid,actor)
                session=await self.s.get_session(uid)
                if not session:raise ValueError('Sin sesión guardada.')
                client=self.s.client(session)
                try:
                    await client.connect()
                    yield client
                finally:await client.disconnect()
        finally:ACTOR.reset(marker)
    async def review_denial(self,actor,uid):
        try:
            await require_account(self.s,uid,actor)
        except ValueError:
            # Never bypass the guard. An administrative review refusal is not
            # evidence that the owner's Telegram session is valid or invalid.
            if not police(actor):raise
            return {'review_state':'not_authorized',
                    'review_reason':'Revisión no autorizada; sin nueva comprobación técnica'}
        return None
    async def check(self,actor,uid):
        if not authorized(actor):raise web.HTTPForbidden()
        denial=await self.review_denial(actor,uid)
        if denial:return denial
        row=await self.s.repo.account(uid)
        if not row or not row['session']:return {'state':'invalid','reason':'Sin sesión guardada','checked':time.time()}
        fingerprint=tag(row);generation=row['generation']
        cached=await self.cached(uid,generation,fingerprint)
        if cached:return cached
        state='unknown';reason='Sin verificar';confirmed=False
        try:
            if self.slots.locked():return {'state':state,'reason':'Hay otra comprobación en curso. Reintenta.','checked':0}
            async with self.slots,asyncio.timeout(18),self.connection(actor,uid) as client:
                me=await identity(client)
                if me is None:reason='Telegram no confirmó la identidad de la sesión'
                elif me.id!=uid:state='invalid';reason='La identidad no coincide con la cuenta';confirmed=True
                else:state='active';reason='Identidad confirmada por Telegram';confirmed=True
        except errors.SessionRevokedError:state='revoked';reason='SESSION_REVOKED';confirmed=True
        except errors.SessionExpiredError:state='expired';reason='SESSION_EXPIRED';confirmed=True
        except (errors.AuthKeyUnregisteredError,errors.AuthKeyInvalidError,errors.AuthKeyDuplicatedError) as exc:state='invalid';reason=type(exc).__name__;confirmed=True
        except errors.FloodWaitError as exc:reason=f'Telegram pide esperar {exc.seconds} segundos'
        except (ValueError,TimeoutError):reason='Cuenta ocupada o comprobación temporalmente no disponible'
        except errors.RPCError as exc:
            name=type(exc).__name__
            if name in ('UserDeactivatedError','UserDeactivatedBanError'):
                state='invalid';reason=name;confirmed=True
            elif isinstance(exc,errors.UnauthorizedError):
                reason='Telegram no confirmó la sesión: '+name
            else:reason='Error temporal: '+name
        except (OSError,ConnectionError):reason='No se pudo conectar con Telegram'
        if not authorized(actor):raise web.HTTPForbidden()
        denial=await self.review_denial(actor,uid)
        if denial:return denial
        current=await self.s.repo.account(uid)
        if not current or current['generation']!=generation or tag(current)!=fingerprint:
            return {'state':'unknown','reason':'La sesión cambió; vuelve a comprobar','checked':0}
        data={'state':state,'reason':reason,'checked':time.time() if confirmed else 0}
        if confirmed:await self.s.repo.set_setting(self.key(uid),json.dumps({**data,'generation':generation,'session_tag':fingerprint}))
        return data
