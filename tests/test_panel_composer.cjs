const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../descargas_app/panel_web/app.js'),'utf8');
function setup(){
 const nodes=new Map();
 const $=id=>{if(!nodes.has(id))nodes.set(id,{value:'',hidden:true,files:[],attributes:{},classes:new Set(),setAttribute(k,v){this.attributes[k]=v;},classList:{toggle(name){const set=nodes.get(id).classes;if(set.has(name)){set.delete(name);return false;}set.add(name);return true;}}});return nodes.get(id);};
 const context={$,attachedFile:null,sendRequest:null,updates:0,notice(v){context.noticeText=v;},updateSendButton(){context.updates++;}};
 vm.createContext(context);
 const start=source.indexOf("$('attachment').onchange=");
 vm.runInContext(source.slice(start,source.indexOf("$('composer').onsubmit",start)),context);
 const toggle=source.indexOf("$('more-compose-tools').onclick=");
 vm.runInContext(source.slice(toggle,source.indexOf('\n',toggle)),context);
 return {context,$};
}
test('audio is rejected by MIME or extension without enabling an audio upload',()=>{
 for(const file of [{name:'recording.mp3',type:''},{name:'recording.bin',type:'audio/ogg'}]){
  const {context,$}=setup();$('attachment').files=[file];$('attachment').onchange();
  assert.equal(context.attachedFile,null);assert.match(context.noticeText,/audios/);assert.equal($('attached').hidden,true);
 }
});
test('photos, videos and files preserve the existing attachment flow',()=>{
 for(const [type,kind] of [['image/jpeg','photo'],['video/mp4','video'],['application/pdf','document']]){
  const {context,$}=setup();const file={name:'archivo',type};$('attachment').files=[file];$('attachment').onchange();
  assert.equal(context.attachedFile,file);assert.equal($('attachment-kind').value,kind);assert.equal($('attached').hidden,false);assert.equal(context.updates,1);
 }
});
test('extra composer tools expand and collapse with matching accessible state',()=>{
 const {$}=setup();const button=$('more-compose-tools');button.onclick();assert.equal(button.attributes['aria-expanded'],'true');assert.equal($('compose-tools').classes.has('expanded'),true);
 button.onclick();assert.equal(button.attributes['aria-expanded'],'false');assert.equal($('compose-tools').classes.has('expanded'),false);
});
