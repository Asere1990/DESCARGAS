"""Upload original bytes with explicit Telegram media metadata."""
import copy
import io
from pathlib import Path
from telethon import types
from PIL import Image, ImageOps


def jpeg_thumbnail(data):
    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        image.thumbnail((320,320))
        for quality in (80,65,45,25):
            stream=io.BytesIO()
            image.save(stream,format='JPEG',quality=quality)
            if stream.tell() < 20000:
                stream.seek(0);stream.name='thumb.jpg'
                return stream
    return None


async def prepare_media(client, path, original):
    path=Path(path)
    expected=getattr(getattr(original,'file',None),'size',None)
    if expected and path.stat().st_size != expected:
        raise ValueError('El archivo descargado no coincide con el tamaño original; no se envió.')
    from .progress import ACTIVE,ITEM_INDEX
    observer=ACTIVE.get();index=ITEM_INDEX.get()
    async def progress(current,total):
        if observer:observer.update(index,current,total,upload=True)
    if observer:uploaded=await client.upload_file(str(path),progress_callback=progress)
    else:uploaded=await client.upload_file(str(path))
    document=getattr(original,'document',None)
    if not document:
        if not original.photo:
            raise ValueError('El mensaje no contiene multimedia compatible.')
        return types.InputMediaUploadedPhoto(file=uploaded)
    attributes=copy.deepcopy(document.attributes)
    thumb=None
    if getattr(document,'thumbs',None):
        # Only thumbnail conversion failures are optional; Telegram RPC failures propagate.
        data=await client.download_media(original,file=bytes,thumb=-1)
        if data:
            try:
                stream=jpeg_thumbnail(data)
            except (OSError,ValueError):
                stream=None
            if stream:
                thumb=await client.upload_file(stream)
    return types.InputMediaUploadedDocument(file=uploaded,mime_type=document.mime_type,
        attributes=attributes,thumb=thumb,
        nosound_video=True if document.mime_type.startswith('video/') else None)
