"""Reuse one ALMACEN copy only after checking the requester's source access."""
from contextvars import ContextVar
import hashlib
import json
import logging
from .moderation import SOURCE,ensure_allowed
from .service import archive_attribution,apply_archive_caption,MAX_FILE,MAX_ALBUM

LOG=logging.getLogger(__name__)
CURRENT_MEDIA=ContextVar('descargas_media_cache',default=None)
CACHE_ONLY=ContextVar('descargas_cache_only',default=False)

class CacheMiss(Exception):
    pass


def cache_key(bot_id,almacen,key,items):
    # A changed album is a new version. Different links to the same album share a key.
    fingerprint=[('photo' if m.photo else 'document',(m.photo or m.document).id,m.file.size) for m in items]
    return hashlib.sha256(json.dumps([bot_id,almacen,key,fingerprint],separators=(',',':')).encode()).hexdigest()


def caption_entities(entities):
    mapping={'Mention':'mention','Hashtag':'hashtag','BotCommand':'bot_command','Url':'url',
        'Email':'email','Bold':'bold','Italic':'italic','Code':'code','Pre':'pre','TextUrl':'text_link',
        'Cashtag':'cashtag','Phone':'phone_number','Underline':'underline','Strike':'strikethrough',
        'Spoiler':'spoiler','Blockquote':'blockquote','CustomEmoji':'custom_emoji'}
    result=[]
    for e in entities or []:
        kind=type(e).__name__.removeprefix('MessageEntity')
        item={'offset':e.offset,'length':e.length}
        if kind=='MentionName':
            item.update(type='text_mention',user={'id':e.user_id,'is_bot':False,'first_name':'Usuario'})
        elif kind in mapping:
            item['type']=mapping[kind]
            if kind=='TextUrl':item['url']=e.url
            if kind=='Pre':item['language']=e.language
            if kind=='CustomEmoji':item['custom_emoji_id']=str(e.document_id)
            if kind=='Blockquote' and getattr(e,'collapsed',False):item['type']='expandable_blockquote'
        else:continue
        result.append(item)
    return result


async def deliver_cached(service,uid,client,link,seen):
    # This fetch uses this requesting user's session on EVERY request, including hits.
    # Paid previews and inaccessible messages are rejected before any cache lookup.
    key,items=await service.source_items(client,link)
    await ensure_allowed(service,uid,key[0])
    SOURCE.set(key[0])
    if key in seen:return 0
    if sum(m.file.size or 0 for m in items)>MAX_ALBUM or any((m.file.size or 0)>MAX_FILE for m in items):
        raise ValueError('El archivo supera 500 MB o el álbum supera 1 GB.')
    identity=cache_key(service.bot_id,service.almacen,key,items)
    from .private_cache import PRIVATE,send_private
    if (await service.repo.policy(service.bot_id,uid,key[0]))['locked']:
        async def private_hit(payload):
            result=await send_private(service,uid,payload,items)
            await service.repo.remember_media_route(service.bot_id,link,identity)
            seen.add(key)
            return result
        saved=await service.repo.private_cache(identity)
        if not saved:
            saved=await service.repo.cached_file_payload(identity,service.bot_id)
            if saved:await service.repo.private_ready(identity,service.bot_id,saved)
        if saved:return await private_hit(saved)
        if CACHE_ONLY.get():raise CacheMiss()
        async with service.repo.media_lock('private:'+identity):
            saved=await service.repo.private_cache(identity)
            if saved:return await private_hit(saved)
            marker=CURRENT_MEDIA.set(identity);privacy=PRIVATE.set(True)
            try:
                result=await service.deliver_items(uid,client,key,items,seen)
                await service.repo.remember_media_route(service.bot_id,link,identity)
                return result
            finally:PRIVATE.reset(privacy);CURRENT_MEDIA.reset(marker)
    async def send_saved(row):
        await service.repo.remember_media_route(service.bot_id,link,identity)
        saved=service.repo.decode(row['payload'])
        ids=saved['ids']
        await service.repo.bind_cached_origin(service.bot_id,service.almacen,ids,key[0],identity)
        if len(ids)!=len(items):raise ValueError('El álbum guardado está incompleto y necesita revisión.')
        await service.before_send(uid,len(items))
        copied=await service.api('copyMessages',{'chat_id':uid,'from_chat_id':service.almacen,
            'message_ids':ids,'remove_caption':True})
        if len(copied)!=len(items):
            raise ValueError('El álbum guardado no se entregó completo. Revisa lo recibido antes de repetir.')
        # Never expose the first requester's ALMACEN identity in another user's DM.
        for message,item in zip(copied,items):
            if item.message:
                await service.api('editMessageCaption',{'chat_id':uid,'message_id':message['message_id'],
                    'caption':item.message,'caption_entities':caption_entities(item.entities)})
        seen.add(key)
        LOG.warning('Media cache hit items=%s',len(items))
        return len(items)
    if CACHE_ONLY.get():
        row=await service.repo.media_cache(identity)
        if not row or row['state']!='ready':raise CacheMiss()
        return await send_saved(row)
    async with service.repo.media_lock(identity):
        row=await service.repo.media_cache(identity)
        if row:
            if row['state']!='ready':
                raise ValueError('El contenido guardado necesita revisión. No se repetirá la copia al almacén.')
            return await send_saved(row)
        marker=CURRENT_MEDIA.set(identity)
        try:
            result=await service.deliver_items(uid,client,key,items,seen)
            stored=await service.repo.media_cache(identity)
            if stored and stored['state']=='ready':await service.repo.remember_media_route(service.bot_id,link,identity)
            return result
        finally:CURRENT_MEDIA.reset(marker)


async def archive_once(service,identity,uid,name,delivered):
    # Recorded before copying: a crash or ambiguous network result never triggers
    # another automatic ALMACEN copy. Known IDs become reusable immediately.
    await service.repo.media_publishing(identity)
    await service.repo.pace('archive-group',3.1*len(delivered))
    copied=await service.api('copyMessages',{'chat_id':service.almacen,'from_chat_id':uid,
        'message_ids':[m.id for m in delivered],'remove_caption':False})
    if len(copied)!=len(delivered):raise ValueError('El álbum no se guardó completo en el almacén.')
    ids=[m['message_id'] for m in copied]
    await service.repo.register_origins(service.bot_id,service.almacen,ids,uid,SOURCE.get())
    from .backup import backup_payload
    backup=backup_payload(service,identity,uid,delivered,ids)
    await service.repo.media_ready(identity,{'ids':ids},backup=backup)
    attribution=await archive_attribution(service,uid,name)
    attribution['caption']+=f'\nGrupo de origen: {SOURCE.get()}'
    await apply_archive_caption(service,ids[0],attribution,delivered[0])
    LOG.warning('Media cache stored items=%s',len(ids))
