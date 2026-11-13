"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const root=__dirname+"/../web_galerias/";
const src=fs.readFileSync(root+"app.js","utf8");
const css=fs.readFileSync(root+"style.css","utf8");
function section(start,end){
 const a=src.indexOf(start),b=src.indexOf(end,a);
 assert.ok(a>=0&&b>a,"missing "+start);
 return src.slice(a,b);
}
test("the pre-rendered title never gets cleared before its existing image has loaded",()=>{
 const code=section("function applyDesign(d){","async function api(op,data={}){");
 assert.ok(code.includes('headline.querySelector?.("img[data-fast-title]")'));
 assert.ok(code.includes("!headline.querySelector?.(\"img\")"));
 assert.ok(code.includes("if(reusable){"));
 assert.ok(code.includes('image.src="/web-galerias/assets/header-fast"'));
 assert.ok(css.includes("#intro.intro-loading.fast-identity .intro-bottom{"));
});
test("the pre-rendered CTA image is reused instead of requesting another",async()=>{
 let imageCount=0;
 class FakeImage{constructor(){imageCount++;}}
 const classes=new Set(),fast={complete:true,naturalWidth:500},button={
   dataset:{fastButtonLabel:"ENTRAR A GALERIAS CUBANAS"},
   querySelector:()=>fast,
   style:{setProperty(){}},
   classList:{add:k=>classes.add(k),remove:k=>classes.delete(k)},
   setAttribute(k,v){this[k]=v;},
   textContent:"",
 };
 const S={me:{design:{button_mode:"image",button_image_height:30}}};
 const ctx={S,Image:FakeImage,$:()=>button,Promise,String,Number,Math};
 vm.createContext(ctx);
 vm.runInContext(section("function renderEntryButtonLabel(","function updateEditorLabelChoices(){"),ctx);
 await ctx.renderEntryButtonLabel("ENTRAR A GALERIAS CUBANAS");
 assert.equal(imageCount,0,"No duplicate image load");
 assert.equal(button["aria-label"],"ENTRAR A GALERIAS CUBANAS");
 assert.equal(classes.has("enter-image-label"),true);
 await ctx.renderEntryButtonLabel("ENTRAR A GALERIAS CUBANAS");
 assert.equal(button.textContent,"","No replace saved image for visitor without membership");
 assert.equal(imageCount,0);
 assert.equal(classes.has("enter-image-label"),true);
});
test("entry remains click-locked until signed bootstrap returns",async()=>{
 let release,called=false;
 const nodes=new Map(),classes=new Map();
 function $(id){
  if(nodes.has(id))return nodes.get(id);
  const set=new Set;
  const node={
   disabled:id==="enter",textContent:"",style:{},
   classList:{
     add:k=>set.add(k),remove:k=>set.delete(k),
     contains:k=>set.has(k),toggle:(k,v)=>v?set.add(k):set.delete(k)
   },
   setAttribute(k,v){this[k]=v;}
  };
  if(id==="intro"){node.classList.add("intro-loading");node.classList.add("fast-identity")}
  nodes.set(id,node);return node;
 }
 const S={me:null,folders:[],personal:[]};
 const ctx={
  $,S,tg:null,pretty:n=>String(n),Promise,
  api:()=>new Promise(resolve=>release=resolve),
  applyDesign:()=>Promise.resolve(),
  renderEntryButtonLabel:()=>Promise.resolve(),
  updateQuotaStatus:()=>{},renderFolders:()=>{}
 };
 vm.createContext(ctx);
 vm.runInContext(section("async function refresh(lite=false){","function renderFolders(){"),ctx);
 const pending=ctx.refresh(false);
 assert.equal($("enter").disabled,true,"User may not click an unverified entrance");
 release({
  design:{title:"Galerías Cubanas"},
  allowed:true,admin:false,folders:[],personal_folders:[],photos:0,videos:0
 });
 await pending;
 assert.equal($("enter").disabled,false,"Signed bootstrap unlocks the original entry handler");
 assert.equal($("intro").classList.contains("intro-loading"),false);
});
