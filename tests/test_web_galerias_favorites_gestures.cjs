"use strict";
const {test}=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");

const js=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");
const tools=fs.readFileSync(__dirname+"/../web_galerias/admin_tools.js","utf8");
const html=fs.readFileSync(__dirname+"/../web_galerias/index.html","utf8");
const css=fs.readFileSync(__dirname+"/../web_galerias/style.css","utf8");
function block(text,first,last){
 const a=text.indexOf(first),b=text.indexOf(last,a);
 assert.ok(a>=0&&b>a,"Missing UI function: "+first);
 return text.slice(a,b);
}
function nodes(){
 const items=new Map();
 return id=>{
  if(!items.has(id)){
   const classes=new Set();
   items.set(id,{
    textContent:"",disabled:false,
    classList:{
     toggle(k,force){if(force)classes.add(k);else classes.delete(k)},
     contains:k=>classes.has(k)
    }
   });
  }
  return items.get(id);
 };
}
test("two gallery taps add a heart but never remove the favorite",async()=>{
 const $=nodes(),calls=[],notices=[],opened=[],timers=new Map();
 let seq=0,rendered=0;
 const item={id:51,favorite:false};
 const S={folder:{id:7},items:[item],epoch:5,selection:false,gridTap:null,
          favoritesPending:null,collectionsAt:10,viewer:0};
 $("viewer").classList.toggle("hidden",true);
 const ctx={
  $,S,Date,Set,Number,tg:null,
  setTimeout:fn=>{const id=++seq;timers.set(id,fn);return id},
  clearTimeout:id=>timers.delete(id),
  api:async(op,payload)=>{calls.push([op,payload]);return{ok:true}},
  refreshCollections:async()=>{},
  renderItems:()=>{rendered++},
  updateViewerFavorite:()=>{},
  notice:v=>notices.push(v),
  openViewer:n=>opened.push(n)
 };
 vm.createContext(ctx);
 vm.runInContext(block(js,"async function markMediaFavorite(","function renderFolders(){"),ctx);
 ctx.galleryTileTap(item,0);
 assert.equal(opened.length,0,"First tap must not open the viewer before the double-tap window");
 ctx.galleryTileTap(item,0);
 await Promise.resolve();await Promise.resolve();
 assert.equal(timers.size,0);
 assert.equal(opened.length,0,"A double tap must not open the viewer");
 assert.equal(item.favorite,true);
 assert.equal(calls.length,1);
 assert.equal(calls[0][0],"favorite-set");
 assert.equal(calls[0][1].media,51);
 assert.equal(calls[0][1].favorite,true);
 assert.ok(rendered>=1,"The tile must draw its heart immediately");
 await ctx.markMediaFavorite(item);
 assert.equal(calls.length,1,"Repeating a double tap must never remove a heart");
 assert.equal(S.collectionsAt,0);
 assert.ok(notices.some(x=>x.includes("Mis favoritos")));
});

test("a single tap still opens the selected file normally",()=>{
 const $=nodes(),timers=new Map(),opened=[];
 let seq=0;
 const item={id:14,favorite:false},S={folder:{id:8},items:[item],
   epoch:2,selection:false,gridTap:null};
 const ctx={
  $,S,Date,Set,Number,tg:null,
  setTimeout:fn=>{timers.set(++seq,fn);return seq},
  clearTimeout:id=>timers.delete(id),
  renderItems:()=>{},updateViewerFavorite:()=>{},
  api:async()=>({ok:true}),refreshCollections:async()=>{},notice:()=>{},
  openViewer:index=>opened.push(index)
 };
 vm.createContext(ctx);
 vm.runInContext(block(js,"async function markMediaFavorite(","function renderFolders(){"),ctx);
 ctx.galleryTileTap(item,0);
 assert.equal(opened.length,0);
 for(const timer of timers.values())timer();
 assert.deepEqual(opened,[0]);
});

test("favorites long press opens actions instead of starting drag or selection",()=>{
 const events={},timers=[];
 const tile={addEventListener:(name,fn)=>{events[name]=fn},
   classList:{remove(){},add(){}},dataset:{}};
 const item={id:123,kind:"video"};
 const hits=[];
 const S={folder:{favorites:true},selected:new Set(),selection:false,
   search:"",drag:null,suppressClick:null};
 const ctx={
  S,tg:null,window:{CubanitasTools:{mediaActions:media=>hits.push(media.id)}},
  setTimeout:fn=>{timers.push(fn);return timers.length},
  clearTimeout:()=>{},clearGridTap:()=>{},
  selectedDragIds:()=>[123],selectionLimit:()=>10,notice:()=>{},
  updateSelection:()=>{},showDragPreview:()=>{},
  requestAnimationFrame:()=>0,dragMenuScroll:()=>{},
  showDragPreview:()=>{},finishDrag:()=>{},hideDragPreview:()=>{}
 };
 vm.createContext(ctx);
 vm.runInContext(block(js,"function attachLongPress(","function fallback("),ctx);
 ctx.attachLongPress(tile,item,0);
 events.pointerdown({pointerType:"touch",button:0,clientX:30,clientY:40,pointerId:1});
 assert.ok(timers.length>0);
 timers[0]();
 assert.deepEqual(hits,[123]);
 assert.equal(S.selection,false);
 assert.equal(S.drag,null);
 assert.equal(S.suppressClick,123);
});

test("checkmark selection in favorites exposes Download and Show in gallery",()=>{
 const $=nodes();
 const S={folder:{favorites:true},items:[{id:123,available:true}],
          selected:new Set([123]),selection:true,me:{admin:false}};
 const ctx={$ ,S,selectionLimit:()=>10};
 vm.createContext(ctx);
 vm.runInContext(block(js,"function updateSelection(){","function toggleSelection("),ctx);
 ctx.updateSelection();
 assert.equal($("selection-download").disabled,false);
 assert.equal($("selection-show-original").disabled,false);
 assert.equal($("selection-organize").classList.contains("hidden"),true);
 assert.equal($("selection-show-original").classList.contains("hidden"),false);
 assert.ok(html.includes('id="selection-show-original"'));
 assert.ok(css.includes("touch-action:manipulation"));
});

test("favorite source action offers each origin separately for mixed selections",()=>{
 assert.ok(tools.includes("function showFavoriteOriginals(){"));
 assert.ok(tools.includes("if(S.folder?.favorites)"));
 assert.ok(tools.includes('choice("↓ Descargar al teléfono"'));
 assert.ok(tools.includes('choice("⌕ Mostrar en galería"'));
 assert.ok(tools.includes("showFavoriteOriginals,"));
 assert.ok(js.includes('window.CubanitasTools?.showFavoriteOriginals?.()'));
 assert.ok(js.includes('S.folder?.personal||S.folder?.favorites||S.search.trim()'));
});
