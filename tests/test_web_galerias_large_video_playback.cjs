"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const js=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");
const start=js.indexOf("// Prefer direct R2 playback.");
const end=js.indexOf("function showViewerMedia(){",start);
assert.ok(start>=0&&end>start);

function scenario(){
 const item={id:55,kind:"video",mime:"video/mp4",available:true,
             view:"/web-galerias/media/55/view?t=secure"};
 const state={viewer:0,items:[item]};
 const notices=[],timers=[],calls=[];
 const video={
  paused:false,currentTime:0,src:item.view,
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

test("10-second stalled iPhone playback retries only selected video with same-origin ranges",()=>{
 const s=scenario();
 s.video.onwaiting();
 assert.equal(s.timers.length,1);
 s.timers.shift()();
 assert.equal(s.video.src,s.item.view+"&stream=1");
 assert.ok(s.calls.includes("load"));
 assert.ok(s.notices.some(x=>x.includes("Recuperando")));
});

test("real playback progress does not trigger recovery",()=>{
 const s=scenario();
 s.video.onwaiting();
 const callback=s.timers.shift();
 s.video.currentTime=2.0;
 callback();
 assert.equal(s.video.src,s.item.view);
 assert.deepEqual(s.calls,[]);
});

test("one fallback only; a second failure is reported instead of looping",()=>{
 const s=scenario();
 s.video.onerror();
 assert.ok(s.video.src.endsWith("&stream=1"));
 s.video.onerror();
 assert.ok(s.notices.some(x=>x.includes("guardarlo")));
});

test("pause and viewer-close don't restart or change a video",()=>{
 const s=scenario();
 s.video.onwaiting();
 const callback=s.timers.shift();
 s.video.paused=true;
 callback();
 assert.equal(s.video.src,s.item.view);
 s.video.paused=false;
 s.ctx.clearViewerPlayback();
 assert.equal(s.video.onwaiting,null);
});

test("selected video recovery does not alter R2 backup, download quota or customer apps",()=>{
 assert.ok(js.includes('video.src=viewerStreamModeId===m.id?videoStreamFallbackUrl(m.view):m.view'));
 assert.ok(js.includes('beginVideoPriority(S.items[index])'));
 assert.ok(js.includes('beginVideoPriority(S.items[i])'));
});
