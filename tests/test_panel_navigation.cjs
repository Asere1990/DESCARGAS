const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

const source=fs.readFileSync(path.join(__dirname,'../descargas_app/panel_web/app.js'),'utf8');
const begin=source.indexOf('// Browser Back and panel X/Back controls');
const end=source.indexOf('function avatar(title)',begin);
assert.ok(begin>0&&end>begin,'Navigation controller must have its own testable block');
const code=source.slice(begin,end);

function setup(){
 const elements=new Map(),events=new Map(),records=[];
 function element(id){
  if(!elements.has(id))elements.set(id,{
   dataset:id==='workspace'?{view:'accounts'}:{},
   value:'',scrollTop:0,hidden:false,open:false,
   classList:{toggle(){}},close(){this.open=false;records.push('close:'+id);}
  });
  return elements.get(id);
 }
 const historyStack=[{}];let position=0;
 const history={
  state:historyStack[0],backCount:0,failPush:false,
  replaceState(data){historyStack[position]=data;this.state=data;},
  pushState(data){if(this.failPush)throw Error('WebView disallows navigation');historyStack.splice(position+1);historyStack.push(data);position++;this.state=data;},
  go(offset){const next=position+offset;if(next<0||next>=historyStack.length)return;position=next;this.state=historyStack[position];events.get('popstate')?.({state:this.state});},
  back(){this.backCount++;this.go(-1);},
  forward(){this.go(1);}
 };
 const ctx={
  $:element,
  document:{querySelector:s=>element(s),querySelectorAll:()=>[{dataset:{filter:'all'},classList:{toggle(){}}}]},
  window:{addEventListener:(name,fn)=>events.set(name,fn)},
  history,location:{pathname:'/panel',search:''},
  requestAnimationFrame:fn=>fn(),setTimeout:()=>0,
  pumpStatuses(){},captureAnchor:()=>null,restoreAnchor:()=>false,
  account:null,chat:null,mode:'messages',topic:0,filter:'all',query:'',epoch:0,
  contextMode:false,previousScrollTop:0,
  stopLive(){records.push('stopLive');},resetComposer(){},startLive(){},
  renderChats(){records.push('renderChats');},notice(value){records.push('notice:'+value);},
  selectAccount:async a=>{ctx.account=a;element('workspace').dataset.view='chats';},
  openChat:async c=>{ctx.chat=c;element('workspace').dataset.view='conversation';},
  setMode:async m=>{ctx.mode=m;},
  openMedia:m=>{element('media-dialog').open=true;ctx.lastMedia=m;},
  openPurchases:async()=>{element('management').open=true;},
 };
 vm.createContext(ctx);
 vm.runInContext(code+'\nthis.panelNavInit=panelNavInit;this.panelNavSave=panelNavSave;this.panelNavPush=panelNavPush;this.panelNavDismiss=panelNavDismiss;this.panelNavBackTo=panelNavBackTo;this.panelNavKind=panelNavKind;this.navReady=()=>panelNavReady;this.view=view;',ctx);
 const flush=()=>new Promise(setImmediate);
 return {ctx,history,element,flush,records,historyStack};
}
const user={id:'22',generation:3},chat={id:'55',handle:'opaque',name:'Grupo'};
function enterConversation(ctx){
 ctx.panelNavInit();
 ctx.account=user;ctx.view('chats');
 ctx.chat=chat;ctx.view('conversation');
}

test('closing the native video by browser Back stays in the same account, gallery and scroll position',async()=>{
 const {ctx,history,element,flush}=setup();
 enterConversation(ctx);
 ctx.mode='gallery';ctx.panelNavPush('view');
 element('thread').scrollTop=427;ctx.panelNavSave();
 element('media-dialog').open=true;ctx.panelNavPush('media',{media:{id:7}});
 history.back();await flush();
 assert.equal(element('media-dialog').open,false);
 assert.equal(element('workspace').dataset.view,'conversation');
 assert.equal(ctx.account.id,'22');assert.equal(ctx.chat.id,'55');
 assert.equal(ctx.mode,'gallery');assert.equal(element('thread').scrollTop,427);
 history.forward();await flush();
 assert.equal(element('media-dialog').open,true);
 assert.equal(ctx.lastMedia.id,7);
});

test('Back from a conversation returns to its own account chat list, not all accounts',async()=>{
 const {ctx,element,flush}=setup();ctx.panelNavInit();
 ctx.account=user;ctx.view('chats');
 element('.chat-list').scrollTop=245;ctx.panelNavSave();
 ctx.chat=chat;ctx.view('conversation');
 assert.equal(ctx.panelNavBackTo('chats'),true);
 await flush();
 assert.equal(element('workspace').dataset.view,'chats');
 assert.equal(ctx.account.id,'22');
 assert.equal(ctx.chat,null);
 assert.equal(element('.chat-list').scrollTop,245);
});

test('X and Back for bought media close only the purchase dialog',async()=>{
 const {ctx,history,element,flush}=setup();
 ctx.panelNavInit();ctx.account=user;ctx.view('chats');
 element('management').open=true;ctx.panelNavPush('purchases');
 ctx.panelNavDismiss('purchases');await flush();
 assert.equal(history.backCount,1);
 assert.equal(element('management').open,false);
 assert.equal(element('workspace').dataset.view,'chats');
 assert.equal(ctx.account.id,'22');
});

test('browser history stores only opaque step numbers, not Telegram account IDs or peer handles',()=>{
 const {ctx,historyStack}=setup();
 enterConversation(ctx);
 ctx.mode='gallery';ctx.panelNavPush('view');
 const entries=JSON.stringify(historyStack);
 assert.doesNotMatch(entries,/opaque|generation|accountRef|chatRef|telegram|22|55/);
 assert.match(entries,/panelStep/);
});

test('if the embedded browser rejects pushState, closing video cannot navigate out of the panel',()=>{
 const {ctx,history}=setup();
 ctx.panelNavInit();history.failPush=true;
 ctx.account=user;ctx.view('chats');
 assert.equal(ctx.navReady(),false);
 ctx.panelNavDismiss('media');
 assert.equal(history.backCount,0);
});

test('Back to another previously opened group restores its group instead of the all-chats screen',async()=>{
 const {ctx,history,element,flush}=setup();
 enterConversation(ctx);
 element('thread').scrollTop=185;ctx.panelNavSave();
 ctx.chat={id:'88',handle:'another-peer',name:'Otro grupo'};
 ctx.panelNavPush('view');
 history.back();await flush();
 assert.equal(ctx.chat.id,'55');
 assert.equal(element('workspace').dataset.view,'conversation');
 assert.equal(element('thread').scrollTop,185);
});
