"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");

const js=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");
const html=fs.readFileSync(__dirname+"/../web_galerias/index.html","utf8");
const start=js.indexOf("let videoPriorityTimer=null");
const finish=js.indexOf("async function refreshAdminViewerMedia(",start);
assert.ok(start>0&&finish>start);

function simulate(){
 const item={id:55,kind:"video",mime:"video/mp4",available:false,view:null,thumb:"/thumb"};
 const state={items:[item],viewer:0};
 const timers=[],ops=[],notices=[],renders=[];
 const indicator={textContent:"",classList:{add:()=>{},toggle:()=>{}}};
 const nodes={
  "viewer":{classList:{contains:()=>false}},
  "viewer-priority-status":indicator,
  "view-video":{hidden:false,load:()=>renders.push("load"),play:()=>{renders.push("play");return Promise.resolve();}}
 };
 const context={
  S:state,$:id=>nodes[id],Date,setTimeout:(callback)=>{timers.push(callback);return timers.length;},
  clearTimeout:()=>{},showViewerMedia:()=>renders.push("render"),
  api:async(op,params)=>{
   ops.push({op,params});
   return op==="media-priority"?
      {available:false,priority_state:"queued"}:
      {available:true,kind:"video",mime:"video/mp4",view:"/web-galerias/media/55/view?t=secret"};
  }
 };
 vm.createContext(context);
 vm.runInContext(js.slice(start,finish),context);
 return {context,item,timers,ops,notices,renders,indicator};
}
async function settle(){
 await new Promise(resolve=>setImmediate(resolve));
}

test("a tapped unavailable video queues one urgent backup then becomes playable",async()=>{
 const env=simulate();
 env.context.beginVideoPriority(env.item);
 await settle();
 assert.equal(env.ops.length,1);
 assert.equal(env.ops[0].op,"media-priority");
 assert.equal(env.ops[0].params.media,55);
 assert.equal(env.timers.length,1);
 env.timers.shift()();
 await settle();
 assert.equal(env.ops.length,2);
 assert.equal(env.ops[1].op,"media-status");
 assert.equal(env.item.available,true);
 assert.ok(env.item.view.includes("/view?t="));
 assert.ok(env.renders.includes("render"));
 assert.ok(env.renders.includes("load"));
 assert.ok(env.renders.includes("play"));
});
test("closing viewer invalidates polling even if a callback fires later",async()=>{
 const env=simulate();
 env.context.beginVideoPriority(env.item);
 await settle();
 const next=env.timers.shift();
 env.context.stopVideoPriority();
 next();
 await settle();
 assert.equal(env.ops.length,1);
});
test("video-specific controls exist and admin is routed through selected user's permissions",()=>{
 assert.ok(html.includes('id="viewer-priority-status"'));
 assert.ok(js.includes('"media-priority":"admin-view-media-priority"'));
 assert.ok(js.includes('beginVideoPriority(S.items[index])'));
 assert.ok(js.includes('beginVideoPriority(S.items[i])'));
 assert.ok(js.includes('if(ADMIN_ACCOUNT_VIEW)void refreshAdminViewerMedia(i,S.items[i])'));
});
