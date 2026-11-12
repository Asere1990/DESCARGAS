"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const source=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");

function block(start,end){
 const a=source.indexOf(start),b=source.indexOf(end,a);
 assert.ok(a>=0&&b>a,"missing functions: "+start);
 return source.slice(a,b);
}
const featureCode=block("function renderLibraryExtras(){","function renderFolders(){");
function fakeNodes(){
 const nodes=new Map();
 function node(id){
  if(!nodes.has(id)){
   const classes=new Set();
   nodes.set(id,{
    textContent:"",disabled:false,attrs:{},value:"",
    classList:{
     add:k=>classes.add(k),remove:k=>classes.delete(k),
     toggle:(k,off)=>{if(off)classes.add(k);else classes.delete(k)},
     contains:k=>classes.has(k)
    },
    setAttribute(k,v){this.attrs[k]=v;},
    getAttribute(k){return this.attrs[k];}
   });
  }
  return nodes.get(id);
 }
 return node;
}
test("continue opens exact media id through existing original-location API",async()=>{
 const $=fakeNodes(),calls=[],focus=[];
 const result={
  folder:{id:42,name:"Colección original",personal:false},
  items:[{id:701},{id:700},{id:699}],
  focus:700,next:699
 };
 const S={me:{allowed:true},resumes:[],folders:[],items:[],next:null,
          selected:new Set(),selection:false,epoch:0};
 const ctx={
  $,S,pretty:n=>String(n),Date,Number,
  api:async(op,data)=>{calls.push([op,data]);return result;},
  show:id=>focus.push(["show",id]),openViewer:i=>focus.push(["viewer",i]),
  renderDropTargets:()=>{},updateSelection:()=>{},renderItems:()=>{},
  notice:msg=>{throw Error("Unexpected resume failure: "+msg);}
 };
 vm.createContext(ctx);
 vm.runInContext(featureCode,ctx);
 await ctx.resumeMedia(700);
 assert.equal(calls.length,1);
 assert.equal(calls[0][0],"locate-media");
 assert.equal(calls[0][1].media,700);
 assert.deepEqual(focus,[["show","album"],["viewer",1]]);
 assert.equal(S.folder.id,42);
 assert.equal(S.next,699);
 assert.equal(S.resuming,false);
});

test("heart toggles personal state without changing current media or payments",async()=>{
 const $=fakeNodes(),calls=[],messages=[];
 const item={id:812,folder:42,favorite:false};
 const S={me:{allowed:true},folder:{favorites:true},items:[item],viewer:0,
          favoritesCount:0,collectionsPending:false,collectionsAt:1,favoritesDirty:false};
 const ctx={
  $,S,pretty:n=>String(n),Date,Number,
  api:async(op,data)=>{calls.push([op,data]);return{ok:true}},
  renderItems:()=>{},notice:msg=>messages.push(msg),
 };
 vm.createContext(ctx);
 vm.runInContext(featureCode,ctx);
 ctx.refreshCollections=async()=>{}; // no network in this isolated test
 await ctx.toggleViewerFavorite();
 assert.equal(item.favorite,true);
 assert.equal($("viewer-favorite").attrs["aria-pressed"],"true");
 assert.equal($("viewer-favorite").textContent,"♥");
 assert.equal(S.favoritesDirty,false);
 await ctx.toggleViewerFavorite();
 assert.equal(item.favorite,false);
 assert.equal($("viewer-favorite").attrs["aria-pressed"],"false");
 assert.equal($("viewer-favorite").textContent,"♡");
 assert.equal(S.favoritesDirty,true);
 assert.equal($("viewer-favorite").disabled,false);
 assert.equal(calls.length,2);
 assert.equal(calls[0][0],"favorite-set");
 assert.equal(calls[1][0],"favorite-set");
 assert.equal(messages.length,2);
});

test("failed favorite save leaves existing heart unchanged",async()=>{
 const $=fakeNodes();
 const item={id:47,favorite:true};
 const S={me:{allowed:true},items:[item],viewer:0};
 const ctx={
  $,S,pretty:n=>String(n),Date,Number,
  api:async()=>{throw Error("No autorizado")},
  renderItems:()=>{},notice:()=>{}
 };
 vm.createContext(ctx);vm.runInContext(featureCode,ctx);
 await ctx.toggleViewerFavorite();
 assert.equal(item.favorite,true);
 assert.equal($("viewer-favorite").disabled,false);
});

test("resume and favorites use authenticated user endpoints only",()=>{
 const intro=source.split('async function refresh(lite=false){')[1]
                    .split('function renderFolders(){')[0];
 assert.ok(intro.includes('refreshCollections().catch'));
 assert.ok(source.includes('api("progress-save",{media})'));
 assert.ok(source.includes('api("favorite-set",{media:item.id,favorite:next})'));
 assert.ok(source.includes('S.folder.favorites?"favorites-items":"items"'));
 assert.ok(source.includes('if(!S.me?.allowed||!m?.id)return;'));
});
