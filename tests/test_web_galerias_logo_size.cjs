const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'..','web_galerias');
const js=fs.readFileSync(path.join(root,'app.js'),'utf8');
const css=fs.readFileSync(path.join(root,'style.css'),'utf8');
const html=fs.readFileSync(path.join(root,'index.html'),'utf8');

test('logo has a dedicated slider and preview next to the unchanged upload input',()=>{
 const upload=html.indexOf('id="upload-logo"');
 const slider=html.indexOf('id="edit-logo_size"',upload);
 const preview=html.indexOf('id="edit-logo-image-preview"',slider);
 assert.ok(upload>=0&&slider>upload&&preview>slider);
 assert.equal(html.match(/id="upload-logo"/g).length,1);
 assert.equal(html.match(/id="edit-logo_size"/g).length,1);
 assert.match(css,/#library \.library-branding #brand-logo\{/);
 assert.match(css,/width:var\(--gallery-logo-size,49px\)/);
 assert.match(css,/height:var\(--gallery-logo-size,49px\)/);
 assert.match(css,/object-fit:contain/);
 assert.match(js,/r\.setProperty\("--gallery-logo-size",logoSize\+"px"\)/);
 assert.match(js,/Math\.min\(200,Math\.max\(24,configuredLogo\)\)/);
});

test('slider updates just the logo preview in real time, no upload required',()=>{
 const start=js.indexOf('function updateEditorLogoSizePreview(){');
 const end=js.indexOf('\nfunction loadEditorImagePreview(kind){',start);
 assert.ok(start>=0&&end>start);
 const fields=new Map([
  ['edit-logo_size',{value:'99'}],
  ['edit-logo-size-value',{textContent:''}],
  ['edit-logo-image-preview',{style:{},hidden:false,dataset:{}}],
  ['upload-logo',{files:[]}]
 ]);
 const context={
  $:id=>fields.get(id),Number,Math,Date,
  URL:{revokeObjectURL(){},createObjectURL:()=> 'blob:test'}
 };
 vm.createContext(context);
 vm.runInContext(js.slice(start,end)+'\nthis.update=updateEditorLogoSizePreview;',context);
 context.update();
 assert.equal(fields.get('edit-logo-size-value').textContent,'99 px');
 assert.equal(fields.get('edit-logo-image-preview').style.width,'99px');
 assert.equal(fields.get('edit-logo-image-preview').style.height,'99px');
 fields.get('edit-logo_size').value='24';context.update();
 assert.equal(fields.get('edit-logo-size-value').textContent,'24 px');
 fields.get('edit-logo_size').value='200';context.update();
 assert.equal(fields.get('edit-logo-image-preview').style.height,'200px');
});

test('new value is populated, persisted by both save routes, and wired on input',()=>{
 const populate=js.split('function populateEditor(){')[1]?.split('async function saveDesign(){')[0]||'';
 const save=js.split('async function saveDesign(){')[1]?.split('async function loadPermissions(){')[0]||'';
 const confirm=js.split('async function confirmEditorChanges(){')[1]?.split('function ',1)[0]||'';
 assert.match(populate,/"logo_size"/);
 assert.match(populate,/loadEditorLogoPreview\(\)/);
 assert.match(save,/"logo_size"/);
 assert.match(confirm,/"logo_size"/);
 assert.match(js,/\$\("edit-"\+key\)\.oninput=updateEditorImageSizes/);
 assert.match(js,/if\(key==="logo"\)loadEditorLogoPreview\(\)/);
 assert.match(confirm,/await api\("admin-design",\{design\}\)/);
 assert.match(js,/function loadEditorLogoPreview\(\)/);
});

test('layout preserves title and gallery media, and rejects skewed logos',()=>{
 assert.match(css,/#library \.library-branding #brand-name\{/);
 assert.match(css,/#tab-diseno \.editor-logo-setting/);
 assert.match(css,/#tab-diseno \.editor-logo-setting #edit-logo-image-preview/);
 assert.match(html,/alt="Vista previa del logo"/);
 assert.doesNotMatch(js,/logo_size\).*(?:\.delete|delete_object)/);
});
