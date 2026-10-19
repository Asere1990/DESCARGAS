"""Discrete-event capacity model. Synthetic timings, no Telegram or network traffic.
Matches the scalable service's per-order worker ownership, two-second pause,
serial polling acknowledgements, shared 0.06s send pacing and archive queue.
Network preparation duration is an explicit assumption, not a measurement.
"""
from collections import deque
import heapq
import itertools
import json
import math
from pathlib import Path
import random

class Simulation:
    def __init__(self,batches,concurrency,prepare_seconds,ack=True,completion=True,fair=False,archives=True,
                 send_interval=.06,db_latency=.005,api_latency=.05):
        self.batches=batches;self.concurrency=concurrency;self.prepare=prepare_seconds
        self.ack=ack;self.completion=completion;self.fair=fair;self.archives=archives
        self.spacing=send_interval;self.db=db_latency;self.api=api_latency
        self.events=[];self.sequence=itertools.count();self.now=0.;self.limits={}
        self.waiting=deque();self.archive_queue=deque();self.free=concurrency;self.archive_free=2
        self.starts={};self.first_delivery={};self.finished={};self.received={};self.archive_finished=[]
        self.remaining=dict(enumerate(batches));self.sent=0
    def at(self,t,callback,*args):heapq.heappush(self.events,(t,next(self.sequence),callback,args))
    def pace(self,key,spacing):
        slot=max(self.now,self.limits.get(key,0.));self.limits[key]=slot+spacing;return slot
    def run(self):
        self.at(0,self.input,0)
        while self.events:
            self.now,_,callback,args=heapq.heappop(self.events);callback(*args)
        assert len(self.finished)==len(self.batches)
        assert self.sent==sum(self.batches)
        def stats(values):
            values=sorted(values);n=len(values)
            return {'median':round(values[n//2],2),'p95':round(values[math.ceil(.95*n)-1],2),
                    'max':round(values[-1],2),'under_60_percent':round(100*sum(v<60 for v in values)/n,1)}
        return {'users':len(self.batches),'files':sum(self.batches),'concurrency':self.concurrency,
            'assumed_prepare_seconds_per_file':self.prepare,
            'first_start_from_user_request':stats(list(self.starts.values())),
            'first_delivery_from_user_request':stats(list(self.first_delivery.values())),
            'complete_order_from_user_request':stats(list(self.finished.values())),
            'last_archive_seconds':round(max(self.archive_finished,default=0),2)}
    def input(self,uid):
        if uid<len(self.batches):self.at(self.now+self.db,self.accept,uid)
    def accept(self,uid):
        self.received[uid]=self.now;self.waiting.append(uid);self.dispatch()
        if self.ack:
            slot=self.pace('bot',self.spacing)
            self.at(slot+self.api+self.db,self.input,uid+1)
        else:self.at(self.now+self.db,self.input,uid+1)
    def dispatch(self):
        while self.free and self.waiting:
            uid=self.waiting.popleft();self.free-=1;self.start(uid)
    def start(self,uid):
        self.starts.setdefault(uid,self.now)
        self.at(self.now+self.prepare,self.ready_to_send,uid)
    def ready_to_send(self,uid):
        slot=self.pace('bot',self.spacing);self.at(slot+.1,self.delivered,uid)
    def delivered(self,uid):
        self.sent+=1;self.first_delivery.setdefault(uid,self.now);self.remaining[uid]-=1
        if self.archives:self.archive_queue.append(uid);self.dispatch_archive()
        self.at(self.now+2,self.next_file,uid)
    def next_file(self,uid):
        if self.remaining[uid]:
            if self.fair:self.waiting.append(uid);self.release()
            else:self.start(uid)
        elif self.completion:
            slot=self.pace('bot',self.spacing);self.at(slot+self.api,self.finish,uid)
        else:self.finish(uid)
    def finish(self,uid):self.finished[uid]=self.now;self.release()
    def release(self):self.free+=1;self.dispatch()
    def dispatch_archive(self):
        while self.archive_free and self.archive_queue:
            uid=self.archive_queue.popleft();self.archive_free-=1
            slot=self.pace('group',3.1);self.at(slot,self.copy_archive,uid)
    def copy_archive(self,uid):
        slot=self.pace('bot',self.spacing);self.at(slot+self.api,self.caption_archive,uid)
    def caption_archive(self,uid):
        slot=self.pace('bot',self.spacing);self.at(slot+self.api,self.finish_archive,uid)
    def finish_archive(self,uid):
        self.archive_finished.append(self.now);self.archive_free+=1;self.dispatch_archive()

def main():
    scenarios={}
    for seconds in (10,30,60):
        for concurrency in (6,60,200,500,600,1000):
            scenarios[f'current_{seconds}s_{concurrency}slots']=Simulation([1]*1000,concurrency,seconds).run()
            scenarios[f'fair_quiet_{seconds}s_{concurrency}slots']=Simulation([1]*1000,concurrency,seconds,
                ack=False,completion=False,fair=True).run()
    # Mixed request sizes: long batches first is a deliberately adverse fairness test.
    batches=[20]*50+[3]*150+[1]*800
    for fair in (False,True):
        scenarios['mixed_'+('fair_quiet' if fair else 'current')]=Simulation(batches,60,30,
            ack=not fair,completion=not fair,fair=fair).run()
    metadata={'kind':'synthetic discrete-event model','arrival_pattern':'1000 already-connected users at time zero',
        'network':'Unlimited aggregate bandwidth; each file gets the assumed independent preparation time.',
        'limitations':['No real Telegram requests or file transfers.',
            'No modeled FloodWait, TCP throughput sharing, CPU contention, login delays or database saturation.',
            'All users have valid sessions and accessible content; one file per request except mixed cases.',
            'The 0.06s pacing is an application setting, not a claimed Telegram platform maximum.',
            'Fair/quiet variants are hypothetical changes, not the deployed implementation.',
            'First delivery is measured separately from all files in the request.'],
        'scenarios':scenarios}
    path=Path(__file__).with_name('results.json');path.write_text(json.dumps(metadata,indent=2)+'\n')
    for name in ['current_30s_6slots','current_30s_60slots','current_30s_200slots','current_30s_500slots','current_30s_1000slots',
                 'fair_quiet_30s_500slots','fair_quiet_30s_1000slots','mixed_current','mixed_fair_quiet']:
        r=scenarios[name]
        print(name,'start max',r['first_start_from_user_request']['max'],'delivery max',r['first_delivery_from_user_request']['max'],
            'start <60%',r['first_start_from_user_request']['under_60_percent'],'delivery <60%',r['first_delivery_from_user_request']['under_60_percent'])
if __name__=='__main__':main()
