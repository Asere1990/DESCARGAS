"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const source=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");
const tools=fs.readFileSync(__dirname+"/../web_galerias/admin_tools.js","utf8");
const markup=fs.readFileSync(__dirname+"/../web_galerias/index.html","utf8");

function between(source,start,end){
 const a=source.indexOf(start),b=source.indexOf(end,a);
 assert.ok(a>=0&&b>a,"Function block not found: "+start);
 return source.slice(a,b);
}
function clock(){
 let now=1000,next=0;const pending=new Map();
 return {
  Date:{now:()=>now},
  setTimeout(fn,delay){const id=++next;pending.set(id,{fn,delay});return id;},
  clearTimeout(id){pending.delete(id);},
  advance(ms){now+=ms;},
  run(delay){
   const scheduled=[...pending.entries()].filter(([,t])=>t.delay===delay);
   for(const [id,t] of scheduled){pending.delete(id);t.fn();}
  },
  pending(){return pending.size;}
 };
}

test("single tap opens photo, double tap saves it without opening viewer",()=>{
 const timers=clock(),opened=[],saved=[],item={id:501,favorite:false};
 const S={folder:{id:19},epoch:1,items:[item],selection:false,selected:new Set(),suppressClick:null};
 const context={...timers,S,window:{matchMedia:()=>({matches:true})},
  openViewer:i=>opened.push(i),saveFavoriteFromDoubleTap:m=>saved.push(m.id),
  toggleSelection:()=>assert.fail("Should not select")};
 vm.createContext(context);
 vm.runInContext(between(source,"let pendingMediaTap=null;","function renderItems(){"),context);
 context.onMediaTileClick(item,0,{pointerType:"touch",detail:1});
 assert.equal(opened.length,0);
 timers.advance(150);
 context.onMediaTileClick(item,0,{pointerType:"touch",detail:1});
 assert.deepEqual(saved,[501]);assert.deepEqual(opened,[]);
 timers.run(275);assert.deepEqual(opened,[]);
 timers.advance(450);
 context.onMediaTileClick(item,0,{pointerType:"touch",detail:1});
 timers.run(275);assert.deepEqual(opened,[0]);
});

test("checkmark selection never triggers a favorite or delayed viewer",()=>{
 const timers=clock(),actions=[],item={id:19};
 const S={folder:{id:3},epoch:1,items:[item],selection:true,selected:new Set(),suppressClick:null};
 const context={...timers,S,window:{matchMedia:()=>({matches:true})},
  openViewer:()=>assert.fail("No viewer in select mode"),
  saveFavoriteFromDoubleTap:()=>assert.fail("No favorite in select mode"),
  toggleSelection:id=>actions.push(id)};
 vm.createContext(context);
 vm.runInContext(between(source,"let pendingMediaTap=null;","function renderItems(){"),context);
 context.onMediaTileClick(item,0,{pointerType:"touch",detail:1});
 assert.deepEqual(actions,[19]);assert.equal(timers.pending(),0);
 S.suppressClick=19;
 context.onMediaTileClick(item,0,{pointerType:"touch",detail:1});
 assert.deepEqual(actions,[19]);
});

test("double tap persists once via existing authenticated favorite API",async()=>{
 const item={id:512,favorite:false};
 const S={me:{allowed:true},folder:{id:9},items:[item],viewer:0,collectionsAt:500};
 const calls=[],messages=[];let paint=0,resume;
 const api=(op,payload)=>{calls.push([op,payload]);return new Promise(resolve=>{resume=resolve;});};
 const context={S,api,tg:null,$:()=>({classList:{contains:()=>true}}),
  renderItems:()=>{paint++;},refreshCollections:async()=>{},
  notice:t=>messages.push(t)};
 vm.createContext(context);
 vm.runInContext(between(source,"// Double tap adds a favorite.","function renderFolders(){"),context);
 const first=context.saveFavoriteFromDoubleTap(item);
 const duplicate=context.saveFavoriteFromDoubleTap(item);
 assert.equal(calls.length,1);assert.equal(calls[0][0],"favorite-set");
 assert.equal(calls[0][1].media,512);assert.equal(calls[0][1].favorite,true);
 resume({ok:true});await Promise.all([first,duplicate]);
 assert.equal(item.favorite,true);assert.equal(S.collectionsAt,0);
 assert.equal(paint,1);assert.equal(messages.length,1);
 await context.saveFavoriteFromDoubleTap(item);assert.equal(calls.length,1);
});

test("favorite long press opens actions instead of drag",()=>{
 const timers=clock(),events={},shown=[],m={id:44};
 const b={addEventListener:(type,cb)=>{events[type]=cb;},classList:{remove(){},add(){}}};
 const S={folder:{favorites:true},search:"",selected:new Set(),selection:false,suppressClick:null,drag:null};
 const context={...timers,S,b,window:{CubanitasTools:{mediaActions:item=>shown.push(item.id)}},
  tg:null,notice:()=>assert.fail("No limit warning")};
 vm.createContext(context);
 vm.runInContext(between(source,"function attachLongPress(b,m,index){","function fallback(b,m)"),context);
 context.attachLongPress(b,m,0);
 events.pointerdown({pointerType:"touch",clientX:40,clientY:50,pointerId:10});
 timers.run(450);
 assert.deepEqual(shown,[44]);assert.equal(S.suppressClick,44);
 assert.equal(S.selection,false);assert.equal(S.drag,null);
 events.pointerup({clientX:40,clientY:50});
 assert.deepEqual(shown,[44]);
});

test("favorites checkmarks display download and original-gallery bottom actions",()=>{
 const nodes=new Map();
 function $(id){
  if(!nodes.has(id))nodes.set(id,{classList:{hidden:false,toggle(name,hide){if(name==="hidden")this.hidden=hide;}},
    textContent:"",disabled:false});
  return nodes.get(id);
 }
 const S={me:{admin:false},folder:{favorites:true},selection:true,
  selected:new Set([1]),items:[{id:1,available:true}]};
 const context={$,S,selectionLimit:()=>10};
 vm.createContext(context);
 vm.runInContext(between(source,"function updateSelection(){","function toggleSelection(id){"),context);
 context.updateSelection();
 assert.equal($("selection-original").classList.hidden,false);
 assert.equal($("selection-original").disabled,false);
 assert.equal($("selection-organize").classList.hidden,true);
 assert.equal($("selection-download").disabled,false);
 S.selected.add(2);S.items.push({id:2,available:true});context.updateSelection();
 assert.equal($("selection-original").disabled,true);
 S.folder={id:55};context.updateSelection();
 assert.equal($("selection-original").classList.hidden,true);
 assert.equal($("selection-organize").classList.hidden,false);
 assert.ok(markup.includes('id="selection-original"'));
});

test("favorite hold menu has precisely the two requested user choices",()=>{
 const menuCode=between(tools,"function mediaActions(item){","async function loadCommercial(){");
 const widgets=new Map();
 function $(id){
  if(!widgets.has(id))widgets.set(id,{children:[],classList:{remove(){}},
    replaceChildren(){this.children=[];},append(child){this.children.push(child);}});
  return widgets.get(id);
 }
 const item={id:77,available:true},located=[];
 const S={me:{admin:false},folder:{favorites:true},selected:new Set()};
 const context={S,$,selectionLimit:()=>10,choice:(label,run)=>({label,run}),
  closeActions:()=>{},downloadIds:async()=>{},movePicker:()=>{},
  locateOriginal:m=>located.push(m.id)};
 vm.createContext(context);vm.runInContext(menuCode,context);
 context.mediaActions(item);
 assert.deepEqual($("media-context-actions").children.map(x=>x.label),
  ["↓ Descargar al teléfono","⌕ Mostrar en galería"]);
 $("media-context-actions").children[1].run();
 assert.deepEqual(located,[77]);
 S.folder={id:4};
 context.mediaActions(item);
 assert.equal($("media-context-actions").children.length,3);
});
