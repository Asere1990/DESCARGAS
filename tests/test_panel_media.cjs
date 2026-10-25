const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../descargas_app/panel_web/app.js'),'utf8');
const viewer=source.slice(source.indexOf('let mediaCurrent='),source.indexOf("$('more-accounts').onclick"));
function setup({before=null,load=async()=>{}}={}){
 const nodes=new Map();
 function node(tag='div'){
  return {tagName:tag.toUpperCase(),children:[],listeners:{},classList:{toggle(){}},open:false,
   append(n){this.children.push(n);},replaceChildren(){this.children=[];},setAttribute(k,v){this[k]=v;},removeAttribute(k){delete this[k];},
   querySelectorAll(selector){return selector==='video,audio'?this.children.filter(n=>['VIDEO','AUDIO'].includes(n.tagName)):[];},
   addEventListener(type,handler){this.listeners[type]=handler;},pause(){this.paused=true;},load(){this.loads=(this.loads||0)+1;},
   close(){this.open=false;},showModal(){this.open=true;}};
 }
 const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
 const photo=id=>({id,text:'Foto '+id,media:{type:'photo',mime:'image/jpeg',name:id+'.jpg'}});
 const context={$,el:node,messages:new Map([photo(3),photo(2)].map(m=>[m.id,m])),modal:false,mode:'gallery',epoch:1,before,
  operationBusy:false,networkTail:Promise.resolve(),labels:{photo:'Foto',video:'Video'},fileUrl:(_,m)=>'/media/'+m.id,
  notice(){},pumpThumbs(){},loadMessages:load,jumpToMessage:async(...args)=>{context.jump=args;}};
 vm.createContext(context);vm.runInContext(viewer+'\nthis.openMedia=openMedia;this.navigateMedia=navigateMedia;',context);
 const swipe=(dx,dy=0,count=1)=>{const t=$('media-content');t.listeners.touchstart({touches:Array.from({length:count},()=>({clientX:200,clientY:200}))});t.listeners.touchend({touches:[],changedTouches:[{clientX:200+dx,clientY:200+dy}]});};
 return {context,$,photo,swipe};
}
test('horizontal finger swipes move both ways; vertical drags, short taps and pinch do not navigate',()=>{
 const {$,context,swipe}=setup();context.openMedia(context.messages.get(3));
 swipe(-100);assert.equal($('media-name').textContent,'2.jpg');
 swipe(100);assert.equal($('media-name').textContent,'3.jpg');
 for(const args of [[-20,0],[-80,100],[-100,0,2]]){swipe(...args);assert.equal($('media-name').textContent,'3.jpg');}
 swipe(100);assert.equal($('media-name').textContent,'3.jpg');
});
test('crossing the last loaded gallery photo fetches the next page and preserves order',async()=>{
 let state,calls=0;state=setup({before:2,load:async()=>{calls++;state.context.messages.set(1,state.photo(1));state.context.before=null;}});
 state.context.openMedia(state.context.messages.get(2));await state.context.navigateMedia(1);
 assert.equal(calls,1);assert.equal(state.$('media-name').textContent,'1.jpg');assert.equal(state.$('media-next').disabled,true);
});
test('closing while another page loads cannot reopen or change the viewer',async()=>{
 let release;const state=setup({before:2,load:()=>new Promise(r=>release=r)});
 state.context.openMedia(state.context.messages.get(2));const pending=state.context.navigateMedia(1);
 state.$('media-dialog').listeners.close();release();await pending;assert.equal(state.context.modal,false);assert.equal(state.$('media-content').children.length,0);
});
test('switching video to photo stops playback and stale video errors cannot overwrite the new photo',()=>{
 const state=setup();const video={id:4,media:{type:'video',mime:'video/mp4',name:'video.mp4'}};state.context.messages.set(4,video);state.context.openMedia(video);
 const player=state.$('media-content').children[0];state.context.navigateMedia(1);
 assert.equal(player.paused,true);assert.equal(player.loads,1);player.onerror();assert.equal(state.$('media-note').textContent,'');
});
test('view in chat requests the exact current message in chat context, even from loaded gallery tiles',async()=>{
 const state=setup();state.context.openMedia(state.context.messages.get(2));await state.$('media-to-chat').onclick();
 assert.deepEqual([...state.context.jump],[2,true]);assert.equal(state.context.modal,false);assert.equal(state.$('message-search').hidden,true);
});
