"""File-id reuse without copying restricted-storage requests to ALMACEN or Dropbox."""
from contextvars import ContextVar
from .backup import backup_payload
from .media_cache import caption_entities

PRIVATE=ContextVar('storage_private',default=False)

def media_type(message):
    if message.photo:return 'photo'
    attrs=getattr(message.document,'attributes',[])
    names={type(a).__name__ for a in attrs}
    if 'DocumentAttributeAnimated' in names:return 'animation'
    if 'DocumentAttributeAudio' in names:
        return 'voice' if any(getattr(a,'voice',False) for a in attrs) else 'audio'
    if 'DocumentAttributeVideo' in names:return 'video'
    return 'document'

async def store_private(service,identity,uid,delivered):
    payload=backup_payload(service,identity,uid,delivered,[0]*len(delivered))
    for entry,message in zip(payload['items'],delivered):
        entry['type']=media_type(message)
        entry.pop('location',None);entry.pop('bot_message_id',None);entry.pop('message_id',None)
    await service.repo.private_ready(identity,service.bot_id,payload)

async def send_private(service,uid,payload,items):
    files=payload['items']
    if len(files)!=len(items):raise ValueError('El álbum guardado está incompleto.')
    await service.before_send(uid,len(items))
    entries=[]
    for stored,original in zip(files,items):
        kind=stored.get('type') or ('photo' if original.photo else media_type(original))
        entries.append({'type':kind,'media':stored['file_id'],'caption':original.message or '',
            'caption_entities':caption_entities(original.entities)})
    # Preserve order and Telegram's separate document/audio album constraints.
    i=0
    while i<len(entries):
        entry=entries[i];kind=entry['type']
        family=lambda t:'visual' if t in ('photo','video') else t
        end=i+1
        if kind in ('photo','video','audio','document'):
            while end<len(entries) and end-i<10 and family(entries[end]['type'])==family(kind):end+=1
        if end-i>1:
            result=await service.api('sendMediaGroup',{'chat_id':uid,'media':entries[i:end]})
            if len(result)!=end-i:raise ValueError('Entrega incompleta. Revisa lo recibido antes de repetir.')
        else:
            payload={'chat_id':uid,kind:entry['media'],'caption':entry['caption'],'caption_entities':entry['caption_entities']}
            await service.api('send'+kind.capitalize(),payload)
        i=end
    return len(items)
