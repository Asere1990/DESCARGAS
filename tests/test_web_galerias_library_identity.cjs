"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const root=__dirname+"/../web_galerias/";
const html=fs.readFileSync(root+"index.html","utf8");
const script=fs.readFileSync(root+"app.js","utf8");
const css=fs.readFileSync(root+"style.css","utf8");

test("editor has three independent headings with text, image and size controls",()=>{
 for(const key of ["library_brand","library_eyebrow","library_heading"]){
  const asset=key.replace("_","-");
  for(const id of ["edit-"+key,"edit-"+key+"_mode",
                    "edit-"+key+"_image_height","upload-"+asset,
                    "edit-"+key+"-image-preview"]){
   assert.equal(html.split('id="'+id+'"').length-1,1,id);
  }
 }
 assert.ok(html.includes('id="collection-eyebrow"'));
 assert.ok(html.includes('id="collection-heading"'));
 assert.ok(html.includes('id="brand-name"'));
 assert.ok(script.includes("applyLibraryIdentity(d);"));
 assert.ok(script.includes("updateLibraryEditorChoices();"));
 assert.ok(script.includes("loadLibraryEditorPreview(slot);"));
 assert.ok(css.includes("#library .library-slot-image img{"));
});

test("the confirm workflow persists all three text and image modes, not just the two sizes",()=>{
 const a=script.indexOf("async function confirmEditorChanges(){");
 const b=script.indexOf("let searchTimer=null;",a);
 assert.ok(a>0&&b>a);
 const body=script.slice(a,b);
 for(const key of ["library_brand","library_brand_mode","library_eyebrow",
                    "library_eyebrow_mode","library_heading","library_heading_mode",
                    "library_brand_image_height","library_eyebrow_image_height",
                    "library_heading_image_height"]){
  assert.ok(body.includes('"'+key+'"'),key);
 }
 for(const asset of ["library-brand","library-eyebrow","library-heading"]){
  assert.ok(body.includes('"'+asset+'"'),asset);
 }
 assert.ok(body.includes('api("admin-design",{design})'));
 assert.ok(body.includes("S.identityRefreshVersion=Date.now()"));
});

test("customer search and quota routes remain unchanged by styling edits",()=>{
 assert.ok(script.includes('if(op==="reserve")'));
 assert.ok(script.includes('if(op==="favorites-items")'));
 assert.ok(html.includes('id="quota-count"'));
 assert.ok(!html.includes('id="go-intro" class="mobile-action-primary"'));
});
