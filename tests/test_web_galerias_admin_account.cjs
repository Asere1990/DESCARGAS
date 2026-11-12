"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const root=__dirname+"/../web_galerias/";
const source=fs.readFileSync(root+"app.js","utf8");
const start=source.indexOf("async function api(op,data={}){");
const end=source.indexOf("async function refresh(lite=false){",start);
assert.ok(start>0&&end>start);
const apiSource=source.slice(start,end);
const build=({admin=true}={})=>{
 const requests=[];
 const context={
  ADMIN_ACCOUNT_VIEW:admin,ADMIN_ACCOUNT_UID:777,
  ADMIN_ACCOUNT_INITDATA:"verified_admin_initData",
  BROWSER_ADMIN:false,tg:{initData:"signed_normal"},ENDPOINT:"/web-galerias/api",
  JSON,Error,String,Set,Number,Promise,
  fetch:async(url,options)=>{
   requests.push({url,headers:options.headers,
                  body:JSON.parse(options.body)});
   return {ok:true,json:async()=>({ok:true}),text:async()=>""};
  }
 };
 vm.createContext(context);vm.runInContext(apiSource,context);
 return {context,requests};
};
test("admin-only API always forces selected target, never substitutes Telegram identity",async()=>{
 const {context,requests}=build();
 await context.api("bootstrap");
 await context.api("items",{folder:12,cursor:0,telegram_id:999999});
 await context.api("favorites-items",{cursor:0});
 await context.api("personal-rename",{personal_folder:4,name:"Nueva"});
 assert.equal(requests[0].body.op,"admin-view-bootstrap");
 assert.equal(requests[1].body.op,"admin-view-items");
 assert.equal(requests[2].body.op,"admin-view-favorites");
 assert.equal(requests[3].body.op,"admin-view-personal-rename");
 for(const item of requests){
  assert.equal(item.body.telegram_id,777);
  assert.equal(item.headers["X-Telegram-Init-Data"],"verified_admin_initData");
 }
});
test("no checkout, presence, progress or user debit during admin view",async()=>{
 const {context,requests}=build();
 await assert.rejects(context.api("checkout"));
 await context.api("presence",{view:"library"});
 await context.api("progress-save",{media:3});
 assert.equal(requests.length,0);
 await context.api("reserve",{ids:[3]});
 assert.equal(requests[0].body.op,"reserve");
 assert.equal(requests[0].body.telegram_id,undefined);
});
test("normal customer API continues to use their own Telegram initData",async()=>{
 const {context,requests}=build({admin:false});
 await context.api("bootstrap",{name:"Ana"});
 assert.equal(requests[0].body.op,"bootstrap");
 assert.equal(requests[0].body.telegram_id,undefined);
 assert.equal(requests[0].headers["X-Telegram-Init-Data"],"signed_normal");
});
