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
from .creator_access import ACTOR,require_account,require_consent,police
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
    await record_confirmed_session(service,uid,row)

async def record_confirmed_session(service,uid,row):
    """Store identity evidence already obtained by an authorized operation."""
    if not row or not row['session']:return
    data={'state':'active','reason':'Identidad confirmada por Telegram',
          'checked':time.time(),'generation':row['generation'],'session_tag':tag(row)}
    await service.repo.set_setting(f'panel-account-status:{service.bot_id}:{uid}',json.dumps(data))

class HealthCheckBusy(ValueError):
    pass

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
        if data.get('state')=='unknown':
            if not allow_stale or not data.get('attempted'):return None
            return {k:data[k] for k in ('state','reason','checked','attempted','retry_until') if k in data}
        terminal=(data.get('state') in ('revoked','expired','invalid') and
                  data.get('reason') in TERMINAL_SESSION_REASONS)
        if not data.get('checked') or (not allow_stale and not terminal and time.time()-data['checked']>300):return None
        return {k:data[k] for k in ('state','reason','checked')}
    @asynccontextmanager
    async def health_lock(self,uid):
        # Same exclusive session lock as the workers, but never request a live
        # connection to yield and never wait for a busy account.
        async with self.s.repo.pool.acquire() as conn:
            if not await conn.fetchval('SELECT pg_try_advisory_lock($1)',uid):
                raise HealthCheckBusy('Cuenta ocupada; no se interrumpe su actividad')
            try:yield
            finally:await conn.execute('SELECT pg_advisory_unlock($1)',uid)
    @asynccontextmanager
    async def connection(self,actor,uid,*,health=False):
        marker=ACTOR.set(actor)
        try:
            if not health:
                async with self.p.live.borrow(actor,uid) as live:
                    if live:
                        yield live[0]
                        return
            # Reuse the saved key under the same exclusive lock as workers.
            # No login, sign-in, session save, erase or key export occurs here.
            lock=self.health_lock(uid) if health else self.s.repo.account_lock(uid,wait=False)
            async with lock:
                await require_account(self.s,uid,actor)
                if health:await self.require_health_permission(actor,uid)
                session=await self.s.get_session(uid)
                if not session:raise ValueError('Sin sesión guardada.')
                client=self.s.client(session)
                try:
                    await client.connect()
                    yield client
                finally:await client.disconnect()
        finally:ACTOR.reset(marker)
    async def require_health_permission(self,actor,uid):
        if not authorized(actor) or not police(actor):raise web.HTTPForbidden()
        await require_consent(self.s,actor,uid)
        if actor!=uid:
            row=await self.s.repo.creator_registration(self.s.bot_id,uid)
            if not row or not row.get('accepted') or row.get('revoked'):
                raise ValueError('Comprobación periódica no autorizada')
    async def review_denial(self,actor,uid,*,health=False):
        try:
            await require_account(self.s,uid,actor)
            if health:await self.require_health_permission(actor,uid)
        except ValueError:
            # Never bypass the guard. An administrative review refusal is not
            # evidence that the owner's Telegram session is valid or invalid.
            if not police(actor):raise
            return {'review_state':'not_authorized',
                    'review_reason':'Revisión no autorizada; sin nueva comprobación técnica'}
        return None
    async def check(self,actor,uid,*,fresh=False,health=False):
        if not authorized(actor):raise web.HTTPForbidden()
        denial=await self.review_denial(actor,uid,health=health)
        if denial:return denial
        row=await self.s.repo.account(uid)
        if not row or not row['session']:return {'state':'invalid','reason':'Sin sesión guardada','checked':time.time()}
        fingerprint=tag(row);generation=row['generation']
        cached=await self.cached(uid,generation,fingerprint)
        if cached and (not fresh or (cached.get('state') in ('revoked','expired','invalid') and cached.get('reason') in TERMINAL_SESSION_REASONS)):return cached
        if health:
            previous=self.cached_value(await self.s.repo.setting(self.key(uid)),generation,fingerprint,allow_stale=True)
            if previous and previous.get('retry_until',0)>time.time():return previous
        state='unknown';reason='Sin verificar';confirmed=False;retry_until=0
        try:
            if self.slots.locked():return {'state':state,'reason':'Hay otra comprobación en curso. Reintenta.','checked':0}
            async with self.slots,asyncio.timeout(18),self.connection(actor,uid,health=health) as client:
                me=await identity(client)
                if me is None:reason='Telegram no confirmó la identidad de la sesión'
                elif me.id!=uid:state='invalid';reason='La identidad no coincide con la cuenta';confirmed=True
                else:state='active';reason='Identidad confirmada por Telegram';confirmed=True
        except errors.SessionRevokedError:state='revoked';reason='SESSION_REVOKED';confirmed=True
        except errors.SessionExpiredError:state='expired';reason='SESSION_EXPIRED';confirmed=True
        except (errors.AuthKeyUnregisteredError,errors.AuthKeyInvalidError,errors.AuthKeyDuplicatedError) as exc:state='invalid';reason=type(exc).__name__;confirmed=True
        except errors.FloodWaitError as exc:
            reason=f'Telegram pide esperar {exc.seconds} segundos';retry_until=time.time()+exc.seconds
        except HealthCheckBusy:
            return {'state':'unknown','reason':'Cuenta ocupada; se conserva la última comprobación','checked':0}
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
        denial=await self.review_denial(actor,uid,health=health)
        if denial:return denial
        current=await self.s.repo.account(uid)
        if not current or current['generation']!=generation or tag(current)!=fingerprint:
            return {'state':'unknown','reason':'La sesión cambió; vuelve a comprobar','checked':0}
        data={'state':state,'reason':reason,'checked':time.time() if confirmed else 0}
        if not confirmed and health:
            data['attempted']=time.time()
            if retry_until:data['retry_until']=retry_until
        if confirmed or health:await self.s.repo.set_setting(self.key(uid),json.dumps({**data,'generation':generation,'session_tag':fingerprint}))
        return data


class AccountRefresh:
    """One paced, identity-only refresh. No chat/media reads or session mutations.

    Unattended checks require explicit current owner consent and a still-authorized
    admin who enabled the schedule. Existing account/session locks are respected;
    a refresh never requests that an active download or live listener yield.
    """
    PERIOD=1800
    GAP=5
    STALE=120
    def __init__(self,panel):
        self.p=panel;self.s=panel.s
        self.task=None;self.scheduler=None;self.guard=asyncio.Lock()
    def key(self,name):return f'panel-account-refresh:{self.s.bot_id}:{name}'
    async def read(self,name):
        try:
            data=json.loads(await self.s.repo.setting(self.key(name)) or '{}')
            return data if isinstance(data,dict) else {}
        except (ValueError,TypeError):return {}
    async def save(self,name,data):
        await self.s.repo.set_setting(self.key(name),json.dumps(data))
    async def valid(self,actor,version):
        if not isinstance(actor,int) or not authorized(actor) or not police(actor):return False
        return version==(await self.s.repo.setting(f'web-panel-version:{self.s.bot_id}:{actor}') or '0')
    def running(self,data):
        return data.get('state')=='running' and time.time()-data.get('heartbeat',0)<self.STALE
    async def status(self,actor):
        if not authorized(actor) or not police(actor):raise web.HTTPForbidden()
        data=await self.read('progress')
        if data.get('state')=='running' and not self.running(data):
            data={**data,'state':'interrupted'}
        config=await self.read('config')
        data['automatic']=bool(config and await self.valid(config.get('actor'),config.get('version')))
        data['period']=self.PERIOD
        return data
    async def launch(self,actor,version,mode):
        async with self.guard:
            if self.task and not self.task.done():return
            if self.running(await self.read('progress')):return
            if not await self.valid(actor,version):return
            self.task=asyncio.create_task(self.run(actor,version,mode))
    async def begin(self,actor,version):
        if not await self.valid(actor,version):raise web.HTTPForbidden()
        await self.save('config',{'actor':actor,'version':version})
        await self.launch(actor,version,'manual')
        return {'accepted':True}
    async def run(self,actor,version,mode):
        # The two-int advisory lock is isolated from the one-int session locks.
        # Other replicas cannot run the same bot's refresh at the same time.
        async with self.s.repo.pool.acquire() as lock:
            acquired=await lock.fetchval("SELECT pg_try_advisory_lock(749,hashtext($1))",self.key('job'))
            if not acquired:return
            progress={'state':'running','mode':mode,'started':time.time(),'heartbeat':time.time(),
                      'total':0,'processed':0,'confirmed':0,'inactive':0,'pending':0,'skipped':0}
            async def publish():
                progress['heartbeat']=time.time()
                await self.save('progress',progress)
            try:
                await publish()
                from .creator_access import VERSION
                rows=await self.s.repo.pool.fetch("""SELECT a.uid,a.session IS NOT NULL AS linked,
                    COALESCE(c.accepted>0 AND NOT c.revoked AND c.version=$2
                             AND c.generation=a.generation,FALSE) AS consented
                    FROM descarga_accounts a
                    LEFT JOIN descarga_creator_consent c ON c.uid=a.uid AND c.bot_id=$1
                    WHERE a.session IS NOT NULL OR a.profile IS NOT NULL
                    ORDER BY a.uid""",self.s.bot_id,VERSION)
                progress['total']=len(rows)
                await publish()
                for row in rows:
                    if not await self.valid(actor,version):
                        progress['state']='interrupted';break
                    probed=False
                    if not row['linked']:
                        progress['inactive']+=1
                    elif row['uid']!=actor and not row['consented']:
                        # Absence of explicit permission is not a session error.
                        progress['skipped']+=1
                    else:
                        try:
                            result=await self.p.overview.check(actor,row['uid'],fresh=True,health=True)
                            if result.get('review_state')=='not_authorized':
                                progress['skipped']+=1
                            elif result.get('state')=='active' and result.get('checked',0)>=progress['started']:
                                progress['confirmed']+=1
                            elif result.get('state') in ('revoked','expired','invalid') and result.get('reason') in TERMINAL_SESSION_REASONS:
                                progress['inactive']+=1
                            else:
                                progress['pending']+=1
                            probed=True
                        except asyncio.CancelledError:raise
                        except web.HTTPException:
                            progress['skipped']+=1
                        except Exception:
                            progress['pending']+=1;probed=True
                    progress['processed']+=1
                    if probed or progress['processed']%20==0 or progress['processed']==progress['total']:
                        await publish()
                    if probed:await asyncio.sleep(self.GAP)
                if progress['state']=='running':progress['state']='finished'
            except asyncio.CancelledError:
                progress['state']='interrupted';raise
            except Exception:
                progress['state']='error'
            finally:
                progress['finished']=time.time()
                try:await publish()
                finally:await lock.execute("SELECT pg_advisory_unlock(749,hashtext($1))",self.key('job'))
    async def loop(self):
        while True:
            try:
                if self.s.ready:
                    config=await self.read('config')
                    last=await self.read('progress')
                    if config and not self.running(last) and time.time()-last.get('finished',0)>=self.PERIOD:
                        await self.launch(config.get('actor'),config.get('version'),'automatic')
            except asyncio.CancelledError:raise
            except Exception:pass
            await asyncio.sleep(30)
    async def start(self,app):
        if not self.scheduler:self.scheduler=asyncio.create_task(self.loop())
    async def stop(self,app):
        tasks=[t for t in (self.scheduler,self.task) if t and not t.done()]
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        self.scheduler=None;self.task=None
