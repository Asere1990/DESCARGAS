"""Read-only purchased media, scoped to the panel's existing account/source policy."""
from telethon import types, functions, utils
from .creator_access import require_account, require_source
from .protected import require_entity


def bought(transaction):
    amount = getattr(transaction, 'amount', None)
    value = getattr(amount, 'amount', getattr(transaction, 'stars', 0))
    return (value < 0 and not any(getattr(transaction, flag, False)
            for flag in ('refund', 'pending', 'failed'))
            and isinstance(getattr(transaction, 'peer', None), types.StarsTransactionPeer)
            and bool(getattr(transaction, 'extended_media', None)))


def media_message(media):
    # Only complete Telegram media, never paid previews or expiring attachments.
    if not isinstance(media, (types.MessageMediaPhoto, types.MessageMediaDocument)):
        return None
    if getattr(media, 'ttl_seconds', None):
        return None
    return types.Message(id=1, peer_id=None, media=media, message='')


def message_link(entity, mid):
    if not isinstance(mid, int) or mid <= 0:
        return None
    if isinstance(entity, types.Channel):
        return f'https://t.me/c/{entity.id}/{mid}'
    # Private chats do not have a shareable https message link.
    return None


async def listing(panel, actor, uid, cursor=''):
    from .web_panel import name, peer_data, media_info
    async with panel.account(actor, uid) as (client, generation):
        offset = ''
        scope = dict(actor=actor, uid=uid, generation=generation)
        if cursor:
            data = panel.unpack(cursor, 'purchases-page')
            if any(data.get(k) != v for k, v in scope.items()):
                raise ValueError('Abre Comprados de nuevo; la cuenta cambió.')
            offset = data['offset']
        result = await client(functions.payments.GetStarsTransactionsRequest(
            peer=types.InputPeerSelf(), offset=offset, limit=50, outbound=True))
        entities = {utils.get_peer_id(e): e for e in [*result.users, *result.chats]}
        items = []
        for tx in result.history or []:
            if not bought(tx):
                continue
            source = utils.get_peer_id(tx.peer.peer)
            entity = entities.get(source)
            if entity is None:
                continue
            try:
                require_entity(entity, uid)
                await require_source(panel.s, uid, source, client=client, entity=entity, actor=actor)
                data = peer_data(entity, uid)
            except ValueError:
                continue
            media = []
            for index, value in enumerate(tx.extended_media):
                message = media_message(value)
                info = media_info(message) if message else None
                if info and info['type'] in ('photo', 'video'):
                    media.append(dict(index=index, **info))
            if not media:
                continue
            handle = panel.pack('peer', **scope, **data)
            stamp = tx.date.timestamp() if hasattr(tx.date, 'timestamp') else tx.date
            items.append({'id':tx.id, 'date':stamp, 'media':media,
                'title':str(getattr(tx, 'title', '') or 'Contenido comprado')[:200],
                'message':getattr(tx, 'msg_id', None),
                'link':message_link(entity, getattr(tx, 'msg_id', None)),
                'token':panel.pack('purchase', **scope, source=source, transaction=tx.id),
                'chat':{'id':str(source), 'handle':handle, 'name':name(entity),
                    'type':'private' if isinstance(entity, types.User) else 'channel' if getattr(entity, 'broadcast', False) else 'group',
                    'bot':bool(getattr(entity, 'bot', False))}})
        await require_account(panel.s, uid, actor)
        next_offset = getattr(result, 'next_offset', None)
        return {'items':items, 'next':panel.pack('purchases-page', **scope, offset=next_offset)
                if next_offset and next_offset != offset else None}


async def resolve(panel, actor, uid, generation, client, entity, token, index):
    data = panel.unpack(token, 'purchase')
    scope = dict(actor=actor, uid=uid, generation=generation, source=utils.get_peer_id(entity))
    if any(data.get(k) != v for k, v in scope.items()):
        raise ValueError('La compra no corresponde a esta cuenta o conversación.')
    result = await client(functions.payments.GetStarsTransactionsByIDRequest(
        peer=types.InputPeerSelf(), id=[types.InputStarsTransaction(id=data['transaction'])]))
    tx = next((t for t in result.history or [] if t.id == data['transaction'] and bought(t)
               and utils.get_peer_id(t.peer.peer) == scope['source']), None)
    if tx is None or not 0 <= index < len(tx.extended_media):
        raise ValueError('Esta compra ya no está disponible para esta cuenta.')
    message = media_message(tx.extended_media[index])
    if message is None:
        raise ValueError('Contenido comprado no disponible.')
    await require_account(panel.s, uid, actor)
    await require_source(panel.s, uid, scope['source'], client=client, entity=entity, actor=actor)
    return message
