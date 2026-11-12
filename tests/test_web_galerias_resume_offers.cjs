"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const source=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");
const html=fs.readFileSync(__dirname+"/../web_galerias/index.html","utf8");
const css=fs.readFileSync(__dirname+"/../web_galerias/style.css","utf8");
function block(start,end){
 const a=source.indexOf(start),b=source.indexOf(end,a);
 assert.ok(a>=0&&b>a,"Missing snippet "+start);
 return source.slice(a,b);
}
test("resume is compact per gallery, with no large global banner",()=>{
 assert.ok(!html.includes('id="resume-feature"'));
 assert.ok(!html.includes('id="resume-open"'));
 assert.ok(source.includes('className="folder-continue"'));
 assert.ok(source.includes('S.resumes.find(p=>Number(p.folder)===Number(folder.id))'));
 assert.ok(source.includes('continueButton.onclick=e=>{e.stopPropagation();resumeMedia(resume.media);}'));
 assert.ok(css.includes('min-height:29px'));
});
test("Atrás returns to exact displayed thumbnail, not top of album",async()=>{
 const seen=[],folder={id:12};
 const tile={
  scrollIntoView:options=>seen.push(options),
  classList:{add:k=>seen.push(k),remove:k=>{}}
 };
 const nodes={
  viewer:{classList:{add:k=>seen.push(k)}},
  "view-video":{pause:()=>seen.push("paused")},
  "album-grid":{querySelector:selector=>{
   seen.push(selector);return tile;
  }}
 };
 const S={folder,viewer:2,items:[{id:11},{id:12},{id:9876}],
          favoritesDirty:false};
 const ctx={S,$:id=>nodes[id],Number,Promise,
  document:{body:{classList:{remove:k=>seen.push(k)}}},
  requestAnimationFrame:fn=>fn(),setTimeout:()=>{},loadItems:async()=>{}};
 vm.createContext(ctx);
 vm.runInContext(block("function closeViewer(){","function moveViewer("),ctx);
 ctx.backToViewedMedia();
 await new Promise(resolve=>setImmediate(resolve));
 assert.ok(seen.includes('[data-media-id="9876"]'));
 assert.equal(seen.find(x=>x&&typeof x==="object"&&x.block).block,"center");
 assert.ok(seen.includes("located-media"));
 assert.ok(html.includes('id="close-viewer" class="viewer-action viewer-return"'));
 assert.ok(html.includes('‹ Atrás'));
 assert.ok(css.includes('#viewer .viewer-bottom #close-viewer.viewer-return{'));
 assert.ok(css.includes('#viewer > .viewer-favorite{'));
});
test("favorites refresh is completed before back scroll",async()=>{
 let resolveReload,found=false;
 const S={folder:{favorites:true},viewer:0,items:[{id:55}],favoritesDirty:true};
 const $=id=>id==="album-grid"?{
   querySelector:()=>{found=true;return{classList:{add(){},remove(){}},scrollIntoView(){}}}
  }:{classList:{add(){}},pause(){}};
 const ctx={S,$,Number,Promise,
  document:{body:{classList:{remove(){}}}},
  requestAnimationFrame:fn=>fn(),setTimeout:()=>{},
  loadItems:()=>new Promise(resolve=>{resolveReload=resolve;})};
 vm.createContext(ctx);
 vm.runInContext(block("function closeViewer(){","function moveViewer("),ctx);
 ctx.backToViewedMedia();
 await Promise.resolve();assert.equal(found,false);
 resolveReload();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(found,true);
});
test("Mostrar más contenido navigates to the existing verified Customer Mini App WebView",async()=>{
 const calls=[],opened=[],native=[],button={disabled:false};
 const $=id=>id==="more-content"?button:{disabled:false};
 const S={};
 const signed="query_id=abc&auth_date=1794500000&hash=verified";
 const ctx={S,$,String,URL,URLSearchParams,BROWSER_ADMIN:false,
  tg:{initData:signed,version:"8.0",platform:"ios",openTelegramLink:url=>native.push(url)},
  window:{location:{assign:url=>opened.push(url)}},
  api:async(op)=>{calls.push(op);return{
    url:"https://t.me/CustomerRobot/miniapp?startapp=offers",
    web_url:"https://customer-service-uimy.onrender.com/cubanas"}},
  notice:msg=>{throw Error("Unexpected offers error "+msg)}
 };
 vm.createContext(ctx);
 vm.runInContext(block("// Swiftgram can ignore","function updateSupportVisibility(screen){"),ctx);
 await ctx.openMoreContent();
 assert.deepEqual(calls,["offers-link"]);
 assert.equal(native.length,0,"Swiftgram's ignored Telegram deep link must not be used");
 assert.equal(opened.length,1);
 const url=new URL(opened[0]);
 assert.equal(url.origin,"https://customer-service-uimy.onrender.com");
 assert.equal(url.pathname,"/cubanas");
 assert.equal(url.searchParams.get("tgWebAppStartParam"),"offers");
 assert.equal(new URLSearchParams(url.hash.slice(1)).get("tgWebAppData"),signed);
 assert.equal(url.searchParams.has("tgWebAppData"),false,"signed identity must never be in HTTP query");
 assert.equal(button.disabled,false);
 assert.ok(html.indexOf('id="more-content"')>html.indexOf('id="personal-folders-toggle"'));
 assert.ok(css.includes('#library .more-content-entry{'));
});

test("non-Customer bot fallback still performs a visible navigation",async()=>{
 const calls=[],opened=[],button={disabled:false};
 const ctx={
  $:id=>button,S:{},BROWSER_ADMIN:false,String,
  tg:{openTelegramLink:()=>{throw Error("Swiftgram silently ignored navigation")}},
  window:{location:{assign:url=>opened.push(url)}},
  api:async(op)=>{calls.push(op);return {url:"https://t.me/CustomerRobot?startapp=offers"}},
  notice:msg=>{throw Error(msg)}
 };
 vm.createContext(ctx);
 vm.runInContext(block("// Swiftgram can ignore","function updateSupportVisibility(screen){"),ctx);
 await ctx.openMoreContent();
 assert.deepEqual(calls,["offers-link"]);
 assert.deepEqual(opened,["https://t.me/CustomerRobot?startapp=offers"]);
});
