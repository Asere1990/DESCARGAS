const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../descargas_app/panel_web/app.js'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../descargas_app/panel_web/index.html'),'utf8');
const css=fs.readFileSync(path.join(__dirname,'../descargas_app/panel_web/style.css'),'utf8');
const backend=fs.readFileSync(path.join(__dirname,'../descargas_app/panel_reports.py'),'utf8');

test('the pre-existing long hold activates checkmarks rather than report menus',()=>{
 assert.match(source,/function installMediaSelection\(node,m\)/);
 assert.match(source,/batchSelecting=true;toggleBatch\(m\)/);
 assert.match(source,/suppressClick=true/);
 assert.doesNotMatch(source,/function installMessageHold\(/);
 assert.doesNotMatch(source,/Menú de pulsación sostenida también/);
 assert.match(source,/installMediaSelection\(b,m\);return b;/);
});
test('every group/channel publication in chat exposes a separate red report control',()=>{
 assert.match(source,/className='bubble-report'/);
 assert.match(source,/Reportar publicación/);
 assert.match(source,/panelAdmin&&chat\.type!=='private'&&!m\.out/);
 assert.match(source,/reportMessageDialog\(m\)/);
 assert.match(css,/\.bubble-report\{/);
});
test('media report button is visible in top header, outside enlarged media content',()=>{
 const start=html.indexOf('<dialog id="media-dialog">');
 const header=html.indexOf('<header class="media-dialog-header">',start);
 const button=html.indexOf('id="report-media"',start);
 const headerEnd=html.indexOf('</header>',header);
 const content=html.indexOf('<div id="media-content">',start);
 assert.ok(start>=0&&header>=start&&button>header&&button<headerEnd&&headerEnd<content);
 assert.match(html,/id="report-media"[^>]*>🚩 Reportar publicación<\/button>/);
 assert.match(css,/#media-dialog \.media-report-action/);
 assert.match(source,/const reportMediaButton=\$\('report-media'\)/);
 assert.match(source,/reportMessageDialog\(chosen\)/);
});
test('the existing implementation sends a real Telegram report from a linked account',()=>{
 assert.match(backend,/functions\.messages\.ReportRequest\(/);
 assert.match(backend,/types\.ReportResultReported/);
 assert.match(backend,/self\.p\.account\(actor,\s*uid/);
 assert.match(backend,/known_message\(client,\s*entity,\s*mid\)/);
});
