"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const root=__dirname+"/../descargas_app/panel_web/";
const js=fs.readFileSync(root+"app.js","utf8");
const html=fs.readFileSync(root+"index.html","utf8");
const css=fs.readFileSync(root+"style.css","utf8");

function block(start,end){
 const a=js.indexOf(start),b=js.indexOf(end,a);
 assert.ok(a>=0&&b>a,"missing section "+start);
 return js.slice(a,b);
}
function node(tag,text=""){
 return {
  tag,textContent:text,children:[],hidden:false,
  append(...children){this.children.push(...children);},
  replaceChildren(...children){this.children=[...children];},
  setAttribute(k,v){this[k]=v;},
 };
}

test("one dropdown underneath the existing Favorites header exposes exactly four categories",()=>{
 assert.match(html,/<button id="favorites-open"[^>]*aria-controls="favorites-menu"/);
 assert.match(html,/<section id="favorites-menu"[^>]*hidden/);
 const values=[...html.matchAll(/<option value="(account|group|channel|private)">/g)].map(x=>x[1]);
 assert.deepEqual(values,["account","group","channel","private"]);
 assert.ok(css.includes(".favorites-dropdown[hidden]{display:none!important}"));
 assert.ok(!block("function favoriteFolder(){","$('favorites-kind').onchange").includes("management("),
   "Favorites no longer opens a modal");
});

test("star is a separate golden control beside each account, not inside its open button",()=>{
 const cards=new Map();
 const context={
  el:node,avatar:name=>node("div",name),accountCards:cards,
  profileObserver:{observe(){}},
  updateAccountCard(){},verifyAccount(){},selectAccount(){},notice(){},
  toggleAccountFavorite(){},
 };
 vm.createContext(context);
 vm.runInContext(block("function createAccountCard(","// Short-lived, administrator-only"),context);
 context.createAccountCard({id:"22",name:"Cuenta principal"},true);
 const card=cards.get("22");
 assert.ok(card);
 assert.equal(card.wrapper.children[0],card.row);
 assert.equal(card.wrapper.children[1],card.favorite);
 assert.equal(card.favorite.textContent,"☆");
 assert.equal(card.favorite.className,"account-favorite-star");
 assert.ok(css.includes(".account-favorite-star[aria-pressed=true]"));
});

test("favorites menu filters accounts, groups, channels, and private chats without mixing",()=>{
 const menu={hidden:false},kind={value:"account"},list=node("section"),
       head=node("button");
 const nodes={"favorites-menu":menu,"favorites-kind":kind,
              "favorites-items":list,"favorites-open":head};
 const ctx={
  $:name=>nodes[name],el:node,notice(){},
  overviewAccounts:new Map(),
  accountFavoriteItems:[{id:"22",name:"Cuenta principal",username:"cuenta",available:true}],
  favoriteItems:[
   {type:"group",id:"-1001",account_id:"22",name:"Grupo Cuba",account:"Cuenta principal",available:false},
   {type:"channel",id:"-1002",account_id:"22",name:"Canal Cuba",account:"Cuenta principal",available:false},
   {type:"private",id:"55",account_id:"22",name:"Privado",account:"Cuenta principal",available:false}
  ],
  selectAccount:async()=>{},openChat:async()=>{},
  api:async()=>({}),refreshAccountFavorites:async()=>{},refreshFavorites:async()=>{},
 };
 vm.createContext(ctx);
 vm.runInContext(block("function hideFavoritesMenu(){","function favoriteFolder(){"),ctx);
 for(const [choice,expected] of [
  ["account","Cuenta principal"],["group","Grupo Cuba"],
  ["channel","Canal Cuba"],["private","Privado"]
 ]){
  kind.value=choice;ctx.renderFavoritesMenu();
  assert.equal(list.children.length,1);
  assert.equal(list.children[0].children[0].children[0].textContent,expected);
 }
});

test("account favorite API is independent from chat favorites and Telegram session actions",async()=>{
 const card={favorite:node("button")};
 const accounts=new Map([["22",{id:"22",name:"Cuenta principal",username:"cuenta"}]]);
 const calls=[];
 const context={
  accountCards:new Map([["22",card]]),overviewAccounts:accounts,
  api:async(op,data)=>{calls.push([op,data]);return {favorite:op==="favorite-account-toggle"};},
  refreshAccountFavorites:async()=>{},notice(){},
 };
 vm.createContext(context);
 vm.runInContext("let accountFavoriteItems=[];\n"+
   block("function isAccountFavorite(uid){","async function toggleChatFavorite(){"),context);
 context.updateAccountFavoriteStar(accounts.get("22"));
 assert.equal(card.favorite.textContent,"☆");
 assert.equal(card.favorite["aria-pressed"],"false");
 await context.toggleAccountFavorite(accounts.get("22"),card.favorite);
 assert.equal(calls.length,1);
 assert.equal(calls[0][0],"favorite-account-toggle");
 assert.equal(calls[0][1].account,"22");
 assert.equal(calls[0][1].favorite,true);
 assert.equal(card.favorite.disabled,false);
 assert.ok(!js.includes("account-favorite-login"));
});
