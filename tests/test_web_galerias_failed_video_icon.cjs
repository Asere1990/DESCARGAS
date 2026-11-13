"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const js=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");
const html=fs.readFileSync(__dirname+"/../web_galerias/index.html","utf8");
const start=js.indexOf("// Only a confirmed failed playback temporarily changes");
const end=js.indexOf("function showDragPreview(",start);
const fbStart=js.indexOf("function fallback(b,m){",end);
const fbEnd=js.indexOf("function updateSelection(){",fbStart);
assert.ok(start>=0&&end>start&&fbStart>=0&&fbEnd>fbStart);

function element(tag){
 return {
  tagName:tag,className:"",dataset:{},attributes:{},children:[],
  setAttribute(key,val){this.attributes[key]=val;},
  append(child){this.children.push(child);},
  prepend(child){this.children.unshift(child);},
  remove(){this.removed=true;}
 };
}
function scene({large=true,thumb="/thumb.jpg",ready=true}={}){
 const item={id:88,size:large?600*1024*1024:15*1024*1024,
             kind:"video",mime:"video/mp4",available:ready,
             thumb,view:ready?"/web-galerias/media/88/view?t=valid":null};
 const S={items:[item],viewer:0,selected:new Set()};
 const grid=element("grid");
 grid.replaceChildren=()=>{grid.children=[];};
 const timers=[],ops=[],viewers=[];
 let serverStatus={available:true,view:item.view,thumb};
 const ctx={
  S,Map,Number,Date,Math,Promise,
  window:{matchMedia:()=>({matches:false})},
  document:{createElement:element},
  $:id=>id==="album-grid"?grid:
        id==="viewer"?{classList:{contains:()=>true}}:null,
  setTimeout:(fn,ms)=>{const next={fn,ms};timers.push(next);return timers.length;},
  clearTimeout:()=>{},
  api:async(op,args)=>{ops.push([op,args]);return op==="media-status"?serverStatus:{ok:true};},
  showViewerMedia:()=>viewers.push("viewer"),
  attachLongPress:()=>{},
  onMediaTileClick:()=>{},
  viewerDirectModeId:null
 };
 vm.createContext(ctx);
 vm.runInContext(js.slice(start,end)+"\n"+js.slice(fbStart,fbEnd),ctx);
 const icon=()=>grid.children[0]?.children.some(x=>x.className==="video-mark");
 const photoLabel=()=>grid.children[0]?.attributes["aria-label"];
 const fallbackText=()=>grid.children[0]?.children.find(x=>x.className==="fallback")?.textContent;
 return {ctx,S,item,grid,timers,ops,viewers,icon,photoLabel,fallbackText,
         setStatus:x=>{serverStatus=x;}};
}
const flush=async()=>new Promise(ok=>setImmediate(ok));

test("normal video keeps play icon and its existing priority, no early hide",()=>{
 const v=scene();
 v.ctx.renderItems();
 assert.equal(v.icon(),true);
 assert.equal(v.photoLabel(),"Abrir video");
 assert.deepEqual(v.ops,[]);
});

test("confirmed error hides video marker and opens thumbnail like a photo",()=>{
 const v=scene();
 v.ctx.renderItems();
 v.ctx.markVideoTemporarilyFailed(v.item);
 assert.equal(v.icon(),false);
 assert.equal(v.photoLabel(),"Abrir foto");
 assert.deepEqual(v.ops.map(x=>x[0]),["media-playback-failed"]);
 assert.equal(v.timers[0].ms,180000);
});

test("even a video without a thumbnail does not show triangle after confirmed failure",()=>{
 const v=scene({thumb:null});
 v.ctx.markVideoTemporarilyFailed(v.item);
 assert.equal(v.icon(),false);
 assert.equal(v.fallbackText(),"▧");
});

test("failed pending video keeps the existing queue; no redundant failure report",()=>{
 const v=scene({large:false,ready:false});
 v.ctx.markVideoTemporarilyFailed(v.item,false);
 assert.equal(v.icon(),false);
 assert.equal(v.timers[0].ms,90000);
 assert.deepEqual(v.ops,[]);
});

test("ready video automatically regains play icon after verified state refresh",async()=>{
 const v=scene();
 v.ctx.markVideoTemporarilyFailed(v.item);
 const recover=v.timers[0];
 recover.fn();
 await flush();
 assert.equal(v.icon(),true);
 assert.equal(v.photoLabel(),"Abrir video");
 assert.deepEqual(v.ops.map(x=>x[0]),["media-playback-failed","media-status"]);
});

test("still pending or transient errors keep thumbnail while existing recovery runs",async()=>{
 const v=scene();
 v.ctx.markVideoTemporarilyFailed(v.item);
 v.setStatus({available:false,view:null,thumb:"/thumb.jpg"});
 v.timers[0].fn();
 await flush();
 assert.equal(v.icon(),false);
 assert.equal(v.item.available,false);
 assert.equal(v.timers.at(-1).ms,30000);
 v.setStatus({available:true,view:"/web-galerias/media/88/view?t=new",thumb:"/thumb.jpg"});
 v.timers.at(-1).fn();
 await flush();
 assert.equal(v.icon(),true);
});

test("no OS-specific paths and new JS resource URL invalidates Telegram WebView caches",()=>{
 assert.ok(!js.includes("navigator.userAgent"));
 assert.ok(html.includes("app.js?v=playback-range-android-ios-20261113-v2-playback-alert"));
 assert.ok(js.includes("if(state===\"waiting\"&&result.priority_failure)"));
 assert.ok(js.includes("markVideoTemporarilyFailed(expected,false)"));
});
