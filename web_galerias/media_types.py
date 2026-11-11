"""Clasificación de multimedia de Telegram para Web Galerías.

La clasificación no descarga mensajes ni altera el original. Los mensajes
sin foto/video/documento multimedia quedan fuera del índice.
"""
from pathlib import PurePosixPath
from telethon import types

IMAGE_EXT={".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",
           ".webp":"image/webp",".bmp":"image/bmp",".heic":"image/heic",
           ".heif":"image/heif",".avif":"image/avif"}
VIDEO_EXT={".mp4":"video/mp4",".mov":"video/quicktime",".webm":"video/webm",
           ".mkv":"video/x-matroska",".avi":"video/x-msvideo",
           ".m4v":"video/x-m4v",".3gp":"video/3gpp"}


def classify(message):
    """Devuelve (tipo web, MIME de origen, categoría) o (None,None,None).

    gif pertenece a la galería de videos, incluso si el original es GIF.
    photo_file/video_file separan documentos de medios nativos.
    """
    if getattr(message,"photo",None):
        return "photo","image/jpeg","photo"
    document=getattr(message,"document",None)
    if document is None:
        return None,None,None
    attrs=getattr(document,"attributes",None) or []
    if any(isinstance(a,types.DocumentAttributeSticker) for a in attrs):
        return None,None,None
    filename=next((str(a.file_name) for a in attrs
                   if getattr(a,"file_name",None)), "")
    ext=PurePosixPath(filename.lower()).suffix
    declared=str(getattr(document,"mime_type","") or "").split(";",1)[0].lower().strip()
    animated=any(isinstance(a,types.DocumentAttributeAnimated) for a in attrs)
    if declared=="image/gif" or ext==".gif":
        return "video","image/gif","gif"
    if animated and (declared.startswith("video/") or ext in VIDEO_EXT):
        return "video",declared if declared.startswith("video/") else VIDEO_EXT[ext],"gif"
    # SVG y otros formatos activos no son fotos seguras para publicar.
    safe_images=set(IMAGE_EXT.values())
    if declared in safe_images or ext in IMAGE_EXT:
        mime=declared if declared in safe_images else IMAGE_EXT[ext]
        return "photo",mime,"photo_file"
    if declared.startswith("video/") or ext in VIDEO_EXT:
        mime=declared if declared.startswith("video/") else VIDEO_EXT[ext]
        return "video",mime,"video_file" if filename else "video"
    return None,None,None
