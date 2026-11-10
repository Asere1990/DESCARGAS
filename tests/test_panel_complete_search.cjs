const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');

const source=fs.readFileSync(__dirname+'/../descargas_app/panel_web/app.js','utf8');
const helpers=source.slice(source.indexOf('function accountMetadataMatches('),source.indexOf('function cancelOverviewSearch()'));
const scan=source.slice(source.indexOf("let overviewNeedle=''"),source.indexOf('function resetAccountSearch()'));
if(!scan.includes('async function runAccountSearch(')||!scan.includes('async function runAccountMetadataSearch('))throw Error('Account search scan not found');

function setup({accounts=[],pages=[],onApi=null,hasMore=false,expand=null}={}){
 const store=new Map(),observations=[],calls=[],results=[];
 function element(name='div',txt=''){
  const node={name,children:[],dataset:{},hidden:false,removed:false,onclick:null,textContent:txt,value:'',
   append(...v){this.children.push(...v);},replaceChildren(...v){this.children=v;},
   querySelector(){return null;},remove(){this.removed=true;},setAttribute(){}};
  return node;
 }
 const $=id=>{
  if(!store.has(id)){
   const obj=element('div');
   if(id==='accounts-progress'){
    let value='';Object.defineProperty(obj,'textContent',{get(){return value;},set(v){value=v;observations.push(v);}});
   }
   store.set(id,obj);
  }
  return store.get(id);
 };
 const accountMap=new Map(accounts.map(a=>[a.id,a]));
 const ctx={$,el:element,Date,Number,Math,Map,Set,String,Array,Promise,JSON,
  setTimeout:callback=>{callback();return 1;},
  overviewAccounts:accountMap,accountsNext:hasMore?'next':null,overviewRun:0,
  overviewSearching:false,statusQueue:[],overviewPending:[],document:{hidden:false},
  operationBusy:false,accountStates:{active:'Activa',unknown:'Sin verificar'},
  pumpStatuses(){},stopLive(){},notice(){},selectAccount(){},cancelOverviewSearch(){
   ctx.overviewRun++;ctx.overviewSearching=false;
   $('cancel-account-search').hidden=true;
  },
  loadAccounts:async()=>{
   if(!expand)throw Error('Unexpected account loading');
   const more=expand(ctx);
   ctx.accountsNext=more||null;
  },
  api:async op=>{
   calls.push(op);
   if(onApi)return onApi(op,calls);
   return pages.shift()||{items:[],next:null};
  },
  accountSearchHit(a,hit){results.push([a.id,hit.chat.id,hit.message?.id||'title']);}
 };
 $('accounts-query').value='Moe';$('accounts-search-mode').value='phrases';
 vm.createContext(ctx);
 vm.runInContext(helpers+'\n'+scan+'\nthis.runAccountSearch=runAccountSearch;this.runAccountMetadataSearch=runAccountMetadataSearch;this.getState=()=>({complete:overviewComplete,total:overviewTotal,failures:overviewFailures.length,results:overviewResults.length});',ctx);
 return {ctx,$,calls,observations,results};
}
const active=(id,name='Cuenta '+id)=>({id:String(id),name,status:{state:'active',checked:Date.now()/1000}});
const inactive=(id,state)=>({id:String(id),name:'Inactiva '+id,status:{state}});

test('the scan visits only confirmed active accounts and advances 0/N, 1/N, 2/N',async()=>{
 const items=[
  {items:[{chat:{id:'-5',name:'Taberna'},message:null}],next:'cursor-1'},
  {items:[{chat:{id:'-5',name:'Taberna'},message:null},{chat:{id:'-5',name:'Taberna'},message:{id:17}}],next:null},
  {items:[{chat:{id:'-8',name:'Moecito'},message:null}],next:null}
 ];
 const s=setup({accounts:[active(1),inactive(2,'revoked'),active(3),inactive(4,'unknown')],pages:items});
 await s.ctx.runAccountSearch();
 assert.deepEqual(s.calls.map(x=>new URL('https://x/'+x).searchParams.get('account')),['1','1','3']);
 assert.ok(s.observations.some(x=>x.startsWith('0/2 ')));
 assert.ok(s.observations.some(x=>x.startsWith('1/2 ')));
 assert.match(s.$('accounts-progress').textContent,/Búsqueda terminada · 2\/2/);
 assert.equal(s.ctx.getState().results,3);
 assert.equal(s.results.length,3);
 assert.equal(s.$('continue-account-search').hidden,true);
});

test('33 pages for a single active account are read, not cut after 30 calls',async()=>{
 let count=0;
 const s=setup({accounts:[active(11)],onApi:async()=>{
  count++;
  return {items:count===33?[{chat:{id:'-7'},message:{id:77}}]:[],next:count<33?'cursor-'+count:null};
 }});
 await s.ctx.runAccountSearch();
 assert.equal(count,33);
 assert.match(s.$('accounts-progress').textContent,/Búsqueda terminada · 1\/1/);
 assert.equal(s.ctx.getState().results,1);
});

test('an incomplete Telegram account cannot be marked complete, and Continue retries it',async()=>{
 let failed=true;
 const s=setup({accounts:[active(1),active(2)],onApi:async op=>{
  if(op.includes('account=1')&&failed){failed=false;throw Error('Telegram rechazó temporalmente la consulta');}
  return {items:[{chat:{id:op.includes('account=1')?'-1':'-2'},message:null}],next:null};
 }});
 await s.ctx.runAccountSearch();
 assert.match(s.$('accounts-progress').textContent,/Búsqueda incompleta · 1\/2/);
 assert.equal(s.$('continue-account-search').hidden,false);
 assert.equal(s.ctx.getState().failures,1);
 await s.ctx.runAccountSearch(true);
 assert.match(s.$('accounts-progress').textContent,/Búsqueda terminada · 2\/2/);
 assert.equal(s.ctx.getState().failures,0);
 assert.equal(s.results.length,3); // First partial render, then both found.
});

test('metadata search also searches only active accounts and displays full progress',async()=>{
 const a=active(11,'José García');a.country='España';
 const b=active(22,'Moe');b.country='Cuba';
 const c=inactive(33,'revoked');c.name='Moe';
 const s=setup({accounts:[a,b,c]});
 s.$('accounts-search-mode').value='name';s.$('accounts-query').value='Moe';
 await s.ctx.runAccountMetadataSearch();
 assert.match(s.$('accounts-progress').textContent,/Búsqueda terminada · 2\/2/);
 assert.equal(s.ctx.getState().results,1);
 assert.ok(s.observations.some(x=>x.startsWith('0/2 ')));
});

test('known API cursor loops never produce a false completed status',async()=>{
 const s=setup({accounts:[active(99)],onApi:async()=>({items:[],next:'repeat'})});
 await s.ctx.runAccountSearch();
 assert.match(s.$('accounts-progress').textContent,/Búsqueda incompleta · 0\/1/);
 assert.equal(s.$('continue-account-search').hidden,false);
});

test('the search results are rendered in batches without discarding remaining matches',async()=>{
 const cases=Array.from({length:185},(_,i)=>({chat:{id:'-'+(i+1),name:'Moe'},message:{id:i+1}}));
 const s=setup({accounts:[active(1)],pages:[{items:cases,next:null}]});
 await s.ctx.runAccountSearch();
 assert.equal(s.ctx.getState().results,185);
 assert.equal(s.results.length,80);
 assert.equal(s.$('accounts-results').children.length,1); // Only the load-more button is appended by this mock.
 s.$('accounts-results').children[0].onclick();
 assert.equal(s.results.length,160);
 s.$('accounts-results').children[1].onclick();
 assert.equal(s.results.length,185);
});
