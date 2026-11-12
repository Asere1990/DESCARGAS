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
 const ctx={$,S,pretty:n=>String(n),Number,Math,ADMIN_ACCOUNT_VIEW:false};
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
test("fixed blue support button opens verified Customer chat in the same WebView",async()=>{
 const $=fakeNodes(),links=[],native=[],requests=[];
 const S={me:{allowed:true}};
 const signed="user=%7B%22id%22%3A7%7D&auth_date=1794500000&hash=verified";
 const ctx={
  $,S,BROWSER_ADMIN:false,ADMIN_ACCOUNT_VIEW:false,URL,URLSearchParams,
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
 vm.runInContext(part("// Swiftgram can ignore",'if($("quota-connect"))'),ctx);
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
 assert.equal($("open-support-chat").disabled,false);
 ctx.updateSupportVisibility("library");
 assert.equal($("open-support-chat").classList.contains("hidden"),false);
 assert.equal($("go-intro").classList.contains("hidden"),true);
 S.me.admin=true;
 ctx.updateSupportVisibility("library");
 assert.equal($("open-support-chat").classList.contains("hidden"),true);
 assert.equal($("go-intro").classList.contains("hidden"),false);
 S.me.admin=false;
 ctx.updateSupportVisibility("library");
 assert.equal($("open-support-chat").classList.contains("hidden"),false);
 assert.equal($("go-intro").classList.contains("hidden"),true);
});
test("support is fixed and there is no draggable bubble",()=>{
 assert.ok(html.includes('id="open-support-chat"'));
 assert.ok(html.includes('Chat con soporte</button>'));
 assert.ok(!html.includes('id="support-bubble"'));
 assert.ok(html.includes('id="go-intro"'));
 assert.ok(!js.includes("mountSupportBubble()"));
 assert.ok(!js.includes("wg_customer_support_button_v1"));
 assert.ok(js.includes('$("open-support-chat").onclick=()=>openSupportChat()'));
});
test("customer support and upgrade components are independent of existing payment buttons",()=>{
 assert.ok(html.includes('id="open-support-chat"'));
 assert.ok(html.includes('id="quota-plus"'));
 assert.ok(html.includes('id="quota-connect"'));
 assert.ok(js.includes('api("quota-plus-checkout")'));
 assert.ok(js.includes('api("checkout")'));
 assert.ok(js.includes('api("chat-link")'));
 assert.ok(css.includes(".mobile-action-primary"));
});
