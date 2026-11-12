"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const root=__dirname+"/../web_galerias/";
const html=fs.readFileSync(root+"index.html","utf8");
const app=fs.readFileSync(root+"app.py","utf8");
const core=fs.readFileSync(root+"core.py","utf8");
const css=fs.readFileSync(root+"style.css","utf8");
const mirrorCss=fs.readFileSync(root+"admin_mirror.css","utf8");
const admin=fs.readFileSync(root+"admin_tools.js","utf8");
const mirror=fs.readFileSync(root+"admin_mirror.js","utf8");

test("user profile opens all collections in independent full-screen view",()=>{
 assert.ok(html.includes('id="admin-mirror"'));
 assert.ok(html.includes('id="admin-mirror-body"'));
 for(const x of ["folders","favorites","downloads"])
  assert.ok(html.includes('id="admin-mirror-tab-'+x+'"'));
 assert.ok(!html.includes('src="/web-galerias/static/admin_mirror.js'));
 assert.ok(admin.includes('asset("script","admin_mirror.js")'));
 assert.ok(admin.includes('asset("link","admin_mirror.css")'));
 assert.ok(!css.includes("Independent admin-only user view"));
 assert.ok(app.includes('"admin_mirror.js"'));
 assert.ok(app.includes('"admin_mirror.css"'));
 for(const x of ['mirror("folders")','mirror("downloads")','mirror("favorites")'])
  assert.ok(admin.includes(x));
 assert.ok(!admin.includes("folders.onclick=()=>loadUserFolders(uid)"));
 assert.ok(admin.includes("actions.append(folders,downloads,favorites,perms,toggle)"));
 assert.ok(mirrorCss.includes("#admin-mirror .admin-mirror-body{"));
 assert.ok(mirrorCss.includes("overflow:auto;overscroll-behavior:contain;"));
});
test("admin viewer uses user-like media tiles but never touches the gallery user session",()=>{
 new (require("node:vm").Script)(mirror);
 assert.ok(mirror.includes('api("admin-mirror-folder-items"')||
           mirror.includes('"admin-mirror-folder-items"'));
 assert.ok(mirror.includes('"admin-mirror-favorites"'));
 assert.ok(mirror.includes('api("admin-mirror-download-dates"'));
 assert.ok(mirror.includes('api("admin-mirror-download-items"'));
 assert.ok(mirror.includes('state.items.forEach((item,i)=>grid.append(tile(item,i)))'));
 assert.ok(mirror.includes('v.controls=true'));
 assert.ok(!mirror.includes("quota-plus-checkout"));
 assert.ok(!mirror.includes("personal-move"));
 assert.ok(!mirror.includes("tg.initData="));
});
test("download history is based on the existing counted ticket ledger",()=>{
 assert.ok(core.includes("wg_download_tickets_user_day"));
 assert.ok(mirror.includes("No confirma que iPhone/Telegram"));
 assert.ok(mirror.includes("archivos contabilizados"));
});
