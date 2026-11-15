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
test('linked-account detail shows existing @username or dash without altering account sessions',()=>{
 const name=html.indexOf('id="account-name"');
 const telegramId=html.indexOf('id="account-telegram-id"',name);
 const username=html.indexOf('id="account-username"',telegramId);
 const phone=html.indexOf('id="account-phone"',username);
 assert.ok(name>=0&&telegramId>name&&username>telegramId&&phone>username);
 const begin=source.indexOf("$('account-username').textContent=");
 const end=source.indexOf("$('account-phone').textContent=",begin);
 assert.ok(begin>=0&&end>begin);
 const fragment=source.slice(begin,end);
 assert.match(fragment,/\$\('account-username'\)\.hidden=false/);
 for(const [usernameValue,expected] of [
   ['cubana','Username: @cubana'],
   ['@cubana','Username: @cubana'],
   ['', 'Username: -'],
   [null, 'Username: -']
 ]){
   const field={hidden:true,textContent:''};
   const context={$:name=>{assert.equal(name,'account-username');return field;},
     a:{username:usernameValue}};
   vm.runInNewContext(fragment,context);
   assert.equal(field.textContent,expected);
   assert.equal(field.hidden,false);
 }
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


function contactHelpers(){
 const begin=source.indexOf('function contactPhone(a){');
 const end=source.indexOf('function createAccountCard(a,skipProfile=false){',begin);
 assert.ok(begin>=0&&end>begin,'Funciones de contacto disponibles solo en el panel');
 const section=source.slice(begin,end);
 const context={};
 vm.createContext(context);
 vm.runInContext(section+'\nthis.contactPhone=contactPhone;this.contactVCard=contactVCard;',context);
 return {contactPhone:context.contactPhone,contactVCard:context.contactVCard,section};
}

test('ADD produces an importable vCard using existing name phone username and Telegram ID',()=>{
 const {contactVCard,section}=contactHelpers();
 const record={id:'7461848707',name:'Cuenta de prueba',phone:'+1 213-410-6305',username:'cuenta_demo'};
 const card=contactVCard(record);
 assert.ok(card.startsWith('BEGIN:VCARD\r\nVERSION:3.0\r\n'));
 assert.ok(card.endsWith('END:VCARD\r\n'));
 assert.match(card,/FN:Cuenta de prueba\r\n/);
 assert.match(card,/TEL;TYPE=CELL:\+12134106305\r\n/);
 assert.match(card,/NOTE:Telegram ID: 7461848707\\nTelegram: @cuenta_demo\r\n/);
 assert.doesNotMatch(section,/api\(|client\.|get_me|TelegramClient/);
});

test('contact button stays disabled without phone and does not invent contact details',()=>{
 const {contactPhone,contactVCard}=contactHelpers();
 assert.equal(contactPhone({phone:''}),'');
 assert.equal(contactPhone({phone:'No disponible'}),'');
 assert.throws(()=>contactVCard({id:'123',name:'Sin teléfono',phone:''}),/teléfono/);
 const v=contactVCard({id:'123',name:'Sin username',phone:'+34 600 00 00 00',username:''});
 assert.match(v,/FN:Sin username/);
 assert.doesNotMatch(v,/Telegram: @/);
});

test('vCard escapes newlines and commas in names, without injecting another contact',()=>{
 const {contactVCard}=contactHelpers();
 const v=contactVCard({id:'123',name:'Nombre,\nBEGIN:VCARD',phone:'+14155551234',username:'test'});
 assert.match(v,/FN:Nombre\\,\\nBEGIN:VCARD\r\n/);
 assert.equal((v.match(/(?:^|\r\n)BEGIN:VCARD\r\n/g)||[]).length,1);
});

test('ADD is next to favorite star, independent from account navigation',()=>{
 const start=source.indexOf('function createAccountCard(a,skipProfile=false){');
 const end=source.indexOf('// Short-lived, administrator-only',start);
 assert.ok(start>=0&&end>start);
 const card=source.slice(start,end);
 assert.match(card,/wrapper\.append\(b,favorite,addContact,checked,check\)/);
 assert.match(card,/addContact\.onclick=e=>\{e\.preventDefault\(\);e\.stopPropagation\(\);/);
 assert.match(card,/addContact\.className='account-contact-add'/);
 assert.match(css,/\.account-card>.account-contact-add\{/);
 assert.match(css,/\.account-card>.account-favorite-star\{right:/);
 assert.match(source,/if\(isAccount\)\{\s*const cached=overviewAccounts/);
 assert.match(source,/entry\.append\(add\)/);
});


test('ADD now opens authenticated inline .vcf contact card instead of iOS share sheet',()=>{
 const begin=source.indexOf('async function addPhoneContact(a){');
 const end=source.indexOf('function createAccountCard(a,skipProfile=false){',begin);
 assert.ok(begin>=0&&end>begin);
 const fragment=source.slice(begin,end);
 assert.match(fragment,/\/api\/contact\.vcf\?account=/);
 assert.match(fragment,/link\.click\(\)/);
 assert.doesNotMatch(fragment,/navigator\.share|navigator\.canShare/);
 assert.doesNotMatch(fragment,/link\.download|URL\.createObjectURL/);
 assert.match(source,/addPhoneContact\(a\)\.catch\(/);
});
