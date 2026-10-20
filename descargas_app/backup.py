"""Durable backup handoff. File IDs refer to the delivering bot, never the user.

Telegram file-id v4 wire format: Pyrogram's public file_id and Photo parsers:
https://github.com/pyrogram/pyrogram/blob/master/pyrogram/file_id.py
https://github.com/pyrogram/pyrogram/blob/master/pyrogram/types/messages_and_media/photo.py
This implements the photo/document subset using Telethon's TL byte serializer.
"""
import asyncio
import base64
import hmac
import os
import struct
from telethon.tl.tlobject import TLObject
from telethon import types
from aiohttp import web


def file_id(message):
    photo=getattr(message,'photo',None)
    media=photo or message.document
    kind=2 if photo else 5
    if not photo:
        names={type(a).__name__ for a in media.attributes}
        if 'DocumentAttributeVideo' in names:kind=4
        if 'DocumentAttributeAnimated' in names:kind=10
        if 'DocumentAttributeAudio' in names:
            kind=3 if any(getattr(a,'voice',False) for a in media.attributes) else 9
        if 'DocumentAttributeSticker' in names:kind=8
    reference=media.file_reference or b''
    raw=struct.pack('<ii',kind | ((1<<25) if reference else 0),media.dc_id)
    if reference:raw+=TLObject.serialize_bytes(reference)
    raw+=struct.pack('<qq',media.id,media.access_hash)
    if photo:
        sizes=[s for s in photo.sizes if hasattr(s,'size') or hasattr(s,'sizes')]
        size=max(sizes,key=lambda s:getattr(s,'size',0) or max(getattr(s,'sizes',[0])))
        raw+=struct.pack('<qiiii',0,1,2,ord(size.type),0)
    elif not reference:raw+=struct.pack('<ii',30,4)
    raw+=bytes((30,4))
    encoded=bytearray();zeros=0
    for value in raw:
        if value==0:
            zeros+=1
            if zeros==255:encoded.extend((0,255));zeros=0
        else:
            if zeros:encoded.extend((0,zeros));zeros=0
            encoded.append(value)
    if zeros:encoded.extend((0,zeros))
    return base64.urlsafe_b64encode(encoded).decode().rstrip('=')


def backup_payload(service,identity,uid,delivered,ids):
    def location(m):
        media=m.photo or m.document
        thumb=''
        if m.photo:
            sizes=[s for s in media.sizes if hasattr(s,'size') or hasattr(s,'sizes')]
            thumb=max(sizes,key=lambda s:getattr(s,'size',0) or max(getattr(s,'sizes',[0]))).type
        return {'id':media.id,'access_hash':media.access_hash,'reference':base64.b64encode(media.file_reference or b'').decode(),'thumb':thumb}
    return {'key':identity,'bot_id':service.bot_id,'uid':uid,
        'items':[{'index':i,'file_id':file_id(m),'size':m.file.size or 0,
            'ext':m.file.ext or '.bin','message_id':ids[i],'bot_message_id':m.id,
            'kind':'photo' if m.photo else 'document','location':location(m)} for i,m in enumerate(delivered)]}


def mount_backup(app,service):
    stream_lock=asyncio.Lock()
    connect_lock=asyncio.Lock()
    async def get_bot():
        async with connect_lock:
            if getattr(service,'backup_bot',None):return service.backup_bot
            key=f'backup_bot_session:{service.bot_id}'
            stored=await service.repo.setting(key)
            session=service.repo.decode(stored.encode())['session'] if stored else ''
            bot=service.client(session)
            try:
                await bot.connect()
                if not await bot.is_user_authorized():await bot.sign_in(bot_token=service.token)
                me=await bot.get_me()
                if me.id!=service.bot_id:raise ValueError('Backup owner mismatch')
                await service.repo.set_setting(key,service.repo.encode({'session':bot.session.save()}).decode())
            except BaseException:
                await bot.disconnect();raise
            service.backup_bot=bot
            return bot
    async def handle(request):
        secret=os.environ.get('DESCARGAS_BACKUP_KEY','')
        if not secret or not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+secret):
            raise web.HTTPUnauthorized()
        if not service.ready:raise web.HTTPServiceUnavailable()
        data=await request.json()
        if not isinstance(data,dict):raise web.HTTPBadRequest()
        op=request.match_info['operation']
        if op=='account-status':
            # Backend-only diagnostics. Never return session bytes, phone numbers or login codes.
            uids=data.get('uids',[])
            if not isinstance(uids,list) or not 1<=len(uids)<=10 or any(type(uid) is not int or not 0<uid<2**53 for uid in uids):raise web.HTTPBadRequest()
            accounts=[]
            for uid in uids:
                row=await service.repo.account(uid)
                result={'uid':uid,'exists':bool(row),'stored_session':bool(row and row['session'])}
                if row:
                    result['generation']=row['generation']
                    result['decryptable']=False
                    if row['session']:
                        try:
                            result['decryptable']=bool(service.cipher.decrypt(bytes(row['session'])).decode())
                        except Exception:pass
                    if row['profile']:
                        try:result['connected_at']=service.repo.decode(row['profile']).get('updated_at')
                        except Exception:pass
                accounts.append(result)
            return web.json_response({'accounts':accounts},headers={'Cache-Control':'no-store'})
        if op=='claim':
            row=await service.repo.backup_claim(service.bot_id)
            result=None
            if row:
                result=service.repo.decode(row['payload'])
                result['items']=[{k:v for k,v in item.items() if k!='location'} for item in result['items']]
                result.update(lease=row['lease'])
            return web.json_response({'job':result},headers={'Cache-Control':'no-store'})
        if op=='file':
            key=data.get('key','');lease=data.get('lease','');index=data.get('index')
            if not isinstance(index,int) or isinstance(index,bool) or not 0<=index<10:raise web.HTTPBadRequest()
            row=await service.repo.backup_leased(key,lease,service.bot_id)
            if not row:raise web.HTTPConflict()
            payload=service.repo.decode(row['payload'])
            if index>=len(payload['items']):raise web.HTTPBadRequest()
            item=payload['items'][index];location=item['location']
            constructor=types.InputPhotoFileLocation if item['kind']=='photo' else types.InputDocumentFileLocation
            source=constructor(location['id'],location['access_hash'],base64.b64decode(location['reference']),location['thumb'])
            async with stream_lock:
                try:bot=await get_bot()
                except Exception:raise web.HTTPServiceUnavailable()
                # Re-fetch our own delivered message to refresh a reference after a long queue wait.
                # Never read another user's source session for backups.
                if item.get('bot_message_id'):
                    fresh=await bot.get_messages(None,ids=item['bot_message_id'])
                    media=(getattr(fresh,'photo',None) or getattr(fresh,'document',None)) if fresh else None
                    if media and media.id==location['id']:
                        source=constructor(media.id,media.access_hash,media.file_reference,location['thumb'])
                response=web.StreamResponse(headers={'Content-Type':'application/octet-stream','Cache-Control':'no-store'})
                if item['size']:response.content_length=item['size']
                iterator=bot.iter_download(source,request_size=512*1024,file_size=item['size'] or None)
                # Fetch first chunk before sending headers, so auth/media errors return an HTTP failure.
                try:
                    first=await anext(iterator)
                    await response.prepare(request)
                    await response.write(bytes(first));written=len(first)
                    async for chunk in iterator:
                        written+=len(chunk)
                        if item['size'] and written>item['size']:raise ValueError('Invalid backup size')
                        await response.write(bytes(chunk))
                    if item['size'] and written!=item['size']:raise ValueError('Incomplete backup')
                    await response.write_eof()
                    return response
                finally:await iterator.close()
        if op in ('heartbeat','done','retry'):
            key=data.get('key','');lease=data.get('lease','')
            if not isinstance(key,str) or len(key)!=64 or not isinstance(lease,str) or len(lease)!=32:raise web.HTTPBadRequest()
            ok=await service.repo.backup_update(key,lease,op)
            return web.json_response({'ok':ok},status=200 if ok else 409)
        raise web.HTTPNotFound()
    app.router.add_post('/internal/backups/{operation}',handle)
