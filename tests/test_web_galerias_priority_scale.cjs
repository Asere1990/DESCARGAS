"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");

const js=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");
const css=fs.readFileSync(__dirname+"/../web_galerias/style.css","utf8");
const start=js.indexOf("let videoPriorityTimer=null");
const finish=js.indexOf("async function refreshAdminViewerMedia(",start);
assert.ok(start>0&&finish>start);

function viewer(){
 const item={id:55,kind:"video",mime:"video/mp4",available:false,
             view:null,thumb:"/thumb"};
 const S={items:[item],viewer:0};
 const timers=[],states=[],ops=[],renders=[];
 const indicator={textContent:"",classList:{add:()=>{},toggle:()=>{}}};
 const nodes={
  viewer:{classList:{contains:()=>false}},
  "viewer-priority-status":indicator,
  "view-video":{hidden:false,load:()=>renders.push("load"),
                play:()=>{renders.push("play");return Promise.resolve();}}
 };
 const payloads=[
  {priority_state:"queued",available:false,queue_position:23,queue_total:250},
  {priority_state:"running",available:false,progress_percent:37,
   received_bytes:3700000,expected_bytes:10000000},
  {priority_state:"running",available:false,progress_percent:99,
   received_bytes:10000000,expected_bytes:10000000},
  {priority_state:"ready",available:true,
   view:"/web-galerias/media/55/view?t=temporary",kind:"video",mime:"video/mp4"}
 ];
 const context={
  S,$:id=>nodes[id],Date,Number,Math,
  setTimeout:(callback,ms)=>{timers.push({callback,ms});return timers.length;},
  clearTimeout:()=>{},
  showViewerMedia:()=>renders.push("render"),
  api:async(op,params)=>{
   ops.push([op,params]);
   return payloads.shift();
  },
 };
 vm.createContext(context);
 vm.runInContext(js.slice(start,finish),context);
 return {context,item,timers,indicator,ops,renders};
}
const settled=async()=>new Promise(resolve=>setImmediate(resolve));

test("pending video shows only 0% (never internal queue numbers) and polls at a restrained rate",async()=>{
 const v=viewer();
 v.context.beginVideoPriority(v.item);
 await settled();
 assert.equal(v.indicator.textContent,"Cargando video · 0 %");
 assert.equal(v.ops[0][0],"media-priority");
 assert.ok(v.timers[0].ms>=8000);
});

test("center overlay advances from 37 to 99, then verified 100 and playback",async()=>{
 const v=viewer();
 v.context.beginVideoPriority(v.item);
 await settled();
 v.timers.shift().callback();
 await settled();
 assert.equal(v.indicator.textContent,"Cargando video · 37 %");
 assert.equal(v.timers[0].ms,2000);
 v.timers.shift().callback();
 await settled();
 assert.equal(v.indicator.textContent,"Cargando video · 99 %");
 v.timers.shift().callback();
 await settled();
 assert.equal(v.indicator.textContent,"Cargando video · 100 %");
 assert.equal(v.item.available,true);
 assert.ok(v.renders.includes("render"));
 assert.ok(v.renders.includes("load"));
 assert.ok(v.renders.includes("play"));
});

test("small priority text is placed at the center, never beneath bottom buttons",()=>{
 assert.ok(css.includes("#viewer .viewer-stage{position:relative}"));
 const block=css.slice(css.indexOf("#viewer .viewer-priority-status{"));
 assert.ok(/top:44%/.test(block));
 assert.ok(/font-size:12px/.test(block));
 assert.ok(/bottom:auto/.test(block));
 assert.ok(!/bottom:64px/.test(block));
 assert.ok(js.includes("progress_percent"));
});
