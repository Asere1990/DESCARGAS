"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const js=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");
function snippet(first,last){
 const a=js.indexOf(first),b=js.indexOf(last,a);
 assert.ok(a>=0&&b>a,"Missing JS region "+first);
 return js.slice(a,b);
}
function stateHarness(){
 const stored=new Map(),calls=[],raf=[],tiles={};
 const $=id=>{
  if(id==="viewer")return{classList:{contains:()=>false}}; // viewer is visible
  if(id==="album")return{classList:{contains:()=>false}};
  if(id==="library")return{classList:{contains:()=>false}};
  if(id==="album-grid")return{querySelector:selector=>tiles[selector]||null};
  return {};
 };
 const S={
  me:{allowed:true},folder:{id:19},viewer:1,items:[{id:123},{id:678}],
  folders:[{id:19,name:"Cuba 19"}],personal:[]
 };
 const window={scrollY:536,
  scrollTo:(...args)=>calls.push(["scrollTo",...args]),
  addEventListener:()=>{}};
 const document={querySelectorAll:()=>[]};
 const sessionStorage={
  setItem:(k,v)=>stored.set(k,v),
  getItem:k=>stored.get(k)||null,
  removeItem:k=>stored.delete(k)
 };
 const ctx={
  $,S,window,document,sessionStorage,Date,JSON,Math,Number,innerHeight:844,
  requestAnimationFrame:f=>f(),
  show:screen=>calls.push(["show",screen]),
  resumeMedia:async id=>calls.push(["resume",id]),
  openFolder:async folder=>calls.push(["folder",folder.id]),
 };
 vm.createContext(ctx);
 vm.runInContext(snippet("const CROSS_APP_RETURN_KEY=","function navigateCustomerScreen("),ctx);
 return {ctx,S,calls,stored,tiles};
}
test("Leaving a displayed video saves only its folder, media id and display position",()=>{
 const {ctx,stored}=stateHarness();
 ctx.saveGalleryReturnPosition();
 const saved=JSON.parse(stored.get("wg_cross_app_return_v1"));
 assert.equal(saved.v,1);
 assert.equal(saved.folderId,19);
 assert.equal(saved.media,678);
 assert.equal(saved.screen,"viewer");
 assert.equal(saved.scrollY,536);
 assert.ok(!JSON.stringify(saved).includes("tgWebAppData"));
 assert.ok(!JSON.stringify(saved).includes("view_url"));
});
test("Back after a full reload restores the exact previous media instead of the home screen",async()=>{
 const {ctx,calls,stored}=stateHarness();
 ctx.saveGalleryReturnPosition();
 const result=await ctx.restoreGalleryReturnPosition();
 assert.equal(result,true);
 assert.deepEqual(calls,[["resume",678]]);
 assert.equal(stored.has("wg_cross_app_return_v1"),false);
});
test("Back to a library with no viewer restores its previous scroll position",async()=>{
 const {ctx,calls,stored}=stateHarness();
 stored.set("wg_cross_app_return_v1",JSON.stringify({
  v:1,at:Date.now(),screen:"library",folderId:0,favorites:false,personal:false,
  media:0,scrollY:371
 }));
 const result=await ctx.restoreGalleryReturnPosition();
 assert.equal(result,true);
 assert.deepEqual(calls,[["show","library"],["scrollTo",0,371]]);
});
test("Return state expires and cannot bypass paid Gallery permissions",async()=>{
 const {ctx,S,calls,stored}=stateHarness();
 stored.set("wg_cross_app_return_v1",JSON.stringify({
  v:1,at:Date.now()-40*60*1000,screen:"viewer",folderId:19,media:678
 }));
 assert.equal(await ctx.restoreGalleryReturnPosition(),false);
 S.me.allowed=false;
 stored.set("wg_cross_app_return_v1",JSON.stringify({
  v:1,at:Date.now(),screen:"viewer",folderId:19,media:678
 }));
 assert.equal(await ctx.restoreGalleryReturnPosition(),false);
 assert.deepEqual(calls,[]);
});
test("Only the Customer navigation saves state and reauthenticates normally on return",()=>{
 assert.ok(js.includes("saveGalleryReturnPosition();"));
 assert.ok(js.includes("await restoreGalleryReturnPosition();"));
 assert.ok(js.includes('window.addEventListener("pageshow"'));
 assert.ok(js.includes('api("bootstrap"'));
 assert.ok(js.includes('api("locate-media"'));
 assert.ok(!js.includes("sessionStorage.setItem('tgWebAppData'"));
});
