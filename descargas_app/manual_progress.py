"""Throttled real byte progress in the existing admin board only."""
import asyncio
from .manual_board import presentation


class ManualProgress:
    def __init__(self,service,job,items):
        self.s=service;self.job=job;self.sizes=[getattr(getattr(m,'file',None),'size',0) or 0 for m in items]
        self.received={};self.uploaded={};self.task=None;self.last=None
    def update(self,index,current,expected,upload=False):
        if index<len(self.sizes) and not self.sizes[index] and expected:self.sizes[index]=expected
        target=self.uploaded if upload else self.received
        target[index]=max(target.get(index,0),max(0,current))
    def text(self):
        total=sum(self.sizes)
        if not total:return 'Preparando transferencia; tamaño todavía no disponible.'
        percent=lambda values:min(100,int(100*sum(min(n,values.get(i,0)) for i,n in enumerate(self.sizes))/total))
        return f'Descarga del archivo o álbum: {percent(self.received)}% · Subida a ALMACEN: {percent(self.uploaded)}%'
    async def publish(self):
        s=self.s;job=self.job;token=job['manual_token'];text=self.text();progress_text=text
        if text==self.last:return
        # Progress is transient: no re-encryption or rewrite of a large gallery
        # snapshot, and no network I/O in the download callback itself.
        async with s.repo.pool.acquire() as conn:
            locked=await conn.fetchval('SELECT pg_try_advisory_lock(752,hashtext($1))',token)
            if not locked:return
            try:
                current=await conn.fetchrow('SELECT * FROM descarga_group_exports WHERE bot_id=$1 AND uid=$2 AND source=$3',s.bot_id,job['uid'],job['source'])
                if not current or current['scan']!=job.get('scan',0) or current['manual_token']!=token:return
                row=await conn.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=$1 AND token=$2',s.bot_id,token)
                if not row or row['closed'] or not row['busy'] or not row['message_id']:return
                data=s.repo.decode(row['payload'])
                if row['position']>=len(data['entries']) or data['entries'][row['position']]['mid']!=job['retry_message']:return
                data['progress']=progress_text
                text,buttons=presentation(row,data)
                target=row['message_id']
                if data.get('number_notice') and data.get('number_request'):
                    target=data['number_notice'];buttons=[]
                    label={'photo':'Foto','video':'Video','album':'Álbum'}.get(data.get('mode'),'Elemento')
                    text=f'⏳ {label} número {data["number_request"]} · Cuenta {row["uid"]}\n{progress_text}'
                async with asyncio.timeout(2):
                    await s.api('editMessageText',{'chat_id':row['actor'],'message_id':target,'text':text,'reply_markup':{'inline_keyboard':buttons},'link_preview_options':{'is_disabled':True}})
                self.last=progress_text
            finally:await conn.execute('SELECT pg_advisory_unlock(752,hashtext($1))',token)
    async def run(self):
        while True:
            try:await self.publish()
            except asyncio.CancelledError:raise
            except Exception:pass
            await asyncio.sleep(5)
    def start(self):self.task=asyncio.create_task(self.run());return self
    async def close(self):
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
