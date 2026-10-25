"""Telegram-native reading and explicit messaging features for the separate panel."""
import base64
from datetime import datetime,timezone
from aiohttp import web
from telethon import types,functions,utils,errors
from .creator_access import require_source,require_consent,require_account
from .moderation import authorized
from .protected import require_entity


def rich_entities(message):
    result=[]
    for entity in getattr(message,'entities',None) or []:
        kind=type(entity).__name__.removeprefix('MessageEntity')
        if kind not in ('Bold','Italic','Underline','Strike','Code','Pre','Spoiler','Blockquote','TextUrl','Url','Mention','Email','CustomEmoji'):continue
        item={'type':kind,'offset':entity.offset,'length':entity.length}
        if kind=='TextUrl':item['url']=entity.url
        if kind=='CustomEmoji':item['document']=str(entity.document_id)
        result.append(item)
    return result


def extra_message(message):
    reactions=[]
    for item in getattr(getattr(message,'reactions',None),'results',None) or []:
        reaction=item.reaction
        reactions.append({'emoji':getattr(reaction,'emoticon',None),'document':str(reaction.document_id) if hasattr(reaction,'document_id') else None,'count':item.count,'chosen':getattr(item,'chosen_order',None) is not None})
    poll=getattr(getattr(message,'media',None),'poll',None);poll_data=None
    if poll:
        results=getattr(message.media,'results',None);counts={r.option:r for r in (getattr(results,'results',None) or [])}
        poll_data={'question':getattr(poll.question,'text',poll.question),'closed':bool(poll.closed),'multiple':bool(poll.multiple_choice),'quiz':bool(poll.quiz),'total':getattr(results,'total_voters',0) or 0,'answers':[{'text':getattr(a.text,'text',a.text),'option':base64.b64encode(a.option).decode(),'votes':getattr(counts.get(a.option),'voters',0),'chosen':bool(getattr(counts.get(a.option),'chosen',False))} for a in poll.answers]}
    replies=getattr(message,'replies',None)
    return {'entities':rich_entities(message),'reactions':reactions,'poll':poll_data,'edited':bool(getattr(message,'edit_date',None)),'pinned':bool(getattr(message,'pinned',False)),'reply_count':getattr(replies,'replies',0) or 0}


class PanelFeatures:
    def __init__(self,panel):self.p=panel;self.s=panel.s
    async def check(self,actor,uid,client,entity):
        if not authorized(actor):raise web.HTTPForbidden()
        await require_account(self.s,uid,actor)
        await require_source(self.s,uid,utils.get_peer_id(entity),client=client,entity=entity,actor=actor)
    async def replies(self,client,entity,items):
        from .web_panel import message_data
        ids={v['reply'] for v in items if v['reply'] and not v.get('reply_external')}
        if not ids:return
        originals=await client.get_messages(entity,ids=list(ids))
        if not isinstance(originals,(list,tuple)):return
        by_id={m.id:m for m in originals if m and getattr(m,'peer_id',None) and utils.get_peer_id(m.peer_id)==utils.get_peer_id(entity)}
        for item in items:
            original=by_id.get(item['reply'])
            value=message_data(original,bool(getattr(entity,'noforwards',False))) if original else None
            if value:item['reply_preview']={k:value[k] for k in ('id','text','sender','media')}
    async def receipts(self,client,entity,items):
        if not isinstance(entity,types.User) or not callable(client):return
        try:
            result=await client(functions.messages.GetPeerDialogsRequest([types.InputDialogPeer(entity)]))
            read_max=result.dialogs[0].read_outbox_max_id if result.dialogs else 0
        except errors.RPCError:return
        for item in items:
            if item['out']:item['delivery']='read' if item['id']<=read_max else 'sent'
    async def context(self,actor,uid,handle,mid):
        from .web_panel import message_data,name
        async with self.p.account(actor,uid) as (client,generation):
            entity=await self.p.target(actor,uid,generation,client,handle)
            await self.p.actions.known_message(client,entity,mid)
            rows=[m async for m in client.iter_messages(entity,offset_id=mid,add_offset=-30,limit=61)]
            items=[v for m in rows[:60] if (v:=message_data(m,bool(getattr(entity,'noforwards',False))))]
            await self.replies(client,entity,items);await self.check(actor,uid,client,entity)
            return {'items':list(reversed(items)),'next':rows[59].id if len(rows)>60 else None,'title':name(entity),'focus':mid}
    async def folders(self,actor,uid):
        async with self.p.account(actor,uid) as (client,generation):
            result=await client(functions.messages.GetDialogFiltersRequest())
            rows=[]
            for f in result.filters:
                if not hasattr(f,'id'):continue
                title=getattr(f.title,'text',f.title)
                rows.append({'id':f.id,'title':title,'include':[str(utils.get_peer_id(p)) for p in [*getattr(f,'pinned_peers',[]),*getattr(f,'include_peers',[])]],'exclude':[str(utils.get_peer_id(p)) for p in getattr(f,'exclude_peers',[])],**{key:bool(getattr(f,key,False)) for key in ('contacts','non_contacts','groups','broadcasts','bots','exclude_muted','exclude_read','exclude_archived')}})
            return {'items':rows}
    async def search(self,actor,uid,query,cursor=''):
        from .web_panel import message_data,peer_data,name,input_peer
        if not query.strip():return {'items':[],'next':None}
        async with self.p.account(actor,uid) as (client,generation):
            await require_consent(self.s,actor,uid)
            kwargs={}
            if cursor:
                c=self.p.unpack(cursor,'search')
                if any(c.get(k)!=v for k,v in {'actor':actor,'uid':uid,'generation':generation,'q':query}.items()):raise ValueError('Repite la búsqueda.')
                kwargs={'offset_rate':c['rate'],'offset_id':c['id'],'offset_peer':input_peer(c['peer'])}
            result=await client(functions.messages.SearchGlobalRequest(q=query[:100],filter=types.InputMessagesFilterEmpty(),min_date=None,max_date=None,offset_rate=kwargs.get('offset_rate',0),offset_peer=kwargs.get('offset_peer',types.InputPeerEmpty()),offset_id=kwargs.get('offset_id',0),limit=60))
            entities={utils.get_peer_id(e):e for e in [*result.users,*result.chats]};items=[]
            for m in result.messages:
                source=utils.get_peer_id(m.peer_id);entity=entities.get(source)
                if not entity:continue
                try:
                    require_entity(entity,uid)
                    await require_source(self.s,uid,source,client=client,entity=entity,actor=actor)
                except ValueError:continue
                value=message_data(m,bool(getattr(entity,'noforwards',False)))
                if not value:continue
                data=peer_data(entity,uid);kind='private' if isinstance(entity,types.User) else 'channel' if getattr(entity,'broadcast',False) else 'group'
                items.append({'message':value,'chat':{'id':str(source),'name':name(entity),'type':kind,'handle':self.p.pack('peer',actor=actor,uid=uid,generation=generation,**data),'avatar':bool(getattr(entity,'photo',None))}})
            next_cursor=None
            if len(result.messages)==60:
                last=result.messages[-1]
                next_cursor=self.p.pack('search',actor=actor,uid=uid,generation=generation,q=query,rate=getattr(result,'next_rate',0) or 0,id=last.id,peer=peer_data(entities[utils.get_peer_id(last.peer_id)],uid))
            await require_consent(self.s,actor,uid)
            return {'items':items,'next':next_cursor}
    async def topics(self,actor,uid,handle,cursor=''):
        async with self.p.account(actor,uid) as (client,generation):
            entity=await self.p.target(actor,uid,generation,client,handle)
            if not getattr(entity,'forum',False):return {'items':[],'next':None}
            offset_date=None;offset_id=0;offset_topic=0
            if cursor:
                value=self.p.unpack(cursor,'topics')
                if any(value.get(k)!=v for k,v in {'actor':actor,'uid':uid,'generation':generation,'peer':utils.get_peer_id(entity)}.items()):raise ValueError('Recarga los temas del grupo.')
                offset_date=datetime.fromtimestamp(value['date'],timezone.utc);offset_id=value['message'];offset_topic=value['topic']
            data=await client(functions.messages.GetForumTopicsRequest(entity,offset_date,offset_id,offset_topic,61))
            items=[{'id':t.id,'title':t.title,'closed':bool(getattr(t,'closed',False)),'unread':getattr(t,'unread_count',0)} for t in data.topics[:60] if isinstance(t,types.ForumTopic)]
            await self.check(actor,uid,client,entity)
            next_cursor=None
            if len(data.topics)>60:
                last=data.topics[59]
                next_cursor=self.p.pack('topics',actor=actor,uid=uid,generation=generation,peer=utils.get_peer_id(entity),date=last.date.timestamp(),message=last.top_message,topic=last.id)
            return {'items':items,'next':next_cursor}
    async def scheduled(self,actor,uid,handle):
        from .web_panel import message_data
        async with self.p.account(actor,uid) as (client,generation):
            entity=await self.p.target(actor,uid,generation,client,handle)
            data=await client(functions.messages.GetScheduledHistoryRequest(entity,0))
            items=[v for m in data.messages if (v:=message_data(m,bool(getattr(entity,'noforwards',False))))]
            await self.check(actor,uid,client,entity)
            return {'items':items}
    async def library(self,actor,uid,kind):
        async with self.p.account(actor,uid) as (client,generation):
            if kind=='stickers':
                data=await client(functions.messages.GetRecentStickersRequest(0));docs=data.stickers
            elif kind=='gifs':
                data=await client(functions.messages.GetSavedGifsRequest(0));docs=data.gifs
            else:raise ValueError('Biblioteca no válida.')
            return {'items':[{'id':str(d.id),'handle':self.p.pack('document',actor=actor,uid=uid,generation=generation,id=d.id,hash=d.access_hash,ref=d.file_reference.hex(),kind=kind),'label':next((a.alt for a in d.attributes if isinstance(a,types.DocumentAttributeSticker)),'GIF'),'mime':d.mime_type} for d in docs]}
    def document(self,actor,uid,generation,token):
        d=self.p.unpack(token,'document')
        if any(d.get(k)!=v for k,v in {'actor':actor,'uid':uid,'generation':generation}.items()):raise ValueError('El archivo no pertenece a esta cuenta. Abre de nuevo la biblioteca.')
        return types.InputDocument(d['id'],d['hash'],bytes.fromhex(d['ref'])),d['kind']
    async def library_thumb(self,actor,uid,token):
        async with self.p.account(actor,uid) as (client,generation):
            document,kind=self.document(actor,uid,generation,token)
            data=await client(functions.messages.GetRecentStickersRequest(0) if kind=='stickers' else functions.messages.GetSavedGifsRequest(0))
            docs=data.stickers if kind=='stickers' else data.gifs
            doc=next((d for d in docs if d.id==document.id),None)
            if not doc:return None
            return await client.download_media(doc,file=bytes,thumb=-1)
    def status(self,entity):
        status=getattr(entity,'status',None)
        if isinstance(status,types.UserStatusOnline):return 'En línea'
        if isinstance(status,types.UserStatusOffline):return 'Última vez: '+status.was_online.isoformat()
        if isinstance(status,types.UserStatusRecently):return 'Última vez recientemente'
        if isinstance(status,types.UserStatusLastWeek):return 'Última vez esta semana'
        if isinstance(status,types.UserStatusLastMonth):return 'Última vez este mes'
        return 'Bot' if getattr(entity,'bot',False) else ''
    async def perform(self,actor,input):
        action=input.get('action');uid=self.p.actions.p_integer(input.get('account'))
        async with self.p.account(actor,uid) as (client,generation):
            entity=await self.p.target(actor,uid,generation,client,input.get('peer',''));member,info=await self.p.actions.permissions(client,entity)
            mid=input.get('message');message=None
            if action in ('reaction','vote'):message=await self.p.actions.known_message(client,entity,mid)
            if action=='typing':
                if not info['send'].get('text'):return {'ok':True}
                await self.check(actor,uid,client,entity)
                await client(functions.messages.SetTypingRequest(entity,types.SendMessageTypingAction(),top_msg_id=self.p.actions.p_integer(input['topic']) if input.get('topic') else None))
                return {'ok':True}
            elif action=='reaction':
                emoji=input.get('emoji','')
                if not isinstance(emoji,str) or len(emoji)>32:raise ValueError('Reacción no válida.')
                request=functions.messages.SendReactionRequest(entity,message.id,reaction=[types.ReactionEmoji(emoji)] if emoji else [])
            elif action=='vote':
                poll=getattr(getattr(message,'media',None),'poll',None)
                if not poll or poll.closed:raise ValueError('Esta encuesta no admite votos.')
                options=input.get('options',[])
                if not isinstance(options,list):raise ValueError('Opciones no válidas.')
                try:options=[base64.b64decode(v,validate=True) for v in options]
                except Exception:raise ValueError('Opciones no válidas.') from None
                if not set(options)<={a.option for a in poll.answers} or (not poll.multiple_choice and len(options)>1):raise ValueError('Opciones no válidas.')
                request=functions.messages.SendVoteRequest(entity,message.id,options)
            elif action=='poll':
                if not info['send'].get('polls',info['send'].get('text')):raise ValueError('La cuenta no puede enviar encuestas aquí.')
                question=input.get('question','');answers=input.get('answers',[])
                if not isinstance(question,str) or not question.strip() or len(question)>300 or not isinstance(answers,list) or not 2<=len(answers)<=10 or any(not isinstance(a,str) or not a.strip() or len(a)>100 for a in answers):raise ValueError('Escribe una pregunta y entre 2 y 10 respuestas válidas.')
                poll=types.Poll(id=0,hash=0,question=types.TextWithEntities(question,[]),answers=[types.PollAnswer(types.TextWithEntities(a,[]),bytes([i])) for i,a in enumerate(answers)],multiple_choice=bool(input.get('multiple')),public_voters=bool(input.get('public')))
                request=functions.messages.SendMediaRequest(entity,types.InputMediaPoll(poll),'',reply_to=types.InputReplyToMessage(self.p.actions.p_integer(input['topic'])) if input.get('topic') else None)
            elif action=='saved-media':
                document,kind=self.document(actor,uid,generation,input.get('document',''))
                if not info['send'].get('stickers' if kind=='stickers' else 'gifs',info['send'].get('documents')):raise ValueError('La cuenta no puede enviar ese contenido aquí.')
                request=functions.messages.SendMediaRequest(entity,types.InputMediaDocument(document),'',reply_to=types.InputReplyToMessage(self.p.actions.p_integer(input['topic'])) if input.get('topic') else None)
            elif action in ('scheduled-send','scheduled-delete'):
                mid=self.p.actions.p_integer(mid);data=await client(functions.messages.GetScheduledHistoryRequest(entity,0))
                if not any(m.id==mid and m.out for m in data.messages):raise ValueError('Ese mensaje programado no está disponible en este chat.')
                request=(functions.messages.SendScheduledMessagesRequest if action=='scheduled-send' else functions.messages.DeleteScheduledMessagesRequest)(entity,[mid])
            else:raise ValueError('Acción no disponible.')
            await self.check(actor,uid,client,entity)
            receipt=await self.p.actions.reserve(actor,uid,utils.get_peer_id(entity),input)
            import json,asyncio
            try:
                await client(request);await self.s.repo.set_setting(receipt,json.dumps({'state':'done','action':action}));return {'ok':True}
            except (Exception,asyncio.CancelledError) as exc:
                await self.s.repo.set_setting(receipt,json.dumps({'state':'rejected' if isinstance(exc,errors.RPCError) else 'review','action':action}))
                if isinstance(exc,asyncio.CancelledError):raise
                raise ValueError('No se pudo confirmar la operación. Comprueba Telegram antes de repetirla: '+type(exc).__name__) from None
