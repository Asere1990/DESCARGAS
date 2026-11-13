"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");

const root=__dirname+"/../web_galerias/";
const js=fs.readFileSync(root+"app.js","utf8");
const html=fs.readFileSync(root+"index.html","utf8");
const css=fs.readFileSync(root+"style.css","utf8");
function block(start,end){
 const a=js.indexOf(start),b=js.indexOf(end,a);
 assert.ok(a>=0&&b>a,"function boundaries "+start);
 return js.slice(a,b);
}
function defer(){
 let resolve;
 const promise=new Promise(done=>{resolve=done;});
 return {promise,resolve};
}
function makeNode(){
 const classes=new Set();
 return {
  classList:{
   add:value=>classes.add(value),
   remove:value=>classes.delete(value),
   toggle:(value,force)=>{if(force===true)classes.add(value);else if(force===false)classes.delete(value);
    else if(classes.has(value))classes.delete(value);else classes.add(value);},
   contains:value=>classes.has(value)
  },
  attrs:{},
  setAttribute(k,v){this.attrs[k]=v;},
  textContent:"",style:{setProperty(k,v){this[k]=v;}},hidden:false,
  replaceChildren(...els){this.children=els;},
 };
}

test("cold HTML hides all provisional branding while promo poster is visible",()=>{
 assert.match(html,/<section id="intro" class="[^"]*intro-loading" aria-busy="true">/);
 assert.match(html,/id="intro-hero-poster"[^>]*src="\/web-galerias\/assets\/intro-poster"/s);
 assert.match(css,/#intro\.intro-loading \.intro-bottom\{\s*visibility:hidden;/);
 assert.match(css,/#intro\.intro-loading \.intro-bottom\{[^}]*pointer-events:none;/s);
 assert.ok(html.indexOf('id="intro-hero-poster"')<html.indexOf('id="intro-headline"'));
});

test("first appearance only happens when title and CTA artwork are both ready",async()=>{
 const title=defer(),button=defer(),nodes=new Map();
 const $=id=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id);};
 const S={me:null,folders:[],personal:[]};
 const ctx={
  $,S,tg:null,pretty:n=>String(n),Promise,
  api:async()=>({design:{title:"Galerías Cubanas",header_mode:"image",
    button_mode:"image",button_label:"ENTRAR"},allowed:true,admin:false,
    folders:[],personal_folders:[],photos:0,videos:0}),
  applyDesign:()=>title.promise,
  renderEntryButtonLabel:()=>button.promise,
  updateQuotaStatus:()=>{},renderFolders:()=>{},
 };
 vm.createContext(ctx);
 vm.runInContext(block("async function refresh(lite=false){","function renderFolders(){"),ctx);
 const result=ctx.refresh(false);
 await new Promise(setImmediate);
 assert.equal($("intro").classList.contains("intro-loading"),true);
 assert.equal($("intro").attrs["aria-busy"],"true");
 title.resolve();
 await new Promise(setImmediate);
 assert.equal($("intro").classList.contains("intro-loading"),true,
  "The default button must never be revealed while its image is still loading");
 button.resolve();
 await result;
 assert.equal($("intro").classList.contains("intro-loading"),false);
 assert.equal($("intro").attrs["aria-busy"],"false");
});

test("unpaid visitor keeps the same edited CTA while checkout remains active",async()=>{
 const title=defer(),nodes=new Map(),calls=[];
 const $=id=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id);};
 const ctx={
  $,
  S:{me:null,folders:[],personal:[]},tg:null,pretty:n=>String(n),Promise,
  api:async()=>({design:{title:"Galerías Cubanas",header_mode:"image",
   button_mode:"image",button_label:"Entrar"},allowed:false,checkout_enabled:true,
   admin:false,folders:[],personal_folders:[],photos:0,videos:0}),
  applyDesign:()=>title.promise,
  renderEntryButtonLabel:(label,force)=>{calls.push([label,force]);return Promise.resolve();},
  updateQuotaStatus:()=>{},renderFolders:()=>{},
 };
 vm.createContext(ctx);
 vm.runInContext(block("async function refresh(lite=false){","function renderFolders(){"),ctx);
 const result=ctx.refresh(false);
 await new Promise(setImmediate);
 assert.equal($("intro").classList.contains("intro-loading"),true);
 assert.deepEqual(calls,[["Entrar",undefined]]);
 assert.equal($("enter").textContent,"","no paywall label replaces the saved entrance");
 title.resolve();
 await result;
 assert.equal($("intro").classList.contains("intro-loading"),false);
});

test("image renderers resolve only after image load; text fallback stays available",async()=>{
 const nodes=new Map(),assets=[];
 const $=id=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id);};
 class FakeImage{
  constructor(){assets.push(this);this.className="";this.alt="";}
  set src(value){this.url=value;}
 }
 const S={me:{design:{button_mode:"image",button_image_height:32}}};
 const ctx={$,S,Image:FakeImage,Promise};
 vm.createContext(ctx);
 vm.runInContext(block("function renderEntryButtonLabel(", "function updateEditorLabelChoices(){"),ctx);
 const pending=ctx.renderEntryButtonLabel("Abrir");
 let settled=false;pending.then(()=>{settled=true;});
 await Promise.resolve();
 assert.equal(settled,false);
 assets[0].onload();
 await pending;
 assert.equal($("enter").children[0],assets[0]);
 assert.equal($("enter").classList.contains("enter-image-label"),true);
 const fallback=ctx.renderEntryButtonLabel("Texto alternativo",true);
 await fallback;
 assert.equal($("enter").textContent,"Texto alternativo");
 assert.equal($("enter").classList.contains("enter-image-label"),false);
});

test("image title is ready only after replacing text, in photo and video modes",async()=>{
 const src=block("function applyDesign(d){", "async function api(op,data={}){");
 assert.match(src,/titleReady=new Promise\(resolve=>/);
 assert.match(src,/if\(S\.titleImageGeneration===titleGeneration\)headline\.replaceChildren\(image\);\s*resolve\(\);/);
 assert.match(src,/if\(mode==="photo"\)\{[\s\S]*?return titleReady;/);
 assert.match(src,/video\.src="\/web-galerias\/assets\/intro"\+revision;\s*return titleReady;/);
});
