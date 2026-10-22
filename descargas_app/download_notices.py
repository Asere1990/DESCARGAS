"""Keep only the newest link-download confirmation in each administrator DM."""
import json
import logging

LOG=logging.getLogger(__name__)


async def latest_confirmation(service,actor,text):
    key=f'link-download-confirmation:{service.bot_id}:{actor}'
    async with service.repo.pool.acquire() as conn:
        await conn.execute('SELECT pg_advisory_lock(753,hashtext($1))',key)
        try:
            raw=await conn.fetchval('SELECT value FROM descarga_settings WHERE key=$1',key)
            state=json.loads(raw) if raw else {}
            previous=list(dict.fromkeys([*state.get('cleanup',[]),*([state['last']] if state.get('last') else [])]))
            # Send first: a failed confirmation never removes the preceding one.
            sent=await service.api('sendMessage',{'chat_id':actor,'text':text,'link_preview_options':{'is_disabled':True}})
            mid=sent['message_id']
            state={'last':mid,'cleanup':[old for old in previous if old!=mid]}
            async def save():
                await conn.execute('INSERT INTO descarga_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=excluded.value',key,json.dumps(state))
            await save()
            failed=[]
            for old in state['cleanup']:
                try:await service.api('deleteMessage',{'chat_id':actor,'message_id':old})
                except Exception:
                    failed.append(old)
                    LOG.warning('Previous link confirmation deletion pending')
            state['cleanup']=failed[-100:]
            await save()
        finally:
            await conn.execute('SELECT pg_advisory_unlock(753,hashtext($1))',key)
