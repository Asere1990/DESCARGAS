"""Bounded album preparation, preserving input order and original media bytes."""
import asyncio
from pathlib import Path
import re
import shutil
from .progress import ACTIVE


async def prepare_album(source, items, directory, prepare, max_file, max_album):
    expected=sum(getattr(item.file,'size',0) or 0 for item in items)
    if shutil.disk_usage(directory).free < expected+512*1024*1024:
        raise ValueError('No hay espacio temporal suficiente. Inténtalo más tarde.')
    semaphore=asyncio.Semaphore(2)
    received=[0]*len(items)
    media=[None]*len(items)

    async def one(index,item):
        async with semaphore:
            ext=item.file.ext or '.bin'
            if not re.fullmatch(r'\.[A-Za-z0-9]{1,10}',ext):ext='.bin'
            target=Path(directory)/f'{index:02d}_{item.id}{ext}'
            async def progress(current,expected):
                received[index]=current
                if ACTIVE.get():ACTIVE.get().update(index,current,expected)
                if current>max_file or sum(received)>max_album:
                    raise ValueError('El archivo supera el límite de descarga.')
            result=await source.download_media(item,file=str(target),progress_callback=progress)
            if not result or not target.exists() or not target.stat().st_size:
                raise ValueError('El archivo no se descargó completo.')
            await progress(target.stat().st_size,0)
            # Item index follows input order even when two transfers run concurrently.
            from .progress import ITEM_INDEX
            marker=ITEM_INDEX.set(index)
            try:media[index]=await prepare(target,item)
            finally:ITEM_INDEX.reset(marker)

    try:
        async with asyncio.TaskGroup() as group:
            for index,item in enumerate(items):group.create_task(one(index,item))
    except ExceptionGroup as exc:
        # All sibling tasks have finished cancellation before temporary files are removed.
        def first(error):
            return first(error.exceptions[0]) if isinstance(error,BaseExceptionGroup) else error
        raise first(exc) from None
    return media
