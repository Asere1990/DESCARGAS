"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const root=__dirname+"/../web_galerias/";
const js=fs.readFileSync(root+"app.js","utf8");
const admin=fs.readFileSync(root+"admin_tools.js","utf8");
const html=fs.readFileSync(root+"index.html","utf8");
const css=fs.readFileSync(root+"style.css","utf8");
const clientCode=(()=>{
 const begin=js.indexOf("async function refreshAdminViewerMedia(");
 const end=js.indexOf("function closeViewer(){",begin);
 assert.ok(begin>0&&end>begin);
 return js.slice(begin,end);
})();

function viewerContext({initial=false,updated=true}={}){
 const state={items:[{id:55,available:initial,view:initial?"/old":null,
                        thumb:"/thumb",kind:"video",mime:"video/mp4"}],
              viewer:0};
 const changes=[],alerts=[];
 const nodes={
  viewer:{classList:{contains:()=>false}},
  "view-video":{error:null,load:()=>changes.push("video-load")}
 };
 const ctx={S:state,ADMIN_ACCOUNT_VIEW:true,$:name=>nodes[name],
  api:async(op,data)=>{
   changes.push([op,data]);
   return {media:55,available:updated,kind:"video",mime:"video/mp4",
           view:updated?"/web-galerias/media/55/view?t=admin":null,thumb:"/thumb-new"};
  },showViewerMedia:()=>changes.push("rendered"),notice:s=>alerts.push(s),
  galleryPresence:()=>{},setDropMenuOpen:()=>{},lastPresence:0,
  document:{body:{classList:{add:()=>{}}}},
  Number,Promise};
 vm.createContext(ctx);
 vm.runInContext(clientCode,ctx);
 return{ctx,changes,alerts};
}

test("an already backed-up video in a stale user folder changes from thumbnail to playable",async()=>{
 const {ctx,changes,alerts}=viewerContext({initial:false,updated:true});
 await ctx.refreshAdminViewerMedia(0,ctx.S.items[0]);
 assert.equal(ctx.S.items[0].available,true);
 assert.ok(ctx.S.items[0].view.includes("view?t=admin"));
 assert.equal(changes[0][0],"media-status");
 assert.equal(changes[0][1].media,55);
 assert.ok(changes.includes("rendered"));
 assert.ok(changes.includes("video-load"));
 assert.deepEqual(alerts,[]);
});
test("admin does not restart an already-playing video for an unnecessary fresh token",async()=>{
 const {ctx,changes}=viewerContext({initial:true,updated:true});
 await ctx.refreshAdminViewerMedia(0,ctx.S.items[0]);
 assert.equal(ctx.S.items[0].available,true);
 assert.ok(!changes.includes("rendered"));
 assert.ok(!changes.includes("video-load"));
});
test("if R2 really has only a thumbnail, viewer reports that instead of inventing playback",async()=>{
 const {ctx,alerts}=viewerContext({initial:false,updated:false});
 await ctx.refreshAdminViewerMedia(0,ctx.S.items[0]);
 assert.equal(ctx.S.items[0].available,false);
 assert.equal(ctx.S.items[0].view,null);
 assert.ok(alerts.some(s=>s.includes("original")));
});
test("admins can open the original photo/video/GIF from viewer or single selection",()=>{
 for(const id of ["viewer-admin-delete","viewer-admin-original","selection-original"]){
  assert.equal(html.split('id="'+id+'"').length-1,1,id);
 }
 assert.ok(js.includes('$("viewer-admin-original").onclick=()=>{'));
 assert.ok(js.includes("window.CubanitasTools?.locateOriginal?.(item)"));
 assert.ok(js.includes('ADMIN_ACCOUNT_VIEW&&S.me?.admin'));
 assert.ok(admin.includes('type:"wg-gallery-admin-open-original",media'));
 assert.ok(admin.includes('event.origin!==location.origin'));
 assert.ok(admin.includes('event.source!==frame.contentWindow'));
 assert.ok(admin.includes('!S.me?.admin'));
 assert.ok(admin.includes('void locateOriginal({id:media},{openViewer:true})'));
 assert.ok(admin.includes("openViewer(originalIndex)"));
 assert.ok(admin.includes('frame.allow="autoplay; fullscreen; picture-in-picture"'));
 assert.ok(css.includes(".viewer-admin-original"));
});
test("regular member gallery is still isolated from admin's original view",()=>{
 assert.ok(js.includes('if(!(S.folder?.favorites||(ADMIN_ACCOUNT_VIEW&&S.me?.admin))'));
 assert.ok(js.includes('!(ADMIN_ACCOUNT_VIEW&&S.me?.admin)'));
 assert.ok(js.includes('if(ADMIN_ACCOUNT_VIEW)void refreshAdminViewerMedia(i,S.items[i])'));
 assert.ok(js.includes('if(ADMIN_ACCOUNT_VIEW){'));
});
