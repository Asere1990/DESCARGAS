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
