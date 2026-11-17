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
 assert.match(control.children[1].textContent,/Descarga nativa solicitada/);
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
test('a cached 2GB video never calls iOS Share Sheet or leaves the panel',async()=>{
 const purchase=item();
 purchase.media=[{type:'video',index:0,name:'Large.mp4',size:2*1024**3}];
 let requests=0,transfer=0;
 const s=setup(async op=>{requests++;return {items:[purchase],next:null};});
 const large={size:2*1024**3,type:'',lastModified:Date.now(),name:'cached-large.mp4'};
 const dir={
  async *entries(){},
  async getFileHandle(){return {async getFile(){return large;}};}
 };
 s.context.crypto={subtle:{async digest(){return new Uint8Array(32).buffer;}}};
 s.context.TextEncoder=TextEncoder;
 const shared=[];
 s.context.File=class TestFile{
  constructor(){throw Error('Must not wrap 2GB file into WebKit File');}
 };
 s.context.navigator={
  storage:{async getDirectory(){return {async getDirectoryHandle(){return dir;}};}},
  canShare(){throw Error('Must not call canShare for a 2GB file');},
  share(data){shared.push(data);throw Error('Do not open native Share for 2GB');}
 };
 s.context.fetch=async()=>{transfer++;throw Error('Do not fetch the file while browsing the cache');};
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 for(let i=0;i<8;i++)await new Promise(setImmediate);
 const control=list.children[2].children[4].children[1],save=control.children[3];
 assert.equal(save.hidden,false);
 assert.match(save.textContent,/Descargar video en Archivos/);
 assert.match(control.children[1].textContent,/NO en Archivos ni Fotos/);
 let prevented=0;
 save.onclick({preventDefault(){prevented++;}});
 assert.equal(prevented,0); // Browser handles attachment in a hidden iframe.
 assert.equal(shared.length,0);
 assert.equal(s.frames.size,1);
 assert.equal(save.target,'panel-file-download-frame');
 assert.equal(s.$('management').open,true);
 assert.match(save.href,/purchase-media\?.*download=1/);
 assert.equal(transfer,0);
 assert.equal(requests,1);
});

test('Stars 1.6 GB transfer uses bounded ranges, verifies size and resumes a partial 5% transfer',async()=>{
 const s=setup(async()=>({items:[],next:null}));
 const total=1600000000;
 let disk=0,interrupted=true;
 const requests=[],updates=[];
 const handle={
  async getFile(){return {size:disk,type:'video/mp4',lastModified:Date.now(),name:'Large.mp4'};},
  async createWritable({keepExistingData}={}){
   let size=keepExistingData?disk:0,position=0;
   return {async seek(n){position=n;},
    async write(chunk){position+=chunk.length;size=Math.max(size,position);},
    async close(){disk=size;},async abort(){}};
  }
 };
 const dir={async getFileHandle(){return handle;}};
 s.context.navigator={
  canShare:({files})=>files.length===1,
  share:()=>Promise.resolve(),
  storage:{async getDirectory(){return {async getDirectoryHandle(){return dir;}};}}
 };
 s.context.File=class TestFile{};
 s.context.AbortController=AbortController;
 s.context.setTimeout=(fn,ms)=>{if(ms<10000)fn();return 1;};
 s.context.clearTimeout=()=>{};
 s.context.fetch=async(url,options)=>{
  const range=options.headers.Range;
  requests.push(range);
  const [start,end]=range.match(/\d+/g).map(Number);
  const partial=interrupted&&start===67108864;
  if(partial)interrupted=false;
  let sent=false;
  return {
   status:206,
   headers:{get(k){return k==='Content-Range'?'bytes '+start+'-'+end+'/'+total:
     k==='Content-Length'?String(end-start+1):null;}},
   body:{getReader(){return {
    async read(){
     if(sent)return {done:true};
     sent=true;
     return {done:false,value:{length:partial?16777216:end-start+1}};
    },
    releaseLock(){}
   };}}
  };
 };
 vm.runInContext('this.stage=panelShareStage;',s.context);
 const result=await s.context.stage('/panel/api/purchase-media?download=1',
  'Large.mp4',{accountId:'22'},(current,total)=>updates.push([current,total]),
  'v2-testcache.mp4',total);
 assert.equal(result.file.size,total);
 assert.ok(requests.length>40);
 assert.ok(requests.every(v=>Number(v.match(/bytes=\d+-(\d+)/)[1])<total));
 assert.equal(requests[2],'bytes=67108864-100663295');
 assert.equal(requests[3],'bytes=83886080-117440511');
 assert.deepEqual(updates.at(-1),[total,total]);
});
test('Starting a large Stars download pauses the paid video preview',async()=>{
 const purchase=item();
 purchase.media=[{type:'video',index:0,name:'Large.mp4',size:1600000000}];
 const s=setup(async()=>({items:[purchase],next:null}));
 s.context.navigator={};
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 const row=list.children[2].children[4];
 row.children[0].onclick();
 const video=row.children[0];
 assert.equal(video.tagName,'VIDEO');
 await row.children[1].children[0].onclick();
 assert.equal(video.paused,true);
 assert.equal(video.src,undefined);
});

test('Large Stars videos always retain the legacy preparation fallback',async()=>{
 const purchase=item();
 purchase.media=[{type:'video',index:0,name:'Large.mp4',size:1600000000}];
 const s=setup(async()=>({items:[purchase],next:null}));
 s.context.navigator={};
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 const control=list.children[2].children[4].children[1];
 const legacy=control.children[6];
 assert.equal(legacy.hidden,false);
 assert.match(legacy.textContent,/preparación tradicional/);
 legacy.onclick();
 assert.equal(legacy.hidden,true);
 const old=control.children[7];
 assert.equal(old.tagName,'SECTION');
 assert.match(old.children[0].textContent,/Preparar descarga prioritaria/);
});

test('a large uncached video shows native Files download without initiating OPFS staging',async()=>{
 const purchase=item();
 purchase.media=[{type:'video',index:0,name:'Large.mp4',size:1600000000}];
 const s=setup(async()=>({items:[purchase],next:null}));
 s.context.navigator={};
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 const control=list.children[2].children[4].children[1];
 const start=control.children[0],save=control.children[3],legacy=control.children[6];
 assert.equal(start.hidden,true);
 assert.equal(save.hidden,false);
 assert.match(save.textContent,/Archivos/);
 assert.equal(legacy.hidden,false);
 let prevented=0;
 save.onclick({preventDefault(){prevented++;}});
 assert.equal(prevented,0);
 assert.equal(save.target,'panel-file-download-frame');
 assert.equal(s.frames.size,1);
});

test('1.6GB Stars video uses a native Safari ticket, not iframe or Share Sheet',async()=>{
 const purchase=item();
 purchase.media=[{
  type:'video',index:0,name:'Video_1600.mp4',size:1600000000,
  native_url:'https://descargas.example/panel/api/native-stars-file?ticket=encrypted'
 }];
 const requests=[];
 const s=setup(async(op)=>{requests.push(op);return {items:[purchase],next:null};});
 let shared=0;
 s.context.navigator={share(){shared++;throw Error('Should not share a 1.6 GB file');}};
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 const control=list.children[2].children[4].children[1];
 assert.equal(control.tagName,'SECTION');
 const link=control.children[0];
 assert.equal(link.tagName,'A');
 assert.match(link.textContent,/Safari/);
 assert.equal(link.href,purchase.media[0].native_url);
 assert.equal(link.target,'_blank');
 assert.equal(link.rel,'noopener noreferrer');
 link.onclick();
 assert.match(control.children[1].textContent,/No se ha confirmado/);
 assert.equal(s.frames.size,0);
 assert.equal(shared,0);
 assert.deepEqual(requests,['purchases?account=22']);
});
test('native Safari link preserves the old prepared-file fallback without touching accounts',async()=>{
 const purchase=item();
 purchase.media=[{
  type:'video',index:0,name:'Video_1600.mp4',size:1600000000,
  native_url:'https://descargas.example/panel/api/native-stars-file?ticket=encrypted'
 }];
 const requests=[];
 const s=setup(async(op,body)=>{requests.push({op,body});return {items:[purchase],next:null};});
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 const control=list.children[2].children[4].children[1];
 const oldButton=control.children[4];
 assert.match(oldButton.textContent,/preparación tradicional/);
 oldButton.onclick();
 assert.equal(oldButton.hidden,true);
 const prior=control.children[6];
 assert.equal(prior.tagName,'SECTION');
 assert.match(prior.children[0].textContent,/Preparar descarga prioritaria/);
 assert.equal(requests.length,1); // Opening an alternative never starts a transfer.
});


test('Telegram WebApp opens the Stars link in the external browser instead of an embedded tab',async()=>{
 const purchase=item();
 const url='https://descargas.example/panel/api/native-stars-file?ticket=encrypted';
 purchase.media=[{type:'video',index:0,name:'Big.mp4',size:1600000000,native_url:url}];
 const s=setup(async()=>({items:[purchase],next:null}));
 const opened=[];
 s.context.window.Telegram={WebApp:{
  initData:'verified-miniapp',openLink(u,options){opened.push([u,options]);}
 }};
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 const control=list.children[2].children[4].children[1];
 let prevented=0;
 control.children[0].onclick({preventDefault(){prevented++;}});
 assert.equal(prevented,1);
 assert.equal(opened.length,1);
 assert.equal(opened[0][0],url);
 assert.equal(opened[0][1].try_instant_view,false);
 assert.match(control.children[1].textContent,/externo/);
 assert.equal(s.frames.size,0);
});
test('Stars browser export provides a copy-link alternative without claiming download success',async()=>{
 const purchase=item();
 const url='https://descargas.example/panel/api/native-stars-file?ticket=encrypted';
 purchase.media=[{type:'video',index:0,name:'Big.mp4',size:1600000000,native_url:url}];
 const s=setup(async()=>({items:[purchase],next:null}));
 const copies=[];
 s.context.navigator={clipboard:{writeText(text){copies.push(text);return Promise.resolve();}}};
 await s.context.openPurchases();
 const list=s.root().children[1];list.children[0].onclick();
 const control=list.children[2].children[4].children[1];
 await control.children[3].onclick();
 assert.deepEqual(copies,[url]);
 assert.match(control.children[1].textContent,/Enlace temporal copiado/);
 assert.doesNotMatch(control.children[1].textContent,/descarga completada/i);
});
