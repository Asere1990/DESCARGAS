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
 assert.equal($("quota-connect").classList.contains("hidden"),false);
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
test("free user sees stacked quota offers with price only on Stripe",()=>{
 assert.ok(html.includes(">Descargar 200 archivos diarios</button>"));
 assert.ok(html.includes(">♾ Descargas ilimitadas</button>"));
 assert.ok(!html.includes("+$1.99/mes"));
 assert.ok(css.includes(".quota-upgrade-actions{display:flex;flex-direction:column;"));
 assert.ok(css.includes(".quota-upgrade-paid{"));
});
test("200/day upgrade goes to the original DESCARGAS Mini App with same-bot signature",()=>{
 const $=fakeNodes(),links=[],bots=[],saved=[];
 const signature="user=%7B%22id%22%3A7%7D&auth_date=123&hash=signed";
 const S={me:{quota_link_miniapp:"https://descargas-eu1x.onrender.com/",
              quota_link_bot:"https://t.me/Descargas_Robot"}};
 const tg={initData:signature,version:"8.0",platform:"ios",openTelegramLink:url=>bots.push(url)};
 const ctx={$,S,tg,BROWSER_ADMIN:false,URL,URLSearchParams,String,
   window:{location:{assign:url=>links.push(url)}},
   notice:msg=>{throw Error(msg)},saveGalleryReturnPosition:()=>saved.push(true)};
 vm.createContext(ctx);
 vm.runInContext(part('if($("quota-connect"))','if($("quota-plus"))'),ctx);
 $("quota-connect").onclick();
 assert.equal(bots.length,0);
 assert.equal(links.length,1);
 const opened=new URL(links[0]);
 assert.equal(opened.origin,"https://descargas-eu1x.onrender.com");
 assert.equal(opened.pathname,"/");
 assert.equal(opened.searchParams.has("tgWebAppData"),false);
 assert.equal(new URLSearchParams(opened.hash.slice(1)).get("tgWebAppData"),signature);
 assert.equal(saved.length,1);
 links.length=0;S.me.quota_link_miniapp=null;
 $("quota-connect").onclick();
 assert.equal(links.length,0);
 assert.equal(bots[0],"https://t.me/Descargas_Robot");
});
test("Support bubble opens the signed Mi Cuenta chat in the same WebView",async()=>{
 const $=fakeNodes(),links=[],native=[],requests=[];
 const S={me:{allowed:true}};
 const signed="user=%7B%22id%22%3A7%7D&auth_date=1794500000&hash=verified";
 const ctx={
  $,S,BROWSER_ADMIN:false,URL,URLSearchParams,
  api:async(op)=>{requests.push(op);return{
   url:"https://t.me/CustomerSupportRobot?startapp=chat",
   web_url:"https://customer-service-uimy.onrender.com/cubanas"}},
  tg:{initData:signed,version:"8.0",platform:"ios",
      openTelegramLink:url=>native.push(url)},
  window:{location:{assign:url=>links.push(url)}},
  String,Error,RegExp,
  notice:()=>{throw Error("Unexpected error")}
 };
 vm.createContext(ctx);
 vm.runInContext(part("// Swiftgram can ignore","function mountSupportBubble(){"),ctx);
 await ctx.openSupportChat();
 assert.deepEqual(requests,["chat-link"]);
 assert.equal(native.length,0);
 assert.equal(links.length,1);
 const opened=new URL(links[0]);
 assert.equal(opened.origin,"https://customer-service-uimy.onrender.com");
 assert.equal(opened.pathname,"/cubanas");
 assert.equal(opened.searchParams.get("tgWebAppStartParam"),"chat");
 assert.equal(new URLSearchParams(opened.hash.slice(1)).get("tgWebAppData"),signed);
 assert.equal(opened.searchParams.get("tgWebAppData"),null);
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
 vm.runInContext(part("function mountSupportBubble(){",'$("open-favorites")'),ctx);
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
