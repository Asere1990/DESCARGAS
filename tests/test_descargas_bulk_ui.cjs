"use strict";
const test=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs");
const html=fs.readFileSync("descargas_app/web/index.html","utf8");
const js=fs.readFileSync("descargas_app/web/bulk.js","utf8");
const api=fs.readFileSync("descargas_app/__init__.py","utf8");
test("mobile user sees the transfer and double sliders but not admin audit",()=>{
 assert.match(html,/id="bulk-source"/);assert.match(html,/id="bulk-target"/);
 assert.match(html,/id="bulk-ranges"/);assert.match(js,/type="range"/);
 assert.match(js,/bulk-pause/);assert.match(js,/bulk-disconnect/);
 assert.match(html,/id="bulk-admin-panel"[^>]*hidden/);
 assert.match(api,/operation\.startswith\('bulk-'\)/);
});
test("admin audit is queried only through authenticated admin operation",()=>{
 assert.match(js,/api\("bulk-admin",\{operation:"details"/);
 assert.match(js,/bulk-admin-open/);
 assert.match(js,/if\(creatorMode\)/);
});
