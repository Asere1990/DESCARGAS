const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');

const source=fs.readFileSync(__dirname+'/../descargas_app/panel_web/app.js','utf8');
const helper=source.slice(source.indexOf('const ENTITY_SEARCH_MODES='),source.indexOf('function activeSearchAccounts('));
const scan=source.slice(source.indexOf("let overviewNeedle=''"),source.indexOf('function resetAccountSearch()'));
if(!helper.includes('function entitySearchMatches(')||!scan.includes('async function runAccountEntitySearch('))
 throw Error('Targeted entity search is missing');

const active=id=>({id:String(id),name:'Cuenta '+id,generation:3,status:{state:'active',checked:Date.now()/1000}});
const revoked=id=>({id:String(id),name:'Revocada '+id,generation:3,status:{state:'revoked',checked:Date.now()/1000}});
const ch=(id,type,name,username='',bot=false)=>({id:String(id),type,name,username,bot,handle:'handle-'+id});
function setup({accounts=[active(1)],pages=[],onApi=null,more=false}={}){
 const nodes=new Map(),progress=[],calls=[];
 function el(tag='div',value=''){
  const o={tagName:tag.toUpperCase(),textContent:value,children:[],dataset:{},hidden:false,removed:false,
   append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},
   querySelector(selector){return selector==='[data-result-more]'?this.children.find(x=>x.dataset?.resultMore&&!x.removed)||null:null;},
   remove(){this.removed=true;},setAttribute(){}};
  return o;
 }
 const $=id=>{
  if(!nodes.has(id)){
   const node=el();
   if(id==='accounts-progress'){
    let current='';Object.defineProperty(node,'textContent',{get(){return current;},set(value){current=value;progress.push(value);}});
   }
   nodes.set(id,node);
  }
  return nodes.get(id);
 };
 const accountsMap=new Map(accounts.map(a=>[a.id,a]));
 const ctx={$ ,el,Date,Number,String,Array,Set,Map,Math,JSON,Promise,
  overviewAccounts:accountsMap,accountsNext:more?'more':null,overviewRun:0,overviewSearching:false,statusQueue:[],overviewPending:[],
  document:{hidden:false},operationBusy:false,accountStates:{active:'Activa',unknown:'Sin verificar'},
  notice(){},pumpStatuses(){},stopLive(){},selectAccount(){},openChat(){},
  cancelOverviewSearch(){ctx.overviewRun++;ctx.overviewSearching=false;$('cancel-account-search').hidden=true;},
  setTimeout:callback=>{callback();return 1;},
  loadAccounts:async()=>{throw Error('Unexpected account paging in test');},
  api:async uri=>{calls.push(uri);return onApi?onApi(uri,calls):pages.shift()||{items:[],next:null,generation:3};}
 };
 $('accounts-query').value='';$('accounts-search-mode').value='channels';
 vm.createContext(ctx);
 vm.runInContext(helper+'\n'+scan+'\nthis.entitySearchMatches=entitySearchMatches;this.runAccountEntitySearch=runAccountEntitySearch;this.getProgress=()=>({complete:overviewComplete,total:overviewTotal,matches:overviewResults.length,failures:overviewFailures.length});',ctx);
 return {ctx,$,calls,progress};
}

test('category match isolates channels, groups and actual Telegram bots',()=>{
 const {ctx}=setup();
 const channel=ch('-100123','channel','Cubanas en el Yuma','cubanas_oficial');
 const supergroup=ch('-100456','group','Grupo México','mexico');
 const basicGroup=ch('-777','group','Vecinos');
 const bot=ch('987','private','Gestor Bot','gestorrobot',true);
 const human=ch('777','private','Gestor Bot','gestorrobot',false);
 assert.equal(ctx.entitySearchMatches(channel,'channels','cubanas'),true);
 assert.equal(ctx.entitySearchMatches(channel,'channels','@cubanas_oficial'),true);
 assert.equal(ctx.entitySearchMatches(channel,'channels',''),true);
 assert.equal(ctx.entitySearchMatches(supergroup,'channels','mexico'),false);
 assert.equal(ctx.entitySearchMatches(supergroup,'groups','mexico'),true);
 assert.equal(ctx.entitySearchMatches(basicGroup,'groups',''),true);
 assert.equal(ctx.entitySearchMatches(bot,'bots','@gestorrobot'),true);
 assert.equal(ctx.entitySearchMatches(human,'bots','Gestor'),false);
 assert.equal(ctx.entitySearchMatches(channel,'bots','Cubanas'),false);
});

test('exact channel and group IDs accept native or -100 forms but not partial matches',()=>{
 const {ctx}=setup();
 const c=ch('-100456789','channel','Canal'),
       group=ch('-100987654','group','Grupo'),
       old=ch('-34567','group','Viejo');
 for(const needle of ['-100456789','456789'])
  assert.equal(ctx.entitySearchMatches(c,'channel_id',needle),true);
 for(const needle of ['-100987654','987654'])
  assert.equal(ctx.entitySearchMatches(group,'group_id',needle),true);
 assert.equal(ctx.entitySearchMatches(old,'group_id','34567'),true);
 assert.equal(ctx.entitySearchMatches(old,'group_id','-34567'),true);
 assert.equal(ctx.entitySearchMatches(c,'channel_id','456'),false);
 assert.equal(ctx.entitySearchMatches(group,'channel_id','987654'),false);
 assert.equal(ctx.entitySearchMatches(c,'group_id','-100456789'),false);
 assert.equal(ctx.entitySearchMatches(c,'channel_id','abc'),false);
});

test('only active accounts are scanned and only matching channels render',async()=>{
 const s=setup({accounts:[active(1),revoked(2),active(3)],pages:[
  {items:[ch('-10011','channel','Cuba Music','musica'),ch('-10022','group','Cuba Music')],next:'more-one',generation:3},
  {items:[ch('-10011','channel','Cuba Music','musica'),ch('40','private','Music Bot','musicbot',true)],next:null,generation:3},
  {items:[ch('-10033','channel','Música Cubana','cuba_music'),ch('-10044','group','Cuba Salsa')],next:null,generation:3}
 ]});
 s.$('accounts-query').value='cuba';s.$('accounts-search-mode').value='channels';
 await s.ctx.runAccountEntitySearch();
 assert.deepEqual(s.calls.map(uri=>new URL('https://x/panel/'+uri).searchParams.get('account')),['1','1','3']);
 assert.ok(s.progress.some(x=>x.startsWith('0/2 ')));
 assert.ok(s.progress.some(x=>x.startsWith('1/2 ')));
 assert.match(s.$('accounts-progress').textContent,/Búsqueda terminada · 2\/2/);
 assert.equal(s.ctx.getProgress().matches,2);
 assert.equal(s.$('accounts-results').children.length,2);
 assert.match(s.$('accounts-results').children[0].children[1].textContent,/Canal · ID:/);
 assert.equal(s.$('continue-account-search').hidden,true);
});

test('bots mode shows only bot chats and supports username lookup',async()=>{
 const s=setup({pages:[{items:[ch('101','private','Cobros Bot','cobrobot',true),ch('202','private','Cobros Ayuda','ayuda',false),
  ch('-100303','channel','Bot alerts')],next:null,generation:3}]});
 s.$('accounts-search-mode').value='bots';s.$('accounts-query').value='@cobrobot';
 await s.ctx.runAccountEntitySearch();
 assert.equal(s.ctx.getProgress().matches,1);
 assert.match(s.$('accounts-results').children[0].children[1].textContent,/Bot · ID: 101/);
 assert.match(s.$('accounts-progress').textContent,/Búsqueda terminada/);
});

test('ID validation avoids all Telegram calls for invalid numeric input',async()=>{
 const s=setup();
 s.$('accounts-search-mode').value='channel_id';s.$('accounts-query').value='abc';
 await s.ctx.runAccountEntitySearch();
 assert.equal(s.calls.length,0);
 assert.match(s.$('accounts-progress').textContent,/Introduce el ID completo/);
});

test('failed pages show incomplete status and retry from their cursors',async()=>{
 let failed=true;
 const s=setup({accounts:[active(1),active(3)],onApi:uri=>{
  const u=new URL('https://x/'+uri);const a=u.searchParams.get('account');
  if(a==='1'){
   if(!u.searchParams.get('cursor'))return {items:[ch('-1004','group','Prueba')],next:'more',generation:3};
   if(failed){failed=false;throw Error('Telegram ocupado');}
   return {items:[ch('-1005','group','Prueba Dos')],next:null,generation:3};
  }
  return {items:[],next:null,generation:3};
 }});
 s.$('accounts-search-mode').value='groups';s.$('accounts-query').value='Prueba';
 await s.ctx.runAccountEntitySearch();
 assert.match(s.$('accounts-progress').textContent,/Búsqueda incompleta · 1\/2/);
 assert.equal(s.ctx.getProgress().matches,1);
 assert.equal(s.$('continue-account-search').hidden,false);
 await s.ctx.runAccountEntitySearch(true);
 assert.match(s.$('accounts-progress').textContent,/Búsqueda terminada · 2\/2/);
 assert.equal(s.ctx.getProgress().matches,2);
});

test('repeat cursors cannot mark an unfinished search as completed',async()=>{
 const s=setup({pages:[
 {items:[],next:'same',generation:3},
 {items:[],next:'same',generation:3}
 ]});
 await s.ctx.runAccountEntitySearch();
 assert.match(s.$('accounts-progress').textContent,/Búsqueda incompleta · 0\/1/);
 assert.equal(s.$('continue-account-search').hidden,false);
});

test('all five search options remain in the existing selector without changing phrase search',()=>{
 const html=fs.readFileSync(__dirname+'/../descargas_app/panel_web/index.html','utf8');
 for(const value of ['channels','channel_id','groups','group_id','bots'])assert.ok(html.includes('value="'+value+'"'));
 assert.ok(html.includes('value="phrases"'));
 assert.match(source,/if\(mode==='phrases'\)runAccountSearch\(false\)/);
 assert.match(source,/else if\(isEntitySearchMode\(mode\)\)runAccountEntitySearch\(false\)/);
});
