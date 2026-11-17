const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../descargas_app/panel_web/app.js'),'utf8');
const helpers=source.slice(source.indexOf('function panelDownloadFrame(){'),source.indexOf('function currentFavorite(){'));
const code=helpers+source.slice(source.indexOf("$('purchases').onclick"),source.indexOf('(async()=>{try{const token=location.hash.slice(1);'));
function setup(api){
 function node(tag='div',text=''){
  return {tagName:tag.toUpperCase(),textContent:text,children:[],isConnected:true,listeners:{},style:{},
   append(...children){for(const child of children){child.parent=this;this.children.push(child);}},
   replaceChildren(...children){this.children=[];this.append(...children);},
   replaceWith(n){const i=this.parent.children.indexOf(this);this.parent.children[i]=n;n.parent=this.parent;},
   querySelectorAll(selector){return this.children.flatMap(c=>[...(c.tagName===selector.toUpperCase()?[c]:[]),...c.querySelectorAll(selector)]);},
   addEventListener(k,f){this.listeners[k]=f;},removeEventListener(k){delete this.listeners[k];},
   close(){this.open=false;this.listeners.close?.();},pause(){this.paused=true;},load(){},
   setAttribute(k,v){this[k]=v;},removeAttribute(k){delete this[k];}};
 }
 const nodes=new Map(),frames=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
 const document={getElementById:id=>frames.get(id)||null,
  createElement:tag=>node(tag),
  body:{append(frame){frames.set(frame.id,frame);}}};
 const context={$,el:node,document,window:{Telegram:null},location:{href:'https://example.org/panel',protocol:'https:'},URL,account:{id:'22',name:'Cuenta'},operationBusy:false,base:'/panel',api,
  date:()=> 'Hoy',notice(){},setTimeout:()=>0,clearTimeout:()=>{},panelNavSave(){},panelNavPush(){},
  panelNavEntries:new Map(),panelNavSteps:[],panelNavIndex:0,queueResume(){},purchasesLeaving:false,management(){ $('management').open=true;return $('management-body');},
  openChat:async c=>{context.opened=c;},jumpToMessage:async id=>{context.jumped=id;}};
 vm.createContext(context);vm.runInContext(code+'\nthis.openPurchases=openPurchases;this.preparedDownloadJobs=preparedDownloadJobs;',context);
 return {context,$,frames,root:()=>$('management-body').children[0]};
}
const item=(id='1',source='7')=>({id,date:1,title:'Compra',message:12,link:'https://t.me/c/7/12',token:'token',media:[{type:'video',index:0}],chat:{id:source,name:'Canal '+source,type:'channel',handle:'handle'}});
test('groups purchases and deduplicates pages without loading files automatically',async()=>{
 let calls=0;const s=setup(async()=> ++calls===1?{items:[item()],next:'page2'}:{items:[item(),item('2'),item('3','8')],next:null});
 await s.context.openPurchases();let [status,list,more]=s.root().children;
 assert.equal(list.children.length,1);assert.equal(more.hidden,false);
 await more.onclick();assert.equal(list.children.length,2);assert.equal(more.hidden,true);
 list.children[0].onclick();const card=list.children[2];
 const row=card.children[4];assert.equal(row.children[0].tagName,'BUTTON');row.children[0].onclick();
 const video=row.children[0];assert.equal(video.tagName,'VIDEO');assert.match(video.src,/purchase-media\?account=22/);
 s.$('management').close();assert.equal(video.paused,true);assert.equal(video.src,undefined);
});
test('purchased video starts priority preparation while its playback remains available',async()=>{
 const requests=[];
 const s=setup(async(op,body)=>{
  requests.push({op,body});
  if(op==='prepare-start')return {job:'job-1',state:'queued',progress:0,received:0,total:100};
  return {items:[item()],next:null};
 });
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 const row=list.children[2].children[4],control=row.children[1];
 assert.equal(control.tagName,'SECTION');
 assert.equal(control.children[0].tagName,'BUTTON');
 row.children[0].onclick();
 assert.equal(row.children[0].tagName,'VIDEO');
 await control.children[0].onclick();
 assert.equal(requests[1].op,'prepare-start');
 assert.equal(requests[1].body.account,'22');
 assert.equal(requests[1].body.purchase,'token');
 assert.equal(requests[1].body.index,0);
 assert.equal(control.children[1].textContent,'En espera de la vía prioritaria…');
 assert.equal(control.children[3].hidden,true);
});
test('prepared purchase displays a short-lived save link after it is ready',async()=>{
 const key=JSON.stringify(['22','handle','token',0]);
 const s=setup(async(op)=>{
  if(op.startsWith('prepare-status?'))return {job:'cached',state:'ready',
   name:'video.mp4',progress:100,file_token:'short-capability'};
  return {items:[item()],next:null};
 });
 s.context.preparedDownloadJobs.set(key,'cached');
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 const control=list.children[2].children[4].children[1];
 await new Promise(setImmediate);
 const save=control.children[3];
 assert.equal(save.tagName,'A');
 assert.equal(save.hidden,false);
 assert.match(save.href,/prepared-file\?token=short-capability/);
 assert.equal(save.download,'video.mp4');
 assert.equal(control.children[0].hidden,true);
});
test('iPhone MP4 download streams in a hidden iframe and does not leave the purchased folder',async()=>{
 const key=JSON.stringify(['22','handle','token',0]);
 const s=setup(async op=>{
  if(op.startsWith('prepare-status?'))return {
   job:'cached',state:'ready',name:'Con_Marcaaa.mp4',total:1385126953,
   received:1385126953,progress:100,file_token:'secure-capability'};
  return {items:[item()],next:null};
 });
 s.context.preparedDownloadJobs.set(key,'cached');
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 await new Promise(setImmediate);
 const control=list.children[2].children[4].children[1];
 const save=control.children[3];
 assert.equal(save.hidden,false);
 let prevented=0;
 save.onclick({preventDefault(){prevented++;}});
 assert.equal(prevented,0); // browser handles native attachment, no blob.
 assert.equal(s.frames.size,1);
 const frame=[...s.frames.values()][0];
 assert.equal(frame.tagName,'IFRAME');
 assert.equal(frame.style.display,'none');
 assert.equal(save.target,frame.name);
 assert.equal(s.$('management').open,true);
 assert.match(save.href,/prepared-file\?token=secure-capability/);
 assert.match(control.children[1].textContent,/Puedes seguir en esta carpeta/);
 save.onclick({preventDefault(){prevented++;}});
 assert.equal(s.frames.size,1); // no new window and no duplicate frames.
});
test('Telegram Mini App uses its native download API without iframe navigation',async()=>{
 const key=JSON.stringify(['22','handle','token',0]),calls=[];
 const s=setup(async op=>op.startsWith('prepare-status?')?{
  job:'cached',state:'ready',name:'video.mp4',progress:100,file_token:'secure-capability'
 }:{items:[item()],next:null});
 s.context.window.Telegram={WebApp:{initData:'signed-session',
  downloadFile(params,done){calls.push(params);done(true);}}};
 s.context.preparedDownloadJobs.set(key,'cached');
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 await new Promise(setImmediate);
 const control=list.children[2].children[4].children[1],save=control.children[3];
 let prevented=0;
 save.onclick({preventDefault(){prevented++;}});
 assert.equal(prevented,1);
 assert.equal(s.frames.size,0);
 assert.equal(calls.length,1);
 assert.equal(calls[0].file_name,'video.mp4');
 assert.match(calls[0].url,/prepared-file\?token=secure-capability/);
});
test('iPhone opens native Share Sheet for an actual OPFS-backed MP4, not a link',async()=>{
 const key=JSON.stringify(['22','handle','token',0]);
 const s=setup(async op=>op.startsWith('prepare-status?')?{
  job:'cached',state:'ready',name:'Con_Marcaaa.mp4',total:6,progress:100,
  file_token:'secure-file-token'
 }:{items:[item()],next:null});
 const chunks=[Uint8Array.from([1,2,3]),Uint8Array.from([4,5,6])];
 const writes=[],shared=[],createdFiles=new Map();
 const directory={
  async *entries(){for(const key of createdFiles.keys())yield [key,{}];},
  async getFileHandle(name){
   return {
    async createWritable(){
     return {async write(bytes){writes.push([...bytes]);},
      async close(){createdFiles.set(name,writes.slice());},
      async abort(){createdFiles.delete(name);}};
    },
    async getFile(){return {size:6,opfs:true};}
   };
  },
  async removeEntry(key){createdFiles.delete(key);}
 };
 class TestFile {
  constructor(parts,name,options){this.parts=parts;this.name=name;this.type=options.type;this.size=parts[0].size;}
 }
 s.context.File=TestFile;
 s.context.AbortController=AbortController;
 s.context.crypto={randomUUID:()=> 'file-id'};
 s.context.navigator={
  storage:{async getDirectory(){return {async getDirectoryHandle(){return directory;}};}},
  canShare(data){return data.files?.length===1&&data.files[0].type==='video/mp4';},
  share(data){shared.push(data);return Promise.resolve();}
 };
 let transferCalls=0;
 s.context.fetch=async()=>{
  transferCalls++;
  let pos=0;
  return {ok:true,headers:{get:header=>header.toLowerCase()==='content-length'?'6':null},
   blob(){throw Error('Video must never be buffered in JS as Blob');},
   arrayBuffer(){throw Error('Video must never be buffered in JS as ArrayBuffer');},
   body:{getReader(){return {
    async read(){return pos<chunks.length?{done:false,value:chunks[pos++]}:{done:true};},
    releaseLock(){}};}}
  };
 };
 s.context.preparedDownloadJobs.set(key,'cached');
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 await new Promise(setImmediate);
 await new Promise(setImmediate);
 const control=list.children[2].children[4].children[1];
 const save=control.children[3];
 assert.equal(save.hidden,false);
 assert.match(save.textContent,/Compartir video/);
 assert.equal(control.children[1].textContent.includes('Guardar video'),true);
 assert.equal(writes.length,2);
 assert.equal(transferCalls,1);
 assert.equal(s.frames.size,0);
 let blocked=0;
 save.onclick({preventDefault(){blocked++;}});
 assert.equal(blocked,1);
 assert.equal(shared.length,1);
 assert.equal(shared[0].files[0].name,'Con_Marcaaa.mp4');
 assert.equal(shared[0].files[0].size,6);
 assert.equal(shared[0].files[0].type,'video/mp4');
 assert.equal(s.$('management').open,true); // The dialog stays open.
 assert.equal(s.frames.size,0); // Share Sheet, not an MP4 page.
});
test('when iOS cannot share a large video, the ordinary download remains available',async()=>{
 const key=JSON.stringify(['22','handle','token',0]);
 const s=setup(async op=>op.startsWith('prepare-status?')?{
  job:'cached',state:'ready',name:'Large.mp4',file_token:'temp',progress:100
 }:{items:[item()],next:null});
 s.context.File=class TestFile{};
 s.context.navigator={storage:{async getDirectory(){throw Error('Sin espacio privado');}},
  canShare(){return true;},share(){throw Error('Never call share without the file');}};
 s.context.preparedDownloadJobs.set(key,'cached');
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 await new Promise(setImmediate);
 const control=list.children[2].children[4].children[1];
 const save=control.children[3];
 assert.equal(save.hidden,false);
 assert.match(save.textContent,/alternativa/);
 assert.match(control.children[1].textContent,/Sin espacio privado/);
 assert.equal(control.children[4].hidden,false); // Safari fallback survives.
});
test('late account response cannot show another account purchases',async()=>{
 let release;const s=setup(()=>new Promise(r=>release=r));const pending=s.context.openPurchases();
 s.context.account={id:'23'};release({items:[item()],next:null});await pending;
 assert.equal(s.root().children[1].children.length,0);
});
test('failed request can retry, and empty pages with a cursor remain navigable',async()=>{
 let calls=0;const s=setup(async()=>{if(++calls===1)throw Error('Reintenta');return {items:[],next:'page2'};});
 await s.context.openPurchases();const [status,list,more]=s.root().children;
 assert.equal(status.textContent,'Reintenta');assert.equal(more.hidden,false);
 await more.onclick();assert.equal(more.hidden,false);assert.match(status.textContent,/Carga más/);
});

test('large purchased Stars videos stream directly without a server prepare job',async()=>{
 const purchase=item();
 purchase.media=[{type:'video',index:0,name:'Large.mp4',size:2*1024**3}];
 const calls=[];
 const s=setup(async op=>{calls.push(op);return {items:[purchase],next:null};});
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 const control=list.children[2].children[4].children[1];
 const start=control.children[0],save=control.children[3];
 assert.equal(start.hidden,true);
 assert.equal(save.hidden,false);
 assert.match(save.href,/purchase-media\?.*download=1/);
 assert.match(control.children[1].textContent,/Archivos/);
 assert.deepEqual(calls,['purchases?account=22']);
});
test('a complete large video cached on iPhone opens Share without another Telegram transfer',async()=>{
 const purchase=item();
 purchase.media=[{type:'video',index:0,name:'Large.mp4',size:2*1024**3}];
 let requests=0,transfer=0;
 const s=setup(async op=>{requests++;return {items:[purchase],next:null};});
 const large={size:2*1024**3,type:'video/mp4',lastModified:Date.now(),name:'cached-large.mp4'};
 const dir={
  async *entries(){},
  async getFileHandle(){return {async getFile(){return large;}};}
 };
 s.context.crypto={subtle:{async digest(){return new Uint8Array(32).buffer;}}};
 s.context.TextEncoder=TextEncoder;
 const shared=[];
 s.context.navigator={
  storage:{async getDirectory(){return {async getDirectoryHandle(){return dir;}};}},
  canShare({files}){return files.length===1&&files[0]===large;},
  share({files}){shared.push(files[0]);return Promise.resolve();}
 };
 s.context.fetch=async()=>{transfer++;throw Error('Cached video must not be re-fetched');};
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 await new Promise(setImmediate);await new Promise(setImmediate);
 const control=list.children[2].children[4].children[1],save=control.children[3];
 assert.equal(save.hidden,false);
 assert.match(control.children[1].textContent,/sin volver a descargarlo/);
 let prevented=0;
 save.onclick({preventDefault(){prevented++;}});
 assert.equal(prevented,1);
 assert.equal(shared[0],large);
 assert.equal(transfer,0);
 assert.equal(requests,1);
});
