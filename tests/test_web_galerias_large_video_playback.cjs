"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const js=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");
const html=fs.readFileSync(__dirname+"/../web_galerias/index.html","utf8");
const start=js.indexOf("// Play video through bounded same-origin HTTP ranges");
const end=js.indexOf("function showViewerMedia(){",start);
assert.ok(start>=0&&end>start);

function scenario(){
 const item={id:55,kind:"video",mime:"video/mp4",available:true,
             view:"/web-galerias/media/55/view?t=secure"};
 const state={viewer:0,items:[item]};
 const notices=[],timers=[],calls=[];
 const video={
  paused:false,currentTime:0,src:item.view+"&stream=1",
  load:()=>calls.push("load"),
  play:()=>{calls.push("play");return Promise.resolve();}
 };
 const nodes={
  "view-video":video,
  "viewer":{classList:{contains:()=>false}}
 };
 const ctx={
  S:state,$:name=>nodes[name],Number,Promise,
  setTimeout:callback=>{timers.push(callback);return timers.length;},
  clearTimeout:()=>{},
  priorityText:text=>notices.push(text)
 };
 vm.createContext(ctx);
 vm.runInContext(js.slice(start,end),ctx);
 ctx.clearViewerPlayback();
 ctx.prepareViewerPlayback(item);
 return {ctx,item,video,calls,timers,notices};
}

test("iPhone and Android start with authorized same-origin streaming, not R2 redirect",()=>{
 const s=scenario();
 assert.equal(s.video.src,s.item.view+"&stream=1");
 assert.equal(s.ctx.videoStreamUrl(s.item.view),s.item.view+"&stream=1");
 assert.ok(js.includes('video.src=viewerDirectModeId===m.id?m.view:videoStreamUrl(m.view)'));
 assert.ok(!js.includes("navigator.userAgent"));
 assert.ok(html.includes("app.js?v=playback-range-android-ios-20261113-v2"));
});

test("10-second stalled video retries direct R2 once on both operating systems",()=>{
 const s=scenario();
 s.video.onwaiting();
 assert.equal(s.timers.length,1);
 s.timers.shift()();
 assert.equal(s.video.src,s.item.view);
 assert.ok(s.calls.includes("load"));
 assert.ok(s.notices.some(x=>x.includes("Probando otra conexión")));
});

test("real playback progress keeps range stream active",()=>{
 const s=scenario();
 s.video.onwaiting();
 const callback=s.timers.shift();
 s.video.currentTime=2.0;
 callback();
 assert.equal(s.video.src,s.item.view+"&stream=1");
 assert.deepEqual(s.calls,[]);
});

test("after one retry a second error is shown rather than looping",()=>{
 const s=scenario();
 s.video.onerror();
 assert.equal(s.video.src,s.item.view);
 s.video.onerror();
 assert.ok(s.notices.some(x=>x.includes("guardarlo")));
});

test("pause and closing viewer don't restart or change a video",()=>{
 const s=scenario();
 s.video.onwaiting();
 const callback=s.timers.shift();
 s.video.paused=true;
 callback();
 assert.equal(s.video.src,s.item.view+"&stream=1");
 s.video.paused=false;
 s.ctx.clearViewerPlayback();
 assert.equal(s.video.onwaiting,null);
});

test("priority and download features remain unchanged",()=>{
 assert.ok(js.includes('beginVideoPriority(S.items[index])'));
 assert.ok(js.includes('beginVideoPriority(S.items[i])'));
 assert.ok(js.includes('if(typeof clearViewerPlayback==="function")clearViewerPlayback();'));
});
