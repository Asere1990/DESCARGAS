const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'..','web_galerias');
const js=fs.readFileSync(path.join(root,'app.js'),'utf8');
const css=fs.readFileSync(path.join(root,'style.css'),'utf8');
const admin=fs.readFileSync(path.join(root,'admin.py'),'utf8');
const sources=fs.readFileSync(path.join(root,'folder_sources.py'),'utf8');

test('source actions distinguish handoff from reset and never request R2 deletion',()=>{
 for(const op of ['admin-folder-handoff','admin-folder-reconnect']){
  assert.ok(js.includes(op),op);
  assert.ok(admin.includes(op),op);
 }
 for(const kind of ['account','source']){
  const op='admin-folder-disconnect-'+kind;
  assert.ok(js.includes('"admin-folder-disconnect-"+mode'),op);
  assert.ok(admin.includes(op),op);
 }
 assert.match(js,/Reemplazar cuenta · mantener avance/);
 assert.match(js,/Conectar cuenta · empezar desde cero/);
 assert.match(sources,/state='paused_preview'/);
 assert.match(sources,/state IN \('preview','paused_preview'\)/);
 assert.match(sources,/progress_preserved/);
 assert.doesNotMatch(sources,/(delete_object|r2_deletion_queue|storage\.delete)\s*\(/);
});
test('admin, gallery and personal folders all use the same long-touch exchange',()=>{
 assert.match(js,/bindFolderSwap\(card,"public",folder\.id\)/);
 assert.match(js,/bindFolderSwap\(main,"personal",folder\.id\)/);
 assert.match(js,/bindFolderSwap\(trigger,"admin",f\.id\)/);
 assert.match(css,/\.folder-sort-held/);
 assert.match(css,/\.folder-sort-target/);
});

function node(){
 const listeners=new Map(),classNames=new Set();
 return {
  dataset:{},listeners,
  classList:{add:k=>classNames.add(k),remove:k=>classNames.delete(k)},
  addEventListener(name,callback){listeners.set(name,callback)},
 };
}
function exercise(kind){
 const nodes=[node(),node()],events=new Map(),calls=[],renders=[];
 const ctx={
  S:{folders:[{id:11},{id:22}],personal:[{id:11},{id:22}],
     admin:{folders:[{id:11},{id:22}]}},
  document:{
   elementFromPoint:()=>({closest:()=>nodes[1]}),
   addEventListener:(event,cb)=>events.set(event,cb),
   removeEventListener:(event)=>events.delete(event)
  },
  Date,Math,Number,String,
  setTimeout:fn=>{fn();return 1},clearTimeout:()=>{},
  api:async(op,payload)=>{calls.push({op,payload});return {ok:true}},
  notice:()=>{},
  previewAdmin:()=>renders.push('admin'),
  renderFolders:()=>renders.push('public'),
  renderPersonalFolders:()=>renders.push('personal')
 };
 const from=js.indexOf('function bindFolderSwap(node,scope,id){');
 const to=js.indexOf('\nfunction renderFolders(){',from);
 assert.ok(from>=0&&to>from);
 vm.createContext(ctx);
 vm.runInContext(js.slice(from,to)+'\nthis.bindFolderSwap=bindFolderSwap;',ctx);
 ctx.bindFolderSwap(nodes[0],kind,11);
 ctx.bindFolderSwap(nodes[1],kind,22);
 nodes[0].listeners.get('pointerdown')({
  pointerType:'mouse',button:0,pointerId:7,
  clientX:15,clientY:15,target:{closest:()=>nodes[0]}
 });
 events.get('pointermove')({
  pointerId:7,clientX:80,clientY:20,preventDefault(){}
 });
 events.get('pointerup')({
  pointerId:7,clientX:80,clientY:20,preventDefault(){}
 });
 return {ctx,calls,renders};
}
for(const scope of ['public','personal','admin']){
 test(scope+' held drag swaps two cards and persists only this scope',async()=>{
  const {ctx,calls,renders}=exercise(scope);
  await new Promise(resolve=>setImmediate(resolve));
  const items=scope==='public'?ctx.S.folders:scope==='personal'?ctx.S.personal:ctx.S.admin.folders;
  assert.equal(items.map(x=>x.id).join(','),'22,11');
  assert.equal(calls.length,1);
  assert.equal(calls[0].op,scope==='admin'?'admin-folder-order':'folder-order');
  assert.equal(calls[0].payload.ids.join(','),'22,11');
  if(scope!=='admin')assert.equal(calls[0].payload.kind,scope);
  assert.deepEqual(renders,[scope]);
 });
}
