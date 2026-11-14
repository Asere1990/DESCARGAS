const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const root=path.join(__dirname,'..','web_galerias');
const script=fs.readFileSync(path.join(root,'app.js'),'utf8');
const block=script.split('function previewAdmin(){')[1]?.split('async function loadAdmin()')[0]||'';

test('only current Telegram connections appear in admin source list',()=>{
 assert.ok(block.length>1000);
 assert.match(block,/const activeSources=Array\.isArray\(f\.sources\)\?/);
 assert.match(block,/f\.sources\.filter\(source=>source\.source_enabled!==false\)/);
 assert.match(block,/for\(const origin of activeSources\)/);
 assert.doesNotMatch(block,/for\(const origin of \(f\.sources\?\.length\?f\.sources:/);
 assert.doesNotMatch(block,/addAdminDatum\("Origen de Telegram",f\.peer/);
 assert.doesNotMatch(block,/addAdminDatum\("Cuenta Telegram para el historial"/);
});

test('an entirely disconnected folder shows R2 only and no stale Telegram totals',()=>{
 assert.match(block,/Sin conexiones de Telegram/);
 assert.match(block,/if\(hasLiveSources\)\{/);
 assert.match(block,/Archivos respaldados en Cloudflare R2/);
 assert.match(block,/Fotos respaldadas en R2/);
 assert.match(block,/Videos respaldados en R2/);
 assert.match(block,/f\.r2_photos/);
 assert.match(block,/f\.r2_videos/);
 assert.match(block,/const hasTelegramTotals=hasLiveSources&&/);
 assert.match(block,/if\(hasLiveSources\)buttons\.append\(reset\)/);
 assert.match(block,/audit\.disabled=!hasLiveSources\|\|!f\.missing/);
});

test('existing drag sorting, source replacement and adding a new group remain available',()=>{
 assert.match(script,/bindFolderSwap\(card,"public",folder\.id\)/);
 assert.match(script,/bindFolderSwap\(trigger,"admin",f\.id\)/);
 assert.match(block,/admin-folder-handoff/);
 assert.match(block,/admin-folder-add-source/);
 assert.match(block,/admin-folder-disconnect-/);
});
