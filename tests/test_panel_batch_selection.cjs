const {test}=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../descargas_app/panel_web/app.js'),'utf8');
const selection=source.slice(source.indexOf('let batchLimit='),source.indexOf('function attachment(m)'));
function setup(){const nodes=new Map();let sent;const make=tag=>({tagName:tag,children:[],hidden:false,classList:{toggle(){}},dataset:{},append(e){this.children.push(e);},setAttribute(){},removeAttribute(){},querySelectorAll(){return [];},remove(){},submit(){sent=this;}});const $=id=>{if(!nodes.has(id))nodes.set(id,make('div'));return nodes.get(id);};const media=(id,protected=false)=>({id,protected,media:{type:id===2?'video':'photo'}});const context={AbortController,URLSearchParams,Blob,URL:{createObjectURL(){return 'blob:zip';},revokeObjectURL(){}},$,el:make,chat:{handle:'current-peer'},account:{id:22},csrf:'csrf-value',base:'/panel',operationBusy:false,messages:new Map([media(1),media(2),media(3,true)].map(m=>[m.id,m])),notice(){},document:{body:make('body')}};vm.createContext(context);vm.runInContext(selection+'\nthis.toggleBatch=toggleBatch;this.cancelBatch=cancelBatch;this.updateBatch=updateBatch;',context);return {context,$,sent:()=>sent};}
test('selection submits the chosen IDs to durable bot delivery',async()=>{const s=setup();let request;s.context.api=async(op,data)=>{request={op,data};return {message:'Registrada'};};s.$('batch-select').onclick();s.$('batch-all').onclick();s.context.toggleBatch(s.context.messages.get(1));await s.$('batch-download').onclick();assert.equal(request.op,'deliver-selection');assert.deepEqual(Array.from(request.data.messages),[2,3]);assert.equal(request.data.account,22);assert.equal(request.data.peer,'current-peer');assert.equal(s.$('zip-progress').textContent,'Registrada');});
test('hold selects once, a normal tap toggles another file, and scrolling cancels the hold',()=>{
 const s=setup();let scheduled=null;let opened=0;
 Object.assign(s.context,{setTimeout(fn){scheduled=fn;return 1;},clearTimeout(){scheduled=null;},openMedia(){opened++;}});
 vm.runInContext('this.installMediaSelection=installMediaSelection',s.context);
 const make=()=>({events:{},addEventListener(k,v){this.events[k]=v;}});
 const a=make(),b=make();s.context.installMediaSelection(a,s.context.messages.get(1));s.context.installMediaSelection(b,s.context.messages.get(2));
 const event={button:0,clientX:10,clientY:10,preventDefault(){}};
 a.events.pointerdown(event);assert.equal(typeof scheduled,'function');scheduled();a.events.pointerup();a.onclick(event);
 assert.equal(s.$('batch-count').textContent,'1 seleccionados');assert.equal(opened,0);
 b.onclick(event);assert.equal(s.$('batch-count').textContent,'2 seleccionados');
 b.events.pointerdown(event);b.events.pointermove({...event,clientY:35});assert.equal(scheduled,null);
 const protectedNode=make();s.context.installMediaSelection(protectedNode,s.context.messages.get(3));protectedNode.events.pointerdown(event);assert.equal(typeof scheduled,'function');scheduled();assert.equal(s.$('batch-count').textContent,'3 seleccionados');
});

test('reviewer selection and mark-all stop at ten files',()=>{const s=setup();vm.runInContext('batchLimit=10',s.context);for(let id=4;id<=20;id++)s.context.messages.set(id,{id,media:{type:'photo'}});s.$('batch-all').onclick();assert.equal(s.$('batch-count').textContent,'10 seleccionados');s.context.toggleBatch(s.context.messages.get(20));assert.equal(s.$('batch-count').textContent,'10 seleccionados');});
