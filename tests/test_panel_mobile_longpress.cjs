const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../descargas_app/panel_web/app.js'),'utf8');
const selection=source.slice(source.indexOf('let batchLimit='),source.indexOf('function attachment(m)'));
test('iOS and Android hold on media opens menu without enabling batch selection',()=>{
 const nodes=new Map();
 function node(){return {hidden:false,dataset:{},classList:{toggle(){}},querySelectorAll(){return [];}};}
 const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
 let timer=null,shows=0;
 const context={
  $,chat:{type:'channel'},account:{id:22},operationBusy:false,
  document:{body:node()},messages:new Map(),
  setTimeout(fn){timer=fn;return 1;},clearTimeout(){timer=null;},
  messageOptions(){shows++;},notice(){},openMedia(){throw Error('tap did not happen')},
  URLSearchParams,Blob,AbortController,URL:{createObjectURL(){return '';},revokeObjectURL(){}}
 };
 vm.createContext(context);
 vm.runInContext(selection+'\nthis.installMessageHold=installMessageHold',context);
 const handlers={};
 const media={id:5,media:{type:'photo'}};
 const n={addEventListener(name,fn){handlers[name]=fn;}};
 context.installMessageHold(n,media);
 const e={button:0,pointerType:'touch',pointerId:7,isPrimary:true,
  clientX:100,clientY:100,stopPropagation(){},preventDefault(){}};
 handlers.pointerdown(e);assert.equal(typeof timer,'function');
 timer();assert.equal(shows,1);
 handlers.contextmenu({...e});assert.equal(shows,1);
 handlers.pointerup(e);
 handlers.pointerdown({...e,pointerType:'mouse'});
 assert.equal(timer,null);
 handlers.pointerdown({...e,pointerType:'pen'});assert.equal(typeof timer,'function');
 handlers.pointercancel(e);assert.equal(timer,null);
});
test('messages and gallery thumbnails both wire the same hold menu',()=>{
 assert.match(source,/installMediaSelection\(b,m\);return b;/);
 assert.match(source,/const b=el\('article'\);b\.dataset\.message=String\(m\.id\);installMessageHold\(b,m\);/);
 assert.ok(source.includes('Reportar a Telegram'));
});
