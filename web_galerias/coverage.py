"""Media-count checks from Telegram, scoped to the connected account's visibility.

These are inexpensive server-side counters: no chat-message content,
media downloads, session mutation or access bypass.
"""
from telethon import errors, functions, types


async def get_media_totals(client,entity):
    """Return photos,videos,method,inexact; use native chat counters when possible."""
    photos_filter=types.InputMessagesFilterPhotos
    videos_filter=types.InputMessagesFilterVideo
    # This is the endpoint used by Telegram clients to count gallery media.
    if callable(getattr(client,"get_input_entity",None)) and callable(client):
        try:
            peer=await client.get_input_entity(entity)
            result=await client(functions.messages.GetSearchCountersRequest(
                peer=peer,filters=[photos_filter(),videos_filter()]))
            totals={}
            imprecise=False
            for row in result:
                if isinstance(getattr(row,"filter",None),photos_filter):
                    totals["photos"]=getattr(row,"count",None)
                    imprecise=imprecise or bool(getattr(row,"inexact",False))
                elif isinstance(getattr(row,"filter",None),videos_filter):
                    totals["videos"]=getattr(row,"count",None)
                    imprecise=imprecise or bool(getattr(row,"inexact",False))
            p,v=totals.get("photos"),totals.get("videos")
            if type(p) is int and type(v) is int and p>=0 and v>=0:
                return p,v,"search_counters",imprecise
        except errors.FloodWaitError:
            raise
        except (errors.RPCError,ValueError,TypeError,AttributeError):
            pass  # Retry the documented search-total method below.
    photos=await client.get_messages(entity,0,filter=photos_filter)
    videos=await client.get_messages(entity,0,filter=videos_filter)
    p,v=getattr(photos,"total",None),getattr(videos,"total",None)
    if type(p) is not int or type(v) is not int or p<0 or v<0:
        raise ValueError("Telegram no devolvió totales de multimedia válidos")
    return p,v,"filtered_search",False
