const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');

const source=fs.readFileSync(__dirname+'/../descargas_app/panel_web/app.js','utf8');
const block=source.slice(source.indexOf('const overviewAccounts=new Map()'),source.indexOf('function cancelOverviewSearch()'));
if(!block.includes('function restoreCachedAccounts()'))throw Error('Account cache code not found');

function setup(){
 const storage=new Map(),nodes=new Map();
 class Node{
  constructor(tag='div',value=''){this.tagName=tag.toUpperCase();this.textContent=value;this.children=[];this.dataset={};this.hidden=false;this.parentElement=null;}
  append(...items){for(const item of items){if(item.parentElement){const i=item.parentElement.children.indexOf(item);if(i>=0)item.parentElement.children.splice(i,1);}this.children.push(item);item.parentElement=this;}}
  replaceChildren(...items){for(const item of this.children)item.parentElement=null;this.children=[];this.append(...items);}
 }
 const el=(tag,value='')=>new Node(tag,value);
 const $=id=>{if(!nodes.has(id))nodes.set(id,el('div'));return nodes.get(id);};
 const ctx={$,el,avatar:()=>el('span'),Date,Number,JSON,Map,Array,Math,
  setTimeout,clearTimeout,panelAdmin:true,panelReviewer:false,accountsNext:null,epoch:0,
  profileQueue:[],profileObserver:{observe(){},disconnect(){}},
  localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};
 vm.createContext(ctx);
 vm.runInContext(block+
  "\nthis.groupSizes=()=>[...accountGroups.entries()].map(([key,group])=>[key,group.list.children.length]);"+
  "this.buildAccountGroups=buildAccountGroups;this.createAccountCard=createAccountCard;"+
  "this.restoreCachedAccounts=restoreCachedAccounts;this.saveAccounts=saveAccounts;"+
  "this.configureCache=()=>{accountListKey='panel-test';accountListReady=true;loadedAccountPages=2;};"+
  "this.resetLists=()=>{overviewAccounts.clear();accountCards.clear();};"+
  "this.addAccount=a=>{overviewAccounts.set(a.id,a);createAccountCard(a,true);};"+
  "this.getAccount=id=>overviewAccounts.get(id);",ctx);
 return {ctx,storage,$};
}
const account=(id,state,checked)=>({id:String(id),name:'Cuenta '+id,username:'user'+id,phone:'123',country:'Cuba',generation:5,status:{state,reason:'Verificada',checked}});

test('exactly two visible lists distinguish active, invalid and unverified accounts',()=>{
 const {ctx}=setup();ctx.buildAccountGroups();
 ctx.addAccount(account(1,'active',Date.now()/1000));
 ctx.addAccount(account(2,'revoked',Date.now()/1000));
 ctx.addAccount(account(3,'expired',Date.now()/1000));
 ctx.addAccount(account(4,'unknown',0));
 assert.deepEqual(Array.from(ctx.groupSizes()),[['active',1],['inactive',3]]);
});

test('authorized administrator sees an immediate bounded display snapshot without credentials',()=>{
 const {ctx,storage}=setup();ctx.buildAccountGroups();ctx.configureCache();
 const a=account(1,'active',Date.now()/1000);a.session='DO_NOT_STORE';a.token='SECRET';ctx.addAccount(a);
 ctx.addAccount(account(2,'invalid',Date.now()/1000));
 ctx.saveAccounts();const raw=storage.get('panel-test');
 assert.ok(raw);assert.ok(!raw.includes('DO_NOT_STORE'));assert.ok(!raw.includes('SECRET'));
 assert.equal(JSON.parse(raw).pages,2);
 ctx.resetLists();
 assert.equal(ctx.restoreCachedAccounts(),2);
 assert.deepEqual(Array.from(ctx.groupSizes()),[['active',1],['inactive',1]]);
 assert.equal(ctx.getAccount('1').session,undefined);
});

test('expired verification never presents an account as currently active',()=>{
 const {ctx}=setup();ctx.buildAccountGroups();ctx.configureCache();
 ctx.addAccount(account(1,'active',(Date.now()-360000)/1000));ctx.saveAccounts();ctx.resetLists();
 assert.equal(ctx.restoreCachedAccounts(),2);
 assert.equal(ctx.getAccount('1').status.state,'unknown');
 assert.deepEqual(Array.from(ctx.groupSizes()),[['active',0],['inactive',1]]);
});

test('cache expiration and storage restrictions fall back safely to server loading',()=>{
 const {ctx,storage}=setup();ctx.buildAccountGroups();ctx.configureCache();
 ctx.addAccount(account(1,'active',Date.now()/1000));ctx.saveAccounts();
 const saved=JSON.parse(storage.get('panel-test'));saved.saved=Date.now()-13*3600000;storage.set('panel-test',JSON.stringify(saved));
 ctx.resetLists();assert.equal(ctx.restoreCachedAccounts(),0);
 ctx.localStorage.setItem=()=>{throw Error('quota');};
 assert.doesNotThrow(()=>ctx.saveAccounts());
});
