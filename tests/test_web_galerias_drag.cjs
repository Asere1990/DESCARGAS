"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");
const source=fs.readFileSync(path.join(__dirname,"../web_galerias/app.js"),"utf8");
const start=source.indexOf("function selectedDragIds(id){");
const end=source.indexOf("\nasync function addToPersonalFolder(",start);
assert.ok(start>=0&&end>start,"La función de arrastre múltiple debe existir");
const context=vm.createContext({S:{selected:new Set()}});
vm.runInContext(source.slice(start,end),context);

function dragged(selection,id){
 context.S.selected=new Set(selection);
 context.dragged=id;
 return Array.from(vm.runInContext("selectedDragIds(dragged)",context));
}

test("arrastrar una de las 10 seleccionadas lleva exactamente las 10",()=>{
 const selected=[1,2,3,4,5,6,7,8,9,10];
 assert.deepEqual(dragged(selected,6),selected);
 assert.deepEqual(dragged(selected,1),selected);
 assert.deepEqual(dragged(selected,10),selected);
});

test("selección parcial conserva todos los archivos seleccionados",()=>{
 assert.deepEqual(dragged([101,102,103],102),[101,102,103]);
 assert.deepEqual(dragged([401],401),[401]);
});

test("sin selección, o arrastrando uno no seleccionado, solo se mueve ese archivo",()=>{
 assert.deepEqual(dragged([],88),[88]);
 assert.deepEqual(dragged([1,2,3],99),[99]);
});

test("no permite lotes superiores a 10 ni identificadores inválidos",()=>{
 assert.deepEqual(dragged(Array.from({length:11},(_,i)=>i+1),6),[]);
 assert.deepEqual(dragged([],0),[]);
 assert.deepEqual(dragged([],-1),[]);
 assert.deepEqual(dragged([],"invalido"),[]);
});

test("gestos táctiles y de mouse usan el mismo grupo seleccionado",()=>{
 assert.match(source,/b\.ondrop=e=>\{[\s\S]*?const ids=selectedDragIds\(id\);[\s\S]*?addToPersonalFolder\(folder\.id,ids\)/);
 assert.match(source,/const destination=document\.elementFromPoint[\s\S]*?const ids=selectedDragIds\(m\.id\);[\s\S]*?addToPersonalFolder\(Number\(destination\.dataset\.folderDrop\),ids\)/);
 assert.match(source,/b\.setPointerCapture\(e\.pointerId\)/);
 assert.match(source,/if\(unique\.length>10\)return notice/);
});


test("miniatura flotante semitransparente conserva foto y contador 5/10",()=>{
 const elements={};
 function element(id){
  if(!elements[id]){
   const classes=new Set(["hidden"]);
   elements[id]={
    style:{},classList:{
     add(x){classes.add(x)},remove(x){classes.delete(x)},
     toggle(x,flag){if(flag)classes.add(x);else classes.delete(x)},
     contains(x){return classes.has(x)}
    },textContent:"",src:""
   };
  }
  return elements[id];
 }
 const widgetContext=vm.createContext({$:(id)=>element(id)});
 const a=source.indexOf("function showDragPreview(m,ids,x,y){");
 const b=source.indexOf("\nfunction dragMenuScroll(){",a);
 assert.ok(a>=0&&b>a);
 vm.runInContext(source.slice(a,b),widgetContext);
 widgetContext.media={thumb:"/web-galerias/media/9/thumb",kind:"photo"};
 widgetContext.group=[1,2,3,4,5];
 vm.runInContext("showDragPreview(media,group,160,280)",widgetContext);
 assert.equal(element("drag-preview-count").textContent,"5");
 assert.equal(element("drag-preview-image").src,"/web-galerias/media/9/thumb");
 assert.equal(element("drag-preview").style.left,"160px");
 assert.equal(element("drag-preview").style.top,"280px");
 assert.equal(element("drag-preview").classList.contains("hidden"),false);
 widgetContext.group=[1,2,3,4,5,6,7,8,9,10];
 vm.runInContext("showDragPreview(media,group,175,210)",widgetContext);
 assert.equal(element("drag-preview-count").textContent,"10");
 vm.runInContext("hideDragPreview()",widgetContext);
 assert.equal(element("drag-preview").classList.contains("hidden"),true);
});

test("arrastre tiene menú desplegable y opción crear carpeta como primer destino",()=>{
 const html=fs.readFileSync(path.join(__dirname,"../web_galerias/index.html"),"utf8");
 const segment=html.split('id="folder-drop-tray"',2)[1].split('id="album-grid"')[0];
 assert.ok(segment.indexOf('id="album-create-folder"')<segment.indexOf('id="folder-drop-targets"'));
 assert.match(segment,/data-folder-drop="new"/);
 assert.match(source,/function dragMenuScroll\(\)/);
 assert.match(source,/menu\.scrollTop\+=delta/);
 assert.match(source,/next\.classList\.add\("drag-target"\)/);
 assert.match(source,/createPersonalFolder\(ids\)/);
});
