"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const root=__dirname+"/../web_galerias/";
const source=fs.readFileSync(root+"app.js","utf8");
const adminTools=fs.readFileSync(root+"admin_tools.js","utf8");
const accountServer=fs.readFileSync(root+"admin_account.py","utf8");
function extract(start,end){
 const a=source.indexOf(start),b=source.indexOf(end,a);
 assert.ok(a>=0&&b>a,start);return source.slice(a,b);
}
function fakeDom(){
 const store=new Map();
 return id=>{
  if(!store.has(id)){
   const classes=new Set();
   store.set(id,{textContent:"",style:{},classList:{
    toggle(name,active){if(active)classes.add(name);else classes.delete(name);},
    contains(name){return classes.has(name);}
   }});
  }
  return store.get(id);
 };
}
const makeCtx=me=>{
 const $=fakeDom();
 const ctx={S:{me},$,Number,Math,ADMIN_ACCOUNT_VIEW:true,
             pretty:n=>Number(n).toLocaleString("es-ES")};
 vm.createContext(ctx);
 vm.runInContext(extract("function updateQuotaStatus(){",
                       "function watermarkFromEditor(){"),ctx);
 return{ctx,$};
};
test("exact customer's 50/day tier shows BOTH original upgrade buttons in admin account",()=>{
 const {ctx,$}=makeCtx({admin:false,admin_view:true,admin_controls:true,
  quota_tier:"basic",quota_daily_limit:50,quota_unlimited:false,
  quota_plus_available:true,remaining:21});
 ctx.updateQuotaStatus();
 assert.equal($("quota-count").textContent,"21 de 50");
 assert.equal($("quota-connect").classList.contains("hidden"),false);
 assert.equal($("quota-plus").classList.contains("hidden"),false);
});
test("exact customer's 200/day tier retains its original upgrade controls",()=>{
 const {ctx,$}=makeCtx({admin:false,admin_view:true,admin_controls:true,
  quota_tier:"linked",quota_daily_limit:200,quota_unlimited:false,
  quota_plus_available:true,remaining:180});
 ctx.updateQuotaStatus();
 assert.equal($("quota-count").textContent,"180 de 200");
 assert.equal($("quota-connect").classList.contains("hidden"),true);
 assert.equal($("quota-plus").classList.contains("hidden"),false);
});
test("exact customer's unlimited tier remains unlimited with no purchase buttons",()=>{
 const {ctx,$}=makeCtx({admin:false,admin_view:true,admin_controls:true,
  quota_tier:"unlimited",quota_daily_limit:null,quota_unlimited:true,
  quota_plus_active:true,quota_plus_available:true,remaining:null});
 ctx.updateQuotaStatus();
 assert.equal($("quota-count").textContent,"Ilimitadas");
 assert.equal($("quota-connect").classList.contains("hidden"),true);
 assert.equal($("quota-plus").classList.contains("hidden"),true);
});
test("extra moderation tools do not turn customer into an admin profile",()=>{
 assert.ok(source.includes('const selectionLimit=()=>(S.me?.admin||S.me?.admin_controls)?100:10'));
 assert.ok(source.includes('$("admin-open").classList.toggle("hidden",!S.me.admin||ADMIN_ACCOUNT_VIEW)'));
 assert.ok(source.includes('$("viewer-admin-delete").classList.toggle("hidden",!(S.me?.admin||S.me?.admin_controls))'));
 assert.ok(adminTools.includes('if(S.me?.admin||S.me?.admin_controls)'));
 assert.ok(accountServer.includes('result["admin_controls"] = True'));
 assert.ok(accountServer.includes('return await user_features.items(g, target, data)'));
 assert.ok(accountServer.includes('return await location.locate(g, target, data)'));
 assert.ok(accountServer.includes('temporary_link(g, owner, media_id, media_kind)'));
});
