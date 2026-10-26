const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync(__dirname+'/../descargas_app/panel_web/app.js','utf8');
const block=source.slice(source.indexOf('// Panel-only continuity.'),source.indexOf('(async()=>{try{const token=location.hash.slice(1);'));
function setup(){
 const nodes=new Map(),storage=new Map(),events={};
 const node=id=>{if(!nodes.has(id))nodes.set(id,{dataset:{view:'conversation'},hidden:false,value:'',scrollTop:45,addEventListener(){},replaceChildren(){}});return nodes.get(id);};
 const ctx={$:node,base:'/panel',Date,Number,JSON,Map,Array,Math,setTimeout,clearTimeout,localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
 document:{querySelector:node,querySelectorAll:()=>[],addEventListener(){}},window:{addEventListener:(k,v)=>events[k]=v},
 account:{id:'123',generation:2},chat:{id:'456',handle:'opaque',name:'Chat',type:'group'},mode:'messages',query:'',filter:'all',topic:0,contextMode:false,contextFocus:null,before:1,epoch:1,
 messages:new Map(Array.from({length:250},(_,i)=>[i+1,{id:i+1,text:'message '+i,protected:i===149}])),captureAnchor:()=>({id:'150',offset:-12}),requestAnimationFrame:fn=>fn(),restoreAnchor:()=>true,
 tabs(){},renderMessages(){ctx.renders++;},renders:0,api:async()=>{ctx.validated=true;return {};},applyLiveChanges:async ids=>{ctx.checked=ids;},loadMessages:async()=>{ctx.refreshed=true;},loadGalleryCount(){},previousScrollTop:0,notice(){},view(){},startLive(){},modal:false};
 vm.createContext(ctx);vm.runInContext(block+"\nresumeKey='test';resumeReady=true;this.saveResume=saveResume;this.readResume=readResume;this.forgetResume=forgetResume;this.restoreConversation=restoreConversation;",ctx);return {ctx,storage,events};
}
test('saves a bounded window around scroll anchor and omits protected content and credentials',()=>{
 const {ctx,storage}=setup();ctx.saveResume();const s=JSON.parse(storage.get('test'));
 assert.equal(s.anchor.id,'150');assert.ok(s.items.length<=100);assert.ok(s.items.some(m=>m.id===149));assert.ok(!s.items.some(m=>m.id===150));assert.equal(s.before,120);assert.equal(s.account.generation,2);assert.equal(s.csrf,undefined);
});
test('expired or corrupt data is ignored and storage failure does not break the panel',()=>{
 const {ctx,storage}=setup();storage.set('test','invalid');assert.equal(ctx.readResume(),null);
 storage.set('test',JSON.stringify({version:1,saved:Date.now()-8*86400000}));assert.equal(ctx.readResume(),null);
 ctx.localStorage.setItem=()=>{throw Error('quota');};assert.doesNotThrow(()=>ctx.saveResume());
});
test('authorization failure prevents cached conversation rendering',async()=>{
 const {ctx}=setup();ctx.api=async()=>{throw Error('revoked');};await assert.rejects(ctx.restoreConversation({items:[{id:77,text:'secret'}]}),/revoked/);assert.equal(ctx.renders,0);
});
test('authorized resume rechecks saved IDs then fetches new messages without a read action',async()=>{
 const {ctx}=setup();await ctx.restoreConversation({mode:'gallery',items:[{id:77,text:'cached'},{id:78,protected:true}],before:70});
 assert.equal(ctx.validated,true);assert.equal(ctx.mode,'gallery');assert.deepEqual([...ctx.checked],[77]);assert.equal(ctx.refreshed,true);assert.equal(ctx.messages.has(78),false);
});
test('logout/revocation cleanup removes snapshot and disables further saves',()=>{
 const {ctx,storage}=setup();ctx.saveResume();ctx.forgetResume();assert.equal(storage.size,0);ctx.saveResume();assert.equal(storage.size,0);
});
