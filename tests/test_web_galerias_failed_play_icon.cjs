"use strict";
const test=require("node:test"), assert=require("node:assert/strict");
const fs=require("node:fs"), vm=require("node:vm");
const js=fs.readFileSync(__dirname+"/../web_galerias/app.js","utf8");
const html=fs.readFileSync(__dirname+"/../web_galerias/index.html","utf8");
test("thumbnail play icon hidden only after confirmed browser playback failure",()=>{
 assert.ok(js.includes('if(m.kind==="video"&&!failedVideo(m))'));
 assert.ok(js.includes('markFailedVideo(m);'));
 assert.ok(js.includes('function failedVideo(m){'));
 assert.ok(js.includes('m.kind==="video"&&full&&m.mime!=="image/gif"&&!failedVideo(m)'));
 assert.ok(js.includes('const imageFull=full&&!failedVideo(m)'));
 assert.ok(js.includes('result.priority_failed'));
 assert.ok(js.includes('setInterval(()=>{void recheckFailedVideos();},60000)'));
 assert.ok(html.includes("app.js?v=failed-play-icon-20261113-v1"));
});
test("a slow or queued video does not hide its icon without confirmed error",()=>{
 const js2=js.slice(js.indexOf("function beginVideoPriority("),js.indexOf("async function refreshAdminViewerMedia("));
 assert.ok(js2.includes('if(result.priority_failed){markFailedVideo(expected);return;}'));
 assert.ok(js2.includes('const state=result.priority_state||"queued";'));
 assert.ok(js2.indexOf('if(result.priority_failed)')<js2.indexOf('const state='));
});
