const {test}=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../descargas_app/panel_web/app.js'),'utf8');
const selection=source.slice(source.indexOf('let batchSelecting='),source.indexOf('function attachment(m)'));
function setup(){const nodes=new Map();let sent;const make=tag=>({tagName:tag,children:[],hidden:false,classList:{toggle(){}},dataset:{},append(e){this.children.push(e);},setAttribute(){},removeAttribute(){},querySelectorAll(){return [];},remove(){},submit(){sent=this;}});const $=id=>{if(!nodes.has(id))nodes.set(id,make('div'));return nodes.get(id);};const media=(id,protected=false)=>({id,protected,media:{type:id===2?'video':'photo'}});const context={$,el:make,chat:{handle:'current-peer'},account:{id:22},csrf:'csrf-value',base:'/panel',operationBusy:false,messages:new Map([media(1),media(2),media(3,true)].map(m=>[m.id,m])),notice(){},document:{body:make('body')}};vm.createContext(context);vm.runInContext(selection+'\nthis.toggleBatch=toggleBatch;this.cancelBatch=cancelBatch;this.updateBatch=updateBatch;',context);return {context,$,sent:()=>sent};}
test('select loaded marks eligible files, toggling updates the count, and the native ZIP form carries only selected IDs and the current scope',()=>{const s=setup();s.$('batch-select').onclick();s.$('batch-all').onclick();assert.equal(s.$('batch-count').textContent,'3 seleccionados');s.context.toggleBatch(s.context.messages.get(1));assert.equal(s.$('batch-count').textContent,'2 seleccionados');s.context.updateBatch();assert.equal(s.$('batch-count').textContent,'2 seleccionados');s.$('batch-download').onclick();const form=s.sent(),fields=Object.fromEntries(form.children.map(n=>[n.name,n.value]));assert.equal(form.method,'POST');assert.equal(form.action,'/panel/api/batch-download');assert.equal(fields.messages,'2,3');assert.equal(fields.peer,'current-peer');assert.equal(fields.account,22);assert.equal(fields.csrf,'csrf-value');s.context.cancelBatch();assert.equal(s.$('batch-count').textContent,'0 seleccionados');assert.equal(s.$('batch-download').disabled,true);});

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
