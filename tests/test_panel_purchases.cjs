const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../descargas_app/panel_web/app.js'),'utf8');
const code=source.slice(source.indexOf("$('purchases').onclick"),source.indexOf('(async()=>{try{const token=location.hash.slice(1);'));
function setup(api){
 function node(tag='div',text=''){
  return {tagName:tag.toUpperCase(),textContent:text,children:[],isConnected:true,listeners:{},
   append(...children){for(const child of children){child.parent=this;this.children.push(child);}},
   replaceChildren(...children){this.children=[];this.append(...children);},
   replaceWith(n){const i=this.parent.children.indexOf(this);this.parent.children[i]=n;n.parent=this.parent;},
   querySelectorAll(selector){return this.children.flatMap(c=>[...(c.tagName===selector.toUpperCase()?[c]:[]),...c.querySelectorAll(selector)]);},
   addEventListener(k,f){this.listeners[k]=f;},removeEventListener(k){delete this.listeners[k];},
   close(){this.open=false;this.listeners.close?.();},pause(){this.paused=true;},load(){},removeAttribute(k){delete this[k];}};
 }
 const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
 const context={$,el:node,account:{id:'22',name:'Cuenta'},operationBusy:false,base:'/panel',api,
  date:()=> 'Hoy',notice(){},management(){ $('management').open=true;return $('management-body');},
  openChat:async c=>{context.opened=c;},jumpToMessage:async id=>{context.jumped=id;}};
 vm.createContext(context);vm.runInContext(code+'\nthis.openPurchases=openPurchases;',context);
 return {context,$,root:()=>$('management-body').children[0]};
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
