const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

const source=fs.readFileSync(__dirname+'/../descargas_app/panel_web/app.js','utf8');
const start=source.indexOf('// Search execution lives on the authenticated web server, not in this browser.');
const end=source.indexOf('// Panel-only continuity.',start);
if(start<0||end<=start)throw Error('Persistent search controller missing');
const block=source.slice(start,end);

function setup(){
 const nodes=new Map(),events={},requests=[],shown=[],started={
  id:'search-1',mode:'phrases',query:'casa',state:'queued',
  completed:0,total:3,found:0,failures:0,current:'Cuenta A'
 };
 let job={...started},nextResult={items:[],next:null,cursor:0},shouldReject=false;
 const node=(tag='div',label='')=>({
  tagName:tag.toUpperCase(),dataset:{},value:'',hidden:false,textContent:label,children:[],
  append(...args){this.children.push(...args);},
  replaceChildren(...args){this.children=[...args];},
  querySelector(){return null;},remove(){},
  addEventListener(){},setAttribute(){}
 });
 const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)};
 const ctx={$,el:node,document:{hidden:false,addEventListener:(key,fn)=>{events['document:'+key]=fn;}},
  window:{addEventListener:(key,fn)=>{events['window:'+key]=fn;}},
  setInterval:()=>1,notice(){},isEntitySearchMode:m=>['channels','channel_id','groups','group_id','bots'].includes(m),
  accountSearchHit(a,hit){shown.push([a.id,hit.chat.id]);},entitySearchHit(a,hit){shown.push([a.id,hit.chat.id]);},
  metadataSearchHit(a){shown.push([a.id,'metadata']);},
  api:async(op,data)=>{requests.push({op,data});
   if(shouldReject)throw Error('network');
   if(op==='search-job-start')return {job};
   if(op==='search-job-status')return {job};
   if(op.startsWith('search-job-results?'))return nextResult;
   if(op==='search-job-control'){
    job={...job,state:data.action==='pause'?'paused':data.action==='cancel'?'cancelled':'queued'};
    return {job};
   }
   throw Error('Unexpected API: '+op);
  }};
 $('workspace').hidden=false;$('workspace').dataset={view:'accounts'};
 $('accounts-search-mode').value='phrases';$('accounts-query').value='casa';
 vm.createContext(ctx);
 vm.runInContext(block+'\nthis.startPersistentSearch=startPersistentSearch;this.refreshPersistentSearch=refreshPersistentSearch;this.controlPersistentSearch=controlPersistentSearch;this.showJob=renderServerJob;',ctx);
 return {ctx,$,events,requests,shown,job:()=>job,setJob:j=>{job={...job,...j}},setResults:r=>nextResult=r,setReject:v=>shouldReject=v};
}

test('starting search uses server API, not browser-only full scan',async()=>{
 const s=setup();
 await s.ctx.startPersistentSearch();
 assert.equal(s.requests[0].op,'search-job-start');
 assert.deepEqual(JSON.parse(JSON.stringify(s.requests[0].data)),{mode:'phrases',query:'casa'});
 assert.match(s.$('accounts-progress').textContent,/0\/3 cuentas activas/);
 assert.equal(s.$('cancel-account-search').hidden,false);
 assert.equal(s.$('cancel-account-search').textContent,'Detener búsqueda');
});

test('leaving the app never sends pause; returning restores progress and saved results',async()=>{
 const s=setup();
 await s.ctx.startPersistentSearch();
 s.ctx.document.hidden=true;
 s.setJob({state:'running',completed:2,found:1,current:'Cuenta C'});
 s.setResults({items:[{account:{id:'11',name:'C'},hit:{chat:{id:'-1004'},message:{id:14}}}],next:null,cursor:9});
 assert.equal(s.requests.some(r=>r.op==='search-job-control'),false);
 s.ctx.document.hidden=false;
 await s.ctx.refreshPersistentSearch();
 assert.equal(s.$('accounts-progress').textContent.includes('2/3'),true);
 assert.deepEqual(s.shown,[['11','-1004']]);
 assert.equal(s.requests.some(r=>r.op==='search-job-control'),false);
});

test('stop button cancels active job without leaving a zombie search',async()=>{
 const s=setup();
 await s.ctx.startPersistentSearch();
 await s.$('cancel-account-search').onclick();
 const stop=s.requests.find(r=>r.op==='search-job-control'&&r.data.action==='cancel');
 assert.ok(stop,'Stop must call the server cancel endpoint');
 assert.match(s.$('accounts-progress').textContent,/Búsqueda detenida/);
 assert.equal(s.$('continue-account-search').hidden,true);
 assert.equal(s.$('cancel-account-search').hidden,true);
});

test('page reload retrieves the existing search job instead of starting another',async()=>{
 const s=setup();
 s.setJob({state:'complete',total:12,completed:12,found:0,query:'grupo',mode:'groups'});
 await s.ctx.refreshPersistentSearch();
 assert.equal(s.$('accounts-query').value,'grupo');
 assert.equal(s.$('accounts-search-mode').value,'groups');
 assert.match(s.$('accounts-progress').textContent,/Búsqueda terminada · 12\/12/);
 assert.equal(s.requests.filter(r=>r.op==='search-job-start').length,0);
});

test('the form submit delegates all search modes to the server',async()=>{
 const s=setup();
 s.$('accounts-search-mode').value='bots';
 s.$('accounts-query').value='@bot';
 s.$('accounts-search-form').onsubmit({preventDefault(){}});
 // startPersistentSearch invokes API synchronously before awaiting.
 assert.equal(s.requests[0].op,'search-job-start');
 assert.equal(s.requests[0].data.mode,'bots');
});
