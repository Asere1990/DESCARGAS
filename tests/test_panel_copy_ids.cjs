const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const dir=path.join(__dirname,'../descargas_app/panel_web');
const source=fs.readFileSync(path.join(dir,'app.js'),'utf8');
const html=fs.readFileSync(path.join(dir,'index.html'),'utf8');
const css=fs.readFileSync(path.join(dir,'style.css'),'utf8');

function copySetup({clipboardWorks=true,fallbackWorks=true}={}){
 const copied=[],notices=[],areas=[];
 const context={
   navigator:{clipboard:{writeText:async value=>{
     if(!clipboardWorks)throw Error('blocked');
     copied.push(value);
   }}},
   document:{
     body:{append:area=>areas.push(area)},
     execCommand:operation=>{
       assert.equal(operation,'copy');
       if(fallbackWorks)copied.push(areas.at(-1).value);
       return fallbackWorks;
     }
   },
   el:(tag,value)=>({
     tag,value,style:{},setAttribute(name,text){this[name]=text;},
     select(){this.selected=true;},remove(){this.removed=true;}
   }),
   notice:message=>notices.push(message),
 };
 const section=source.slice(
   source.indexOf('async function copyTelegramId(value){'),
   source.indexOf('function locked(text){'));
 assert.ok(section.startsWith('async function copyTelegramId(value){'));
 vm.createContext(context);
 vm.runInContext(section+
   '\nthis.copyTelegramId=copyTelegramId;this.copyIdButton=copyIdButton;',context);
 return {context,copied,notices,areas};
}

test('selected linked-account header displays Telegram ID directly under the name',()=>{
 const start=html.indexOf('<strong id="account-name">');
 const row=html.indexOf('id="account-telegram-row"',start);
 const number=html.indexOf('id="account-telegram-id"',row);
 const button=html.indexOf('id="copy-account-id"',number);
 assert.ok(start>=0&&row>start&&number>row&&button>number);
 assert.match(source,/\$\('account-telegram-id'\)\.textContent='Telegram ID: '\+a\.id/);
 assert.match(source,/\$\('account-telegram-row'\)\.hidden=false/);
 assert.match(source,/\$\('copy-account-id'\)\.onclick=\(\)=>\{if\(account\)copyTelegramId\(account\.id\)/);
});
test('linked-account header shows the existing phone beneath the Telegram ID',()=>{
 const name=html.indexOf('id="account-name"');
 const id=html.indexOf('id="account-telegram-id"',name);
 const phone=html.indexOf('id="account-phone"',id);
 const next=html.indexOf('id="new-chat"',phone);
 assert.ok(name>=0&&id>name&&phone>id&&next>phone);
 assert.match(source,/\$\('account-phone'\)\.textContent='Teléfono: '\+\(a\.phone\|\|'No disponible'\)/);
 assert.match(source,/\$\('account-phone'\)\.hidden=false/);
 assert.match(css,/\.account-context \.account-phone\{/);
});
test('groups and channels have separate copy buttons; ordinary chat opening stays unchanged',()=>{
 assert.match(source,/const showPeerId=panelAdmin&&\(c\.type==='group'\|\|c\.type==='channel'\)/);
 assert.match(source,/body\.append\(el\('small','ID: '\+c\.id\)\)/);
 assert.match(source,/shell\.append\(b,copyIdButton\(c\.id,/);
 assert.match(source,/b\.onclick=\(\)=>openChat\(c\)\.catch\(e=>notice\(e\.message\)\)/);
 assert.match(source,/else \$\('chats'\)\.append\(b\)/);
 const a=html.indexOf('id="chat-id-row"');
 assert.ok(a>=0);
 assert.ok(html.indexOf('id="chat-source-id"',a)>a);
 assert.ok(html.indexOf('id="copy-chat-id"',a)>a);
 assert.match(source,/\$\('chat-id-row'\)\.hidden=!showHeaderPeerId/);
 assert.match(source,/\$\('copy-chat-id'\)\.onclick=\(\)=>\{if\(chat&&\(chat\.type==='group'\|\|chat\.type==='channel'\)\)copyTelegramId\(chat\.id\)/);
 assert.match(css,/\.chat-row-shell\{/);
 assert.match(css,/\.copy-id\{/);
});
test('copy button writes exact signed Telegram ID, without triggering chat navigation',async()=>{
 const {context,copied,notices}=copySetup();
 const b=context.copyIdButton(-1003685406950,'Copiar ID del grupo');
 assert.equal(b.type,'button');
 assert.equal(b.className,'copy-id');
 assert.match(b['aria-label'],/1003685406950/);
 let prevented=0,stopped=0;
 b.onclick({preventDefault(){prevented++;},stopPropagation(){stopped++;}});
 await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(copied,['-1003685406950']);
 assert.equal(prevented,1);
 assert.equal(stopped,1);
 assert.match(notices.at(-1),/copiado al portapapeles/);
});
test('clipboard fallback copies exact account ID in webviews with restricted clipboard API',async()=>{
 const {context,copied,areas,notices}=copySetup({clipboardWorks:false});
 await context.copyTelegramId(1081133199);
 assert.deepEqual(copied,['1081133199']);
 assert.equal(areas.length,1);
 assert.equal(areas[0].selected,true);
 assert.equal(areas[0].removed,true);
 assert.match(notices.at(-1),/copiado al portapapeles/);
});
test('clipboard failures never show a false success and user can still manually select',async()=>{
 const {context,notices}=copySetup({clipboardWorks:false,fallbackWorks:false});
 await context.copyTelegramId('123456789');
 assert.match(notices.at(-1),/No se pudo copiar/);
 assert.doesNotMatch(notices.at(-1),/copiado al portapapeles/);
});
