"""Export inventory and explicit exact-caption exclusions, separate from /lock ID."""
import hashlib
import re
import unicodedata

SCHEMA='''
CREATE TABLE IF NOT EXISTS descarga_export_files(
 bot_id BIGINT NOT NULL,almacen BIGINT NOT NULL,source BIGINT NOT NULL,
 kind TEXT NOT NULL,file_id BIGINT NOT NULL,
 PRIMARY KEY(bot_id,almacen,source,kind,file_id));
CREATE TABLE IF NOT EXISTS descarga_export_caption_refs(
 bot_id BIGINT NOT NULL,almacen BIGINT NOT NULL,message_id BIGINT NOT NULL,
 caption_hash TEXT NOT NULL,PRIMARY KEY(bot_id,almacen,message_id));
CREATE TABLE IF NOT EXISTS descarga_export_caption_locks(
 bot_id BIGINT NOT NULL,caption_hash TEXT NOT NULL,actor BIGINT NOT NULL,
 PRIMARY KEY(bot_id,caption_hash));
CREATE TABLE IF NOT EXISTS descarga_export_pending(
 bot_id BIGINT NOT NULL,uid BIGINT NOT NULL,source BIGINT NOT NULL,message_id BIGINT NOT NULL,
 error TEXT NOT NULL,PRIMARY KEY(bot_id,uid,source,message_id));
'''


def caption_hash(text):
    normalized=' '.join(unicodedata.normalize('NFC',text or '').split())
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else None


def album_hash(items):
    return next((value for m in items if (value:=caption_hash(getattr(m,'message',None)))),None)


def files(items):
    return list(dict.fromkeys(('photo' if m.photo else 'document',(m.photo or m.document).id) for m in items))


async def excluded(service,items):
    digest=album_hash(items)
    return bool(digest and await service.repo.pool.fetchval('SELECT 1 FROM descarga_export_caption_locks WHERE bot_id=$1 AND caption_hash=$2',service.bot_id,digest))


async def stored(service,source,items):
    # Source media IDs identify actual files, independently of message order or repost ID.
    for kind,fid in files(items):
        if not await service.repo.pool.fetchval('SELECT 1 FROM descarga_export_files WHERE bot_id=$1 AND almacen=$2 AND source=$3 AND kind=$4 AND file_id=$5',service.bot_id,service.almacen,source,kind,fid):return False
    return True


async def remember(service,source,items,ids):
    await service.repo.pool.executemany('INSERT INTO descarga_export_files VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING',[(service.bot_id,service.almacen,source,kind,fid) for kind,fid in files(items)])
    digest=album_hash(items)
    if digest and ids:
        await service.repo.pool.executemany('INSERT INTO descarga_export_caption_refs VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING',[(service.bot_id,service.almacen,mid,digest) for mid in ids])


async def resolve_pending(service,job,mid):
    await service.repo.pool.execute('DELETE FROM descarga_export_pending WHERE bot_id=$1 AND uid=$2 AND source=$3 AND message_id=$4',job['bot_id'],job['uid'],job['source'],mid)


async def reply_lock(service,message,operation,actor):
    """Only trusted ALMACEN media may create caption rules; never trust attribution IDs."""
    original=message.get('reply_to_message',{})
    chat=message.get('chat',{})
    async def reply(text):
        await service.api('sendMessage',{'chat_id':chat['id'],'text':text,'reply_parameters':{'message_id':message['message_id']}})
    if chat.get('id')!=service.almacen or not original.get('message_id'):
        await reply('Responde a un archivo o álbum en ALMACEN con /lock o /unlock. /lock ID conserva su función habitual.')
        return
    mid=original['message_id']
    origin=await service.repo.archive_origin(service.bot_id,service.almacen,mid)
    if not origin:
        await reply('Este mensaje no tiene procedencia registrada. Responde al archivo o álbum original del ALMACEN.')
        return
    digest=await service.repo.pool.fetchval('SELECT caption_hash FROM descarga_export_caption_refs WHERE bot_id=$1 AND almacen=$2 AND message_id=$3',service.bot_id,service.almacen,mid)
    if not digest:
        # Existing exports predate caption refs. Strip only our complete trusted suffix.
        text=original.get('caption','')
        suffix=rf'(?:^|\n\n)Usuario: [^\n]*\nTelegram ID: {origin["uid"]}(?:\nGrupo de origen: [^\n]*(?:\nID de origen: -?\d+)?)?$'
        match=re.search(suffix,text)
        if match:text=text[:match.start()]
        digest=caption_hash(text)
    if not digest:
        await reply('No hay caption original para bloquear. Responde al elemento del álbum que contiene el caption; no se bloquearán todos los archivos sin texto.')
        return
    if operation=='lock':
        await service.repo.pool.execute('INSERT INTO descarga_export_caption_locks VALUES($1,$2,$3) ON CONFLICT(bot_id,caption_hash) DO UPDATE SET actor=excluded.actor',service.bot_id,digest,actor)
        await reply('🔒 Caption excluido de las descargas /descargar de este bot. Se omitirán los contenidos que repitan ese texto original, aunque cambien de archivo o grupo. No se borra lo ya guardado.')
    else:
        await service.repo.pool.execute('DELETE FROM descarga_export_caption_locks WHERE bot_id=$1 AND caption_hash=$2',service.bot_id,digest)
        await reply('🔓 Exclusión por caption retirada. La próxima orden /descargar volverá a revisar todo el historial.')
