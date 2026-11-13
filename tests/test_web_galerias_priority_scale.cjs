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

function viewer(responses){
 const item={id:55,kind:"video",mime:"video/mp4",available:false,
             view:null,thumb:"/thumb"};
 const S={items:[item],viewer:0};
 const timers=[],states=[],ops=[],renders=[];
 let nextTimer=0;
 const indicator={textContent:"",classList:{add:()=>{},toggle:()=>{}}};
 const nodes={
  viewer:{classList:{contains:()=>false}},
  "viewer-priority-status":indicator,
  "view-video":{hidden:false,load:()=>renders.push("load"),
                play:()=>{renders.push("play");return Promise.resolve();}}
 };
 const defaults=[
  {priority_state:"queued",available:false,queue_position:23,queue_total:250},
  {priority_state:"running",available:false,progress_percent:37,
   received_bytes:3700000,expected_bytes:10000000},
  {priority_state:"running",available:false,progress_percent:99,
   received_bytes:10000000,expected_bytes:10000000},
  {priority_state:"ready",available:true,
   view:"/web-galerias/media/55/view?t=temporary",kind:"video",mime:"video/mp4"}
 ];
 const payloads=responses||defaults;
 const context={
  S,$:id=>nodes[id],Date,Number,Math,
  setTimeout:(callback,ms)=>{
   const id=++nextTimer;
   timers.push({id,callback,ms,active:true});
   return id;
  },
  clearTimeout:(id)=>{const timer=timers.find(t=>t.id===id);
   if(timer)timer.active=false;},
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
async function fire(v,predicate){
 const timer=v.timers.find(t=>t.active&&predicate(t));
 assert.ok(timer,"expected a matching active timer");
 timer.active=false;
 timer.callback();
 await settled();
}
async function flushPercentageTicks(v,limit=80){
 for(let count=0;count<limit;count++){
  if(v.indicator.textContent==="Cargando video · 100 %")return;
  if(!v.timers.some(t=>t.active&&t.ms<500))return;
  await fire(v,t=>t.ms<500);
 }
 throw Error("percentage ticks did not settle");
}

test("pending video shows only 0% (never internal queue numbers) and polls at a restrained rate",async()=>{
 const v=viewer();
 v.context.beginVideoPriority(v.item);
 await settled();
 assert.equal(v.indicator.textContent,"Cargando video · 0 %");
 assert.equal(v.ops[0][0],"media-priority");
 assert.ok(v.timers.some(t=>t.active&&t.ms>=8000));
});

test("intermediate byte samples animate through 37 and 99, then verified 100",async()=>{
 const v=viewer();
 v.context.beginVideoPriority(v.item);
 await settled();
 await fire(v,t=>t.ms>=8000); // 23rd place: intentionally low polling.
 await flushPercentageTicks(v);
 assert.equal(v.indicator.textContent,"Cargando video · 37 %");
 assert.ok(v.timers.some(t=>t.active&&t.ms===550));
 await fire(v,t=>t.ms===550);
 await flushPercentageTicks(v);
 assert.equal(v.indicator.textContent,"Cargando video · 99 %");
 await fire(v,t=>t.ms===550);
 assert.equal(v.item.available,true);
 // Playback begins immediately; the short visual progress catches up in parallel.
 assert.ok(v.renders.includes("render"));
 assert.ok(v.renders.includes("load"));
 assert.ok(v.renders.includes("play"));
 await fire(v,t=>t.ms<=750);
 assert.equal(v.indicator.textContent,"Cargando video · 100 %");
});
test("one of 250 urgent videos detects ready promptly and animates an immediate 0-to-100 sample",async()=>{
 const v=viewer([
  {priority_state:"queued",available:false,queue_position:1,queue_total:250},
  {priority_state:"ready",available:true,
   view:"/web-galerias/media/55/view?t=temporary",kind:"video",mime:"video/mp4"}
 ]);
 v.context.beginVideoPriority(v.item);
 await settled();
 assert.equal(v.indicator.textContent,"Cargando video · 0 %");
 assert.ok(v.timers.some(t=>t.active&&t.ms===800));
 await fire(v,t=>t.ms===800);
 assert.equal(v.item.available,true);
 assert.ok(v.renders.includes("play"),"playback must not wait for animation");
 await fire(v,t=>t.ms<500);
 assert.notEqual(v.indicator.textContent,"Cargando video · 0 %");
 assert.notEqual(v.indicator.textContent,"Cargando video · 100 %");
 await flushPercentageTicks(v);
 assert.equal(v.indicator.textContent,"Cargando video · 100 %");
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
