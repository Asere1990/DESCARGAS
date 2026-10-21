"""Bot-owned reusable file IDs, independent of ALMACEN messages and configuration."""
from .media_cache import cache_key,caption_entities
from .backup import file_id
from .private_cache import media_type


def identity(bot_id,key,items):
    return cache_key(bot_id,0,key,items)


def payload(service,key,items,delivered=None,legacy=None):
    if delivered is not None and len(delivered)!=len(items):
        raise ValueError('El álbum no se guardó completo; requiere revisión.')
    if legacy is not None and (legacy.get('bot_id')!=service.bot_id or len(legacy.get('items',[]))!=len(items)):
        raise ValueError('El respaldo no corresponde al bot o al álbum solicitado.')
    entries=[]
    for index,original in enumerate(items):
        media=delivered[index] if delivered is not None else original
        identifier=legacy['items'][index]['file_id'] if legacy is not None else file_id(media)
        entries.append({'index':index,'file_id':identifier,'type':media_type(media),
            'caption':original.message or '', 'caption_entities':caption_entities(original.entities)})
    return {'key':identity(service.bot_id,key,items),'bot_id':service.bot_id,
        'source':key[0],'album':key[1],'items':entries}


def valid(saved,bot_id,count):
    return bool(isinstance(saved,dict) and saved.get('bot_id')==bot_id
        and len(saved.get('items',[]))==count
        and all(isinstance(item.get('file_id'),str) and item['file_id'] for item in saved['items']))


async def find(service,key,items,legacy_identity):
    """Read stable IDs first; promote already-existing normal-download backups."""
    stable=identity(service.bot_id,key,items)
    saved=await service.repo.private_cache(stable)
    if valid(saved,service.bot_id,len(items)):return saved
    legacy=await service.repo.cached_file_payload(legacy_identity,service.bot_id)
    if valid(legacy,service.bot_id,len(items)):
        saved=payload(service,key,items,legacy=legacy)
        await service.repo.private_ready(stable,service.bot_id,saved)
        return saved


async def capture(service,key,items,ids):
    """Lazy migration for older exports: fetch metadata only, never file bytes."""
    messages=await service.bot.get_messages(service.almacen,ids=ids)
    if len(messages)!=len(items) or any(not m or not (m.photo or m.document) for m in messages):
        return None
    saved=payload(service,key,items,delivered=messages)
    await service.repo.private_ready(saved['key'],service.bot_id,saved)
    return saved
