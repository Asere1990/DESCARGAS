"""Copia segura de GIF: conservar original animado en R2 y servir MP4 si es viable."""
import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path
from PIL import Image,ImageOps

log=logging.getLogger("web-galerias-gif")


def convert_gif_to_mp4(src,dst):
    """Conversión acotada: nunca ejecutar una transcodificación sin límite."""
    from imageio_ffmpeg import get_ffmpeg_exe
    cmd=[get_ffmpeg_exe(),"-hide_banner","-loglevel","error","-nostdin","-y",
         "-i",str(src),"-vf","fps=15,scale=trunc(iw/2)*2:trunc(ih/2)*2",
         "-c:v","libx264","-preset","veryfast","-crf","27",
         "-pix_fmt","yuv420p","-movflags","+faststart",str(dst)]
    subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                   check=True,timeout=120)
    if not Path(dst).is_file() or Path(dst).stat().st_size<=0:
        raise ValueError("GIF convertido vacío")


def copy_from_r2(storage,key,destination):
    """Reanudar la conversión sin descargar por segunda vez desde Telegram."""
    obj=storage._client().get_object(Bucket=storage.bucket,Key=storage.safe_key(key))
    body=obj["Body"]
    try:
        with open(destination,"wb") as output:
            for chunk in body.iter_chunks(chunk_size=256*1024):
                if chunk:output.write(chunk)
    finally:
        body.close()


async def backup_gif(g,folder,item,message,client,guard):
    """Mantener el GIF original en R2 y una copia MP4 reproducible en la web.

    Si ffmpeg no puede convertir ese GIF particular, la web reproduce el
    original animado como imagen, conservándolo en la categoría de videos.
    """
    await guard()
    base=f"folders/{folder}/{message.id}"
    source_key=base+"/source.gif"
    mp4_key=base+"/original.mp4"
    thumb_key=base+"/thumb.jpg"
    src_size=await g.storage.object_size(source_key)
    mp4_size=await g.storage.object_size(mp4_key)
    thumb_size=await g.storage.object_size(thumb_key)
    with tempfile.TemporaryDirectory(prefix="wg-gif-") as root:
        source=Path(root)/"source.gif"
        output=Path(root)/"original.mp4"
        thumb=Path(root)/"thumb.jpg"
        if not mp4_size:
            if src_size:
                await asyncio.to_thread(copy_from_r2,g.storage,source_key,source)
            else:
                name=await client.download_media(message,file=str(source))
                await guard()
                if not name or not Path(name).is_file() or Path(name).stat().st_size<=0:
                    raise ValueError("Telegram no entregó GIF")
                source=Path(name)
                await g.storage.upload(source,source_key,"image/gif")
                size=source.stat().st_size
                if await g.storage.object_size(source_key)!=size:
                    raise ValueError("No se verificó el original GIF en R2")
                src_size=size
        if not thumb_size and source.is_file():
            try:
                with Image.open(source) as image:
                    image.seek(0)
                    pic=ImageOps.exif_transpose(image).convert("RGB")
                    pic.thumbnail((640,640))
                    pic.save(thumb,"JPEG",quality=86,optimize=True)
            except (OSError,ValueError) as exc:
                log.info("GIF thumbnail skipped (%s)",type(exc).__name__)
            if thumb.is_file():
                await guard()
                await g.storage.upload(thumb,thumb_key,"image/jpeg")
                thumb_size=await g.storage.object_size(thumb_key)
                if thumb_size!=thumb.stat().st_size:
                    raise ValueError("No se verificó thumbnail GIF en R2")
        if not mp4_size and source.is_file():
            try:
                await asyncio.to_thread(convert_gif_to_mp4,source,output)
                await guard()
                await g.storage.upload(output,mp4_key,"video/mp4")
                mp4_size=await g.storage.object_size(mp4_key)
                if mp4_size!=output.stat().st_size:
                    raise ValueError("No se verificó video GIF en R2")
            except (subprocess.SubprocessError,OSError,ValueError) as exc:
                log.warning("GIF kept in original format (%s)",type(exc).__name__)
    if mp4_size:
        key=mp4_key;size=mp4_size;mime="video/mp4"
        filename=Path(item.get("filename") or "animacion.gif").stem+".mp4"
    else:
        key=source_key;size=src_size;mime="image/gif"
        filename=Path(item.get("filename") or "animacion.gif").stem+".gif"
    if not size:
        raise ValueError("No se pudo confirmar la copia del GIF")
    await guard()
    await g.db().execute("""UPDATE wg_media SET state='ready',file_key=$3,
       thumb_key=$4,size=$5,mime=$6,filename=$7,error='',source_checks=0
       WHERE folder=$1 AND message=$2 AND state IN ('preview','pending')""",
       folder,message.id,key,thumb_key if thumb_size else None,size,mime,filename)
