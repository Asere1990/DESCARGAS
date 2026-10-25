"""Explicit, scoped batch downloads for the independent account panel."""
import asyncio
import io
import re
import time
import zipfile
from aiohttp import web
from telethon import utils


class ZipSink:
    """Unseekable ZIP output, drained after each Telegram chunk."""
    def __init__(self):self.position=0;self.chunks=[]
    def write(self,data):
        self.chunks.append(bytes(data));self.position+=len(data);return len(data)
    def tell(self):return self.position
    def seek(self,*args):raise io.UnsupportedOperation('stream')
    def flush(self):pass
    async def drain(self,response):
        chunks,self.chunks=self.chunks,[]
        for chunk in chunks:
            if chunk:await response.write(chunk)


async def download(panel,request,actor,data):
    from .web_panel import integer,media_info,authorized,require_account,require_source,HEADERS
    uid=integer(data.get('account'));handle=data.get('peer','')
    raw=data.get('messages','').split(',')
    if not raw or len(raw)>500:raise ValueError('Selecciona entre 1 y 500 fotos o videos por lote.')
    ids=list(dict.fromkeys(integer(value) for value in raw))
    async with panel.account(actor,uid,timeout=3600) as (client,generation):
        entity=await panel.target(actor,uid,generation,client,handle)
        source=utils.get_peer_id(entity)
        async def check():
            if not authorized(actor):raise web.HTTPForbidden()
            await require_account(panel.s,uid,actor)
            await require_source(panel.s,uid,source,client=client,entity=entity,actor=actor)
        def validate(message):
            if not message or getattr(message,'peer_id',None) is None or utils.get_peer_id(message.peer_id)!=source:raise ValueError('Un archivo seleccionado ya no está disponible en este chat.')
            info=media_info(message)
            if not info or info['type'] not in ('photo','video'):raise ValueError('El lote solo admite fotos y videos disponibles para descargar.')
            return info
        rows=await client.get_messages(entity,ids=ids)
        by_id={m.id:m for m in rows if m and getattr(m,'id',None) in ids}
        for mid in ids:validate(by_id.get(mid))
        await check()
        response=web.StreamResponse(headers={**HEADERS,'Content-Type':'application/zip','Content-Disposition':'attachment; filename="fotos-videos.zip"'})
        await response.prepare(request)
        sink=ZipSink();iterator=None;checked=time.monotonic()
        try:
            with zipfile.ZipFile(sink,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
                for mid in ids:
                    await check()
                    message=await client.get_messages(entity,ids=mid)
                    info=validate(message)
                    filename=str(mid)+'_'+re.sub(r'[^a-zA-Z0-9._-]','_',info['name'])[:150]
                    if '.' not in filename:filename+='.jpg' if info['type']=='photo' else '.webm' if info['mime']=='video/webm' else '.mp4'
                    with archive.open(filename,'w',force_zip64=True) as output:
                        await sink.drain(response);written=0
                        iterator=client.iter_download(message.media,request_size=262144,chunk_size=262144)
                        try:
                            async for chunk in iterator:
                                if not authorized(actor):raise web.HTTPForbidden()
                                if time.monotonic()-checked>=5:await check();checked=time.monotonic()
                                output.write(chunk);written+=len(chunk);await sink.drain(response)
                        finally:
                            await iterator.close();iterator=None
                        if info['size'] and written!=info['size']:raise ValueError('Transferencia incompleta.')
                    await sink.drain(response)
            await check();await sink.drain(response);await response.write_eof()
        except asyncio.CancelledError:raise
        except Exception:
            # Never finish a valid-looking archive after a failed or revoked transfer.
            response.force_close()
            if request.transport:request.transport.close()
        finally:
            if iterator is not None:await iterator.close()
        return response
