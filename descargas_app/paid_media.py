from copy import copy
from telethon import types


def purchased_items(message):
    """Return full paid media supplied by this user's session, never previews."""
    if not isinstance(getattr(message, 'media', None), types.MessageMediaPaidMedia):
        return None
    entries = message.media.extended_media
    if not entries or any(
        not isinstance(entry, types.MessageExtendedMedia)
        or not isinstance(entry.media, (types.MessageMediaPhoto, types.MessageMediaDocument))
        or not (getattr(entry.media, 'photo', None) or getattr(entry.media, 'document', None))
        for entry in entries
    ):
        raise ValueError('La cuenta conectada todavía no tiene acceso al contenido completo. Comprueba que lo compraste con esa misma cuenta.')
    items = []
    for index, entry in enumerate(entries):
        item = copy(message)
        item.media = entry.media
        item._file = None
        if index:
            item.message = ''
            item.entities = []
        items.append(item)
    return items
