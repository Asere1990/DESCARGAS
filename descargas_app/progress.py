"""Low-frequency, best-effort status; transfer callbacks never perform network I/O."""
import asyncio
from contextvars import ContextVar
import time

ITEM_INDEX=ContextVar('descargas_item_index',default=0)
ACTIVE=ContextVar('descargas_progress',default=None)
NOTE='El proceso puede tardar unos minutos. Puedes hacer otras cosas; te dejaremos el contenido descargado aquí.'

class Progress:
    def __init__(self,service,uid,sizes):
        self.service=service;self.uid=uid;self.sizes=sizes
        self.received={};self.uploaded={};self.message=None;self.task=None;self.tick=0
        self.phase='download';self.finished=False
    def update(self,index,current,expected,upload=False):
        (self.uploaded if upload else self.received)[index]=max(0,current)
    def text(self):
        self.tick=(self.tick+1)%3
        total=sum(self.sizes)
        moved=sum(min(n,self.received.get(i,0))+min(n,self.uploaded.get(i,0)) for i,n in enumerate(self.sizes))
        percentage=min(99,int(100*moved/(2*total))) if total else None
        title='Enviando' if self.phase=='sending' else '𝘿𝙚𝙨𝙘𝙖𝙧𝙜𝙖𝙣𝙙𝙤'
        return title+'.'*(self.tick+1)+(f' {percentage}%' if percentage is not None and self.phase!='sending' else '')+'\n\n'+NOTE
    async def run(self):
        # Start independently; a slow status request cannot hold up media transfer.
        try:
            result=await self.service.api('sendMessage',{'chat_id':self.uid,'text':self.text()})
            self.message=result['message_id']
            while True:
                await asyncio.sleep(8)
                await self.service.api('editMessageText',{'chat_id':self.uid,'message_id':self.message,'text':self.text()})
        except asyncio.CancelledError:raise
        except Exception:pass
    def start(self):self.task=asyncio.create_task(self.run());return self
    async def close(self):
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        if self.message:
            # Fire-and-forget cleanup owned by the service, bounded HTTP timeout.
            async def remove():
                try:await self.service.api('deleteMessage',{'chat_id':self.uid,'message_id':self.message})
                except Exception:pass
            task=asyncio.create_task(remove())
            if hasattr(self.service,'tasks'):
                self.service.tasks.append(task)
                task.add_done_callback(lambda done:self.service.tasks.remove(done) if done in self.service.tasks else None)
