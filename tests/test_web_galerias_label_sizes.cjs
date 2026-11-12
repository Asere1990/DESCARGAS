"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");

const source=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");
function block(begin,end){
 const start=source.indexOf(begin),finish=source.indexOf(end,start);
 assert.ok(start>=0&&finish>start,begin);
 return source.slice(start,finish);
}

test("the title and button image sizes update independently in the editor",()=>{
 const nodes=new Map();
 const node=id=>{
  if(!nodes.has(id))nodes.set(id,{value:"",textContent:"",style:{}});
  return nodes.get(id);
 };
 node("edit-title_image_height").value="220";
 node("edit-button_image_height").value="12";
 const ctx={$:node,updateLibraryEditorSizes:()=>{}};
 vm.createContext(ctx);
 vm.runInContext(block("function updateEditorImageSizes(){",
                        "function loadEditorImagePreview(kind){"),ctx);
 ctx.updateEditorImageSizes();
 assert.equal(node("edit-title-image-size-value").textContent,"220 px");
 assert.equal(node("edit-title-image-preview").style.height,"220px");
 assert.equal(node("edit-button-image-size-value").textContent,"12 px");
 assert.equal(node("edit-button-image-preview").style.height,"12px");
 node("edit-title_image_height").value="40";
 node("edit-button_image_height").value="48";
 ctx.updateEditorImageSizes();
 assert.equal(node("edit-title-image-preview").style.height,"40px");
 assert.equal(node("edit-button-image-preview").style.height,"48px");
});

test("size only scales the label image and checkout restores the text",()=>{
 const classes=new Set(),properties=new Map(),assets=[];
 const button={
  textContent:"",children:[],
  classList:{add:v=>classes.add(v),remove:v=>classes.delete(v)},
  style:{setProperty:(k,v)=>properties.set(k,v)},
  setAttribute(k,v){this[k]=v;},
  replaceChildren(...children){this.children=children;}
 };
 class FakeImage{
  constructor(){assets.push(this);this.alt="";this.className="";this.onload=null;}
  set src(value){this.url=value;}
  get src(){return this.url;}
 }
 const S={me:{design:{button_mode:"image",button_image_height:46}}};
 const ctx={S,$:id=>id==="enter"?button:null,Image:FakeImage};
 vm.createContext(ctx);
 vm.runInContext(block("function renderEntryButtonLabel(", "function updateEditorLabelChoices(){"),ctx);

 ctx.renderEntryButtonLabel("Entrar");
 assert.equal(button.textContent,"Entrar");
 assert.equal(button["aria-label"],"Entrar");
 assert.equal(properties.get("--entry-label-image-height"),"46px");
 assert.equal(classes.has("enter-image-label"),false);
 assets[0].onload();
 assert.equal(button.children[0],assets[0]);
 assert.equal(classes.has("enter-image-label"),true);

 S.me.design.button_image_height=12;
 ctx.renderEntryButtonLabel("Abrir galerías");
 assert.equal(properties.get("--entry-label-image-height"),"12px");
 assets[1].onload();
 assert.equal(button.children[0],assets[1]);
 assert.equal(button["aria-label"],"Abrir galerías");

 // A purchase is always explained in plain text, never overridden by a
 // previously delayed image request.
 S.me.design.button_image_height=48;
 ctx.renderEntryButtonLabel("COMPRAR MEMBRESÍA",true);
 assert.equal(button.textContent,"COMPRAR MEMBRESÍA");
 assert.equal(classes.has("enter-image-label"),false);
 assets[1].onload();
 assert.equal(button.textContent,"COMPRAR MEMBRESÍA");
 assert.equal(classes.has("enter-image-label"),false);
});
