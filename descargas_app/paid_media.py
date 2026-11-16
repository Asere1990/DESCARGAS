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


async def reveal_purchased(client, entity, messages, checked=None, scope=None):
    """Ask Telegram to refresh only owned paid posts, never to unlock unpaid media.

    Telegram returns UpdateMessageExtendedMedia only for purchases belonging to
    this logged-in account. Keep negative lookups on a 15-second cooldown.
    """
    import time
    from telethon import errors, functions, utils
    source=utils.get_peer_id(entity)
    now=time.monotonic()
    pending={}
    for message in messages:
        if not isinstance(getattr(message,'media',None),types.MessageMediaPaidMedia):
            continue
        try:
            if purchased_items(message):
                continue
        except ValueError:
            pass
        key=(scope,source,message.id)
        if checked is None or now-checked.get(key,-1e9)>=15:
            pending[message.id]=message
            if checked is not None:
                if len(checked)>=2048:checked.clear()
                checked[key]=now
    if not pending:
        return messages
    try:
        result=await client(functions.messages.GetExtendedMediaRequest(
            peer=entity,id=list(pending)))
    except errors.RPCError:
        return messages
    for update in getattr(result,'updates',[]) or []:
        if not isinstance(update,types.UpdateMessageExtendedMedia):
            continue
        if utils.get_peer_id(update.peer)!=source or update.msg_id not in pending:
            continue
        original=pending[update.msg_id]
        candidate=copy(original)
        candidate.media=copy(original.media)
        candidate.media.extended_media=update.extended_media
        try:
            owned=purchased_items(candidate)
        except ValueError:
            continue
        if owned:
            original.media=candidate.media
    return messages
