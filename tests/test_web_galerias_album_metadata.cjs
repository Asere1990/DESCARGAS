"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const root=__dirname+"/../web_galerias/";
const sync=fs.readFileSync(root+"sync.py","utf8");
const media=fs.readFileSync(root+"media_text.py","utf8");
const core=fs.readFileSync(root+"core.py","utf8");
const web=fs.readFileSync(root+"app.js","utf8");
const ocr=fs.readFileSync(root+"ocr_low_priority.py","utf8");

test("photo/video/GIF captions and replies become searchable across album siblings",()=>{
 assert.ok(sync.includes("await media_text.capture_reply(g,folder,m)"));
 assert.ok(sync.includes("await media_text.enqueue(g,folder,changed)"));
 assert.ok(sync.includes("await media_text.enqueue(g,folder,touched)"));
 assert.ok(media.includes('key="album:"+str(row["grouped"])'));
 assert.ok(media.includes("search_caption=$3,search_replies=$4,search_ocr=$5"));
 assert.ok(media.includes("search_text=$6"));
 assert.ok(media.includes("wg_media_text_outbox"));
 assert.ok(media.includes("async def reply_backfill_batch")===false);
 assert.ok(sync.includes("async def reply_backfill_batch"));
});
test("full text results still query the original GIN index, with no synchronous OCR",()=>{
 assert.ok(core.includes("CREATE INDEX IF NOT EXISTS wg_media_text ON wg_media USING GIN"));
 assert.ok(core.includes("to_tsvector('simple',search_text)"));
 assert.ok(!core.includes("await ocr_low_priority.ocr_once(self)"));
 assert.ok(!web.includes('api("ocr"'));
 assert.ok(ocr.includes("ProcessPoolExecutor"));
});
test("original gallery's preview and R2 backup take precedence over reply backfill",()=>{
 const prime=sync.indexOf('await fair_round(g,folders,phase="prime")');
 const archive=sync.indexOf('await fair_round(g,selected,phase="archive")');
 const replies=sync.indexOf('await fair_round(g,historical,phase="metadata")');
 assert.ok(prime>=0&&archive>prime&&replies>archive);
});
