"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const js=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");
const html=fs.readFileSync(__dirname+"/../web_galerias/index.html","utf8");
const css=fs.readFileSync(__dirname+"/../web_galerias/style.css","utf8");
function part(a,b){
 const start=js.indexOf(a),end=js.indexOf(b,start);
 assert.ok(start>=0&&end>start,"Missing "+a);
 return js.slice(start,end);
}
function fakeNodes(){
 const map=new Map();
 return id=>{
  if(!map.has(id)){
   const classes=new Set();
   map.set(id,{
    textContent:"",disabled:false,style:{},
    classList:{toggle(k,on){if(on)classes.add(k);else classes.delete(k)},
               contains(k){return classes.has(k)}}
   });
  }
  return map.get(id);
 };
}
test("50 and 200 daily limits show accurately without changing the 10-file batch",()=>{
 const $=fakeNodes();
 const S={me:{quota_unlimited:false,quota_tier:"basic",
   quota_daily_limit:50,remaining:37,quota_plus_available:true}};
 const ctx={$,S,pretty:n=>String(n),Number,Math};
 vm.createContext(ctx);
 vm.runInContext(part("function updateQuotaStatus(){","function watermarkFromEditor(){"),ctx);
 ctx.updateQuotaStatus();
 assert.equal($("quota-count").textContent,"37 de 50");
 assert.equal($("quota-fill").style.width,"74%");
 assert.equal($("quota-plus").classList.contains("hidden"),false);
 S.me.quota_tier="linked";S.me.quota_daily_limit=200;S.me.remaining=150;
 ctx.updateQuotaStatus();
 assert.equal($("quota-count").textContent,"150 de 200");
 assert.equal($("quota-connect").classList.contains("hidden"),true);
 S.me.quota_unlimited=true;S.me.quota_plus_active=true;S.me.remaining=null;
 ctx.updateQuotaStatus();
 assert.equal($("quota-count").textContent,"Ilimitadas");
 assert.equal($("quota-plus").classList.contains("hidden"),true);
 assert.ok(js.includes("S.me?.admin?100:10"));
});
test("chat link is authenticated, destination pinned, and never guessed",async()=>{
 const $=fakeNodes(),links=[],requests=[];
 const S={me:{allowed:true}};
 const ctx={$,S,BROWSER_ADMIN:false,
  api:async(op,payload)=>{requests.push(op);return{url:"https://t.me/CustomerSupportRobot?startapp=chat"}},
  tg:{openTelegramLink:url=>links.push(url)},
  window:{location:{assign:url=>links.push(url)}},
  String,Error,RegExp,
  notice:()=>{throw Error("Unexpected error")}
 };
 vm.createContext(ctx);
 vm.runInContext(part("let customerChatLink=null","function mountSupportBubble(){"),ctx);
 await ctx.openSupportChat();
 assert.deepEqual(requests,["chat-link"]);
 assert.deepEqual(links,["https://t.me/CustomerSupportRobot?startapp=chat"]);
 assert.equal($("support-bubble").disabled,false);
 ctx.updateSupportVisibility("album");
 assert.equal($("support-bubble").classList.contains("hidden"),false);
 ctx.updateSupportVisibility("admin");
 assert.equal($("support-bubble").classList.contains("hidden"),true);
});
test("draggable customer chat does not activate on a drag",()=>{
 const events={};const saved={};const hits=[];
 const button={
  offsetWidth:56,offsetHeight:56,style:{},
  addEventListener:(name,fn)=>events[name]=fn,
  getBoundingClientRect:()=>({left:Number.parseFloat(button.style.left)||0,
     top:Number.parseFloat(button.style.top)||0}),
  setPointerCapture:()=>{}
 };
 const ctx={
  $:()=>button,
  innerWidth:390,innerHeight:844,
  window:{addEventListener:()=>{}},
  localStorage:{getItem:()=>null,setItem:(k,v)=>saved[k]=v},
  Math,JSON,Number,parseFloat,
  setTimeout:()=>1,
  openSupportChat:()=>hits.push("open")
 };
 vm.createContext(ctx);
 vm.runInContext(part("function mountSupportBubble(){","$('open-favorites')"),ctx);
 // The trailing mountSupportBubble() call is included by the block.
 assert.ok(events.pointerdown);
 const oldX=button.style.left,oldY=button.style.top;
 events.pointerdown({pointerType:"touch",pointerId:2,clientX:265,clientY:480});
 events.pointermove({pointerId:2,clientX:145,clientY:240});
 events.pointerup({pointerId:2});
 events.click({preventDefault:()=>{}});
 assert.notEqual(button.style.left,oldX);
 assert.notEqual(button.style.top,oldY);
 assert.equal(hits.length,0,"Dragging must not open the customer chat");
 assert.ok(saved.wg_customer_support_button_v1);
 assert.ok(JSON.parse(saved.wg_customer_support_button_v1).x<.85);
});
test("customer support and upgrade components are independent of existing payment buttons",()=>{
 assert.ok(html.includes('id="support-bubble"'));
 assert.ok(html.includes('id="quota-plus"'));
 assert.ok(html.includes('id="quota-connect"'));
 assert.ok(js.includes('api("quota-plus-checkout")'));
 assert.ok(js.includes('api("checkout")'));
 assert.ok(js.includes('api("chat-link")'));
 assert.ok(css.includes(".support-bubble{"));
 assert.ok(css.includes("touch-action:none"));
});
