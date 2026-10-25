"""Scoped custom emoji playback: TGS/Lottie, static images and video emoji."""
import asyncio
from collections import OrderedDict
import io
import json
import zlib
from telethon import functions,types
from PIL import Image

MAX_CACHE=24*1024*1024


def tgs_animation(data):
    decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
    raw=decoder.decompress(data,4*1024*1024+1)
    if len(raw)>4*1024*1024 or decoder.unconsumed_tail or not decoder.eof:raise ValueError('Animación no válida.')
    value=json.loads(raw)
    if not isinstance(value,dict) or not isinstance(value.get('layers'),list):raise ValueError('Animación no válida.')
    if any(isinstance(a,dict) and isinstance(a.get('p'),str) for a in value.get('assets',[])):raise ValueError('La animación contiene imágenes externas.')
    if value.get('fonts') or value.get('chars'):raise ValueError('La animación contiene fuentes externas.')
    return value


async def video_webp(data):
    import imageio_ffmpeg
    process=await asyncio.create_subprocess_exec(imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error','-c:v','libvpx-vp9','-i','pipe:0','-t','3','-an','-vf','scale=100:100:force_original_aspect_ratio=decrease,fps=30','-c:v','libwebp_anim','-q:v','80','-loop','0','-f','webp','pipe:1',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(10):out,error=await process.communicate(data)
    except BaseException:
        if process.returncode is None:process.kill()
        await process.wait();raise
    if process.returncode or len(out)>4*1024*1024 or not out.startswith(b'RIFF') or out[8:12]!=b'WEBP':raise ValueError('No se pudo preparar el emoji de video.')
    return out


class PremiumEmoji:
    def __init__(self,panel):self.p=panel;self.cache=OrderedDict();self.bytes=0;self.slots=asyncio.Semaphore(2)
    def put(self,document,item):
        if document in self.cache:self.bytes-=self.cache.pop(document)[2]
        size=len(item[1]);self.cache[document]=(item[0],item[1],size);self.bytes+=size
        while self.bytes>MAX_CACHE or len(self.cache)>256:
            _,old=self.cache.popitem(last=False);self.bytes-=old[2]
        return item
    async def asset(self,actor,uid,handle,mid,document):
        async with self.p.account(actor,uid) as (client,generation):
            entity=await self.p.target(actor,uid,generation,client,handle)
            message=await self.p.actions.known_message(client,entity,mid)
            if getattr(message,'ttl_period',None) or getattr(getattr(message,'media',None),'ttl_seconds',None):raise ValueError('Mensaje temporal no disponible.')
            allowed={e.document_id for e in getattr(message,'entities',[]) or [] if isinstance(e,types.MessageEntityCustomEmoji)}
            allowed.update(r.reaction.document_id for r in getattr(getattr(message,'reactions',None),'results',[]) or [] if isinstance(r.reaction,types.ReactionCustomEmoji))
            if document not in allowed:raise ValueError('El emoji no pertenece a este mensaje.')
            cached=self.cache.get(document)
            if cached:self.cache.move_to_end(document);return cached[:2]
            async with self.slots:
                cached=self.cache.get(document)
                if cached:return cached[:2]
                docs=await client(functions.messages.GetCustomEmojiDocumentsRequest([document]))
                doc=next((d for d in docs if isinstance(d,types.Document) and d.id==document),None)
                if not doc or not any(isinstance(a,types.DocumentAttributeCustomEmoji) for a in doc.attributes):raise ValueError('Emoji no disponible.')
                if doc.size>1024*1024:raise ValueError('Archivo de emoji no válido.')
                data=await client.download_media(doc,file=bytes)
                if not data or len(data)>1024*1024:raise ValueError('Archivo de emoji no válido.')
                await self.p.features.check(actor,uid,client,entity)
                if doc.mime_type=='application/x-tgsticker':return self.put(document,('application/json',json.dumps({'format':'lottie','animation':tgs_animation(data)},separators=(',',':')).encode()))
                if doc.mime_type=='video/webm':return self.put(document,('image/webp',await video_webp(data)))
                if doc.mime_type in ('image/webp','image/png'):
                    with Image.open(io.BytesIO(data)) as image:
                        if image.width>512 or image.height>512:raise ValueError('Tamaño de emoji no válido.')
                    return self.put(document,(doc.mime_type,data))
                raise ValueError('Formato de emoji no disponible.')
