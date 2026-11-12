"use strict";
function gallerySourceLabel(value){
 const id=Number(value||0);
 return id?(id<0?"DATOS:":"DESCARGAS:")+Math.abs(id):"No asignada";
}
function gallerySourceInput(value){
 const match=/^(?:(DATOS|DESCARGAS)\s*:\s*)?([1-9][0-9]{4,18})$/i.exec(String(value||"").trim());
 return match?{account_uid:match[2],account_system:(match[1]||"descargas").toLowerCase()}:null;
}

const $=id=>document.getElementById(id);
// This is only a visual size; the entry remains disabled until signed auth.
const fastButtonHeight=Number($("enter")?.dataset.fastButtonHeight);
if(Number.isFinite(fastButtonHeight)&&fastButtonHeight>=12&&fastButtonHeight<=48)
 $("enter").style.setProperty("--entry-label-image-height",fastButtonHeight+"px");
const tg=window.Telegram?.WebApp;
const ENDPOINT="/web-galerias/api";
// Customer document reused for a verified admin's look-through.
const ADMIN_ACCOUNT_VIEW=/^\/web-galerias\/admin\/view\/?$/.test(location.pathname);
const ADMIN_ACCOUNT_UID=ADMIN_ACCOUNT_VIEW?Number(new URLSearchParams(location.search).get("target")):0;
const ADMIN_ACCOUNT_INITDATA=ADMIN_ACCOUNT_VIEW?
 (new URLSearchParams(location.hash.slice(1)).get("tgWebAppData")||""):"";
const BROWSER_ADMIN=/^\/web-galerias\/admin(?:\/view)?\/?$/.test(location.pathname)&&
 !tg?.initData&&!ADMIN_ACCOUNT_INITDATA;
const S={me:null,folders:[],personal:[],folder:null,filter:"all",items:[],next:null,search:"",selection:false,selected:new Set(),viewer:0,admin:null,adminExpanded:new Set(),epoch:0,toastTimer:null,prepared:null,saveData:null,suppressClick:null,personalMenuOpen:false,dropMenuOpen:false,drag:null,personalSaving:false,coverFolder:null,
 resumes:[],favoritesCount:0,collectionsReady:false,collectionsPending:false,collectionsAt:0,progressTimer:null,favoritesDirty:false};
const pretty=n=>Number(n||0).toLocaleString("es-ES");
const selectionLimit=()=>(S.me?.admin||S.me?.admin_controls)?100:10;
let lastPresence=0;
function galleryPresence(){
 if(BROWSER_ADMIN||ADMIN_ACCOUNT_VIEW||!S.me?.allowed||document.hidden)return;
 const view=$("viewer").classList.contains("hidden")?
   (["album","library"].find(id=>!$(id).classList.contains("hidden"))||""):"viewer";
 if(!view||Date.now()-lastPresence<18000)return;
 lastPresence=Date.now();
 api("presence",{view}).catch(()=>{lastPresence=0;});
}
setInterval(galleryPresence,25000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden){lastPresence=0;galleryPresence();}});

const esc=x=>String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const show=id=>{for(const node of document.querySelectorAll(".screen"))node.classList.add("hidden");$(id).classList.remove("hidden");window.scrollTo(0,0);
 updateSupportVisibility(id);
 if(id==="album"||id==="library"){lastPresence=0;galleryPresence();}
 if(id==="library"&&S.me?.allowed){
  renderLibraryExtras();
  if(!S.collectionsReady||Date.now()-S.collectionsAt>30000)
   refreshCollections().catch(()=>{});
 }
};
function notice(message){const t=$("toast");t.textContent=message;t.classList.remove("hidden");clearTimeout(S.toastTimer);S.toastTimer=setTimeout(()=>t.classList.add("hidden"),4200);}
function renderWatermark(element,config){
 const line1=String(config?.wm_line1??"Búscame en Google").trim();
 const line2=String(config?.wm_line2??"hoo.be/Cubanas").trim();
 const text=[line1,line2].filter(Boolean).join("\n");
 const active=config?.wm_enabled!==false&&!!text;
 element.classList.toggle("hidden",!active);
 element.textContent=text;
 const position=config?.wm_position;
 element.style.alignItems=position==="top"?"flex-start":position==="bottom"?"flex-end":"center";
 const opacity=Number(config?.wm_opacity??30);
 element.style.opacity=String(Number.isFinite(opacity)?Math.min(100,Math.max(0,opacity))/100:0.3);
 const size=Number(config?.wm_size??20);
 element.style.fontSize=(Number.isFinite(size)?Math.min(42,Math.max(12,size)):20)+"px";
 const color=String(config?.wm_color??"#FFFFFF");
 element.style.color=/^#[0-9a-f]{6}$/i.test(color)?color:"#FFFFFF";
}
function renderViewerWatermark(config=S.me?.design){
 if(S.me?.free_access){
  $("viewer-watermark").classList.add("hidden");
  $("viewer-watermark").textContent="";
  return;
 }
 renderWatermark($("viewer-watermark"),config);
}
function updateQuotaStatus(){
 if(!S.me)return;
 const unlimited=!!S.me.quota_unlimited;
 const limit=Number(S.me.quota_daily_limit)||50;
 const remaining=Math.max(0,Number(S.me.remaining)||0);
 $("quota-count").textContent=unlimited?"Ilimitadas":pretty(remaining)+" de "+pretty(limit);
 $("quota-fill").style.width=unlimited?"100%":Math.max(0,Math.min(100,remaining/limit*100))+"%";
 const linked=S.me.quota_tier==="linked";
 const tierLabel=$("quota-tier");
 if(!tierLabel)return; // A running session may still hold the previous HTML.
 tierLabel.textContent=unlimited?
   (S.me.quota_plus_active?"♾ Descargas ilimitadas · extra mensual activo":
     "♾ Descargas ilimitadas · autorización existente"):
   linked?"🔗 Cuenta vinculada a DESCARGAS · 200 archivos diarios":
     "Plan básico · 50 archivos diarios";
 $("quota-connect")?.classList.toggle("hidden",unlimited||linked);
 $("quota-plus")?.classList.toggle("hidden",unlimited||!S.me.quota_plus_available);
}
function watermarkFromEditor(){
 return {
  wm_enabled:$("edit-wm-enabled").checked,
  wm_line1:$("edit-wm-line1").value,
  wm_line2:$("edit-wm-line2").value,
  wm_position:$("edit-wm-position").value,
  wm_opacity:Number($("edit-wm-opacity").value),
  wm_size:Number($("edit-wm-size").value),
  wm_color:$("edit-wm-color").value
 };
}
function previewWatermark(){
 const w=watermarkFromEditor();
 $("wm-opacity-value").textContent=w.wm_opacity+" %";
 $("wm-size-value").textContent=w.wm_size+" px";
 renderWatermark($("wm-preview-text"),w);
}
function populateWatermarkEditor(d){
 $("edit-wm-enabled").checked=d.wm_enabled!==false;
 for(const k of ["line1","line2","position","opacity","size","color"]){
  const key="wm_"+k,element=$("edit-wm-"+k);
  if(d[key]!==undefined)element.value=d[key];
 }
 previewWatermark();
}
async function saveWatermark(){
 const button=$("save-watermark");
 button.disabled=true;
 try{
  const result=await api("admin-watermark",{watermark:watermarkFromEditor()});
  S.me.design=result.design;
  renderViewerWatermark(result.design);
  populateWatermarkEditor(result.design);
  notice("Marca de agua actualizada en fotos y videos.");
 }catch(e){notice(e.message);}
 finally{button.disabled=false;}
}
function renderEntryButtonLabel(label,forceText=false){
 const button=$("enter"),text=String(label||"ENTRAR A LA GALERÍA");
 // A stale image load must never overwrite a newer label or the checkout CTA.
 const generation=(S.buttonLabelGeneration||0)+1;
 S.buttonLabelGeneration=generation;
 // Current operator-selected artwork was already embedded in the page.
 // Reuse the same image instead of blanking it and downloading it again
 // after the slower per-user bootstrap response.
 const fast=button.querySelector?.("img[data-fast-button]");
 if(!forceText&&fast&&S.me?.design?.button_mode==="image"&&
    button.dataset.fastButtonLabel===text){
  button.classList.add("enter-image-label");
  button.setAttribute("aria-label",text);
  const initialHeight=Number(button.dataset.fastButtonHeight);
  if(Number.isFinite(initialHeight)&&initialHeight>=12&&initialHeight<=48)
   button.style.setProperty("--entry-label-image-height",initialHeight+"px");
  return new Promise(resolve=>{
   if(fast.complete){if(!fast.naturalWidth)button.textContent=text;resolve();return;}
   fast.addEventListener("load",resolve,{once:true});
   fast.addEventListener("error",()=>{button.textContent=text;resolve();},{once:true});
  });
 }
 button.classList.remove("enter-image-label");
 button.textContent=text;
 button.setAttribute("aria-label",text);
 if(forceText||S.me?.design?.button_mode!=="image")return Promise.resolve();
 const requested=Number(S.me.design.button_image_height??30);
 const height=Number.isFinite(requested)?Math.min(48,Math.max(12,requested)):30;
 button.style.setProperty("--entry-label-image-height",height+"px");
 // Commit the image atomically: the button is still hidden on the first
 // screen until the selected artwork has loaded (or failed safely).
 return new Promise(resolve=>{
  const image=new Image();
  image.className="enter-label-image";image.alt="";
  image.onload=()=>{
   if(S.buttonLabelGeneration===generation&&S.me?.design?.button_mode==="image"){
    button.replaceChildren(image);
    button.classList.add("enter-image-label");
   }
   resolve();
  };
  // On network failure, retain the saved accessible text as fallback.
  image.onerror=()=>resolve();
  image.src="/web-galerias/assets/button-label-fast"+(S.identityRefreshVersion?"?v="+S.identityRefreshVersion:"");
 });
}
function updateEditorLabelChoices(){
 const titleImage=$("edit-header_mode").value==="image";
 const buttonImage=$("edit-button_mode").value==="image";
 $("edit-title-text-wrap").classList.toggle("hidden",titleImage);
 $("edit-title-image-wrap").classList.toggle("hidden",!titleImage);
 $("edit-button-text-wrap").classList.toggle("hidden",buttonImage);
 $("edit-button-image-wrap").classList.toggle("hidden",!buttonImage);
 updateLibraryEditorChoices();
}
function updateEditorImageSizes(){
 const titleHeight=Number($("edit-title_image_height").value);
 const buttonHeight=Number($("edit-button_image_height").value);
 $("edit-title-image-size-value").textContent=titleHeight+" px";
 $("edit-button-image-size-value").textContent=buttonHeight+" px";
 $("edit-title-image-preview").style.height=titleHeight+"px";
 $("edit-button-image-preview").style.height=buttonHeight+"px";
 updateLibraryEditorSizes();
}
function loadEditorImagePreview(kind){
 const asset=kind==="title"?"header":"button-label-image";
 const image=$("edit-"+kind+"-image-preview");
 if(image.dataset.objectUrl){
  URL.revokeObjectURL(image.dataset.objectUrl);
  delete image.dataset.objectUrl;
 }
 const file=$("upload-"+asset).files?.[0];
 image.hidden=true;
 image.onload=()=>{image.hidden=false;};
 image.onerror=()=>{image.hidden=true;};
 if(file){
  const url=URL.createObjectURL(file);
  image.dataset.objectUrl=url;image.src=url;
 }else image.src="/web-galerias/assets/"+asset+"?t="+Date.now();
 updateEditorImageSizes();
}

const LIBRARY_SLOTS=["library_brand","library_eyebrow","library_heading"];
const LIBRARY_FALLBACKS={library_brand:"Web Galerías",library_eyebrow:"MI COLECCIÓN",library_heading:"Mis galerías"};
const LIBRARY_TARGETS={library_brand:"brand-name",library_eyebrow:"collection-eyebrow",library_heading:"collection-heading"};
function updateLibraryEditorChoices(){
 for(const slot of LIBRARY_SLOTS){
  const image=$("edit-"+slot+"_mode").value==="image";
  $("edit-"+slot+"-text-wrap").classList.toggle("hidden",image);
  $("edit-"+slot+"-image-wrap").classList.toggle("hidden",!image);
 }
}
function updateLibraryEditorSizes(){
 for(const slot of LIBRARY_SLOTS){
  const height=Number($("edit-"+slot+"_image_height").value);
  $("edit-"+slot+"-image-size-value").textContent=height+" px";
  $("edit-"+slot+"-image-preview").style.height=height+"px";
 }
}
function loadLibraryEditorPreview(slot){
 const asset=slot.replaceAll("_","-"),image=$("edit-"+slot+"-image-preview");
 if(image.dataset.objectUrl){
  URL.revokeObjectURL(image.dataset.objectUrl);
  delete image.dataset.objectUrl;
 }
 image.hidden=true;
 image.onload=()=>{image.hidden=false;};
 image.onerror=()=>{image.hidden=true;};
 const file=$("upload-"+asset).files?.[0];
 if(file){
  const url=URL.createObjectURL(file);
  image.dataset.objectUrl=url;image.src=url;
 }else image.src="/web-galerias/assets/"+asset+"?t="+Date.now();
 updateLibraryEditorSizes();
}
function applyLibraryIdentity(d){
 const generation=(S.libraryIdentityGeneration||0)+1;
 S.libraryIdentityGeneration=generation;
 for(const slot of LIBRARY_SLOTS){
  const element=$(LIBRARY_TARGETS[slot]);if(!element)continue;
  const label=String(d[slot]??LIBRARY_FALLBACKS[slot]);
  const mode=d[slot+"_mode"]==="image"?"image":"text";
  element.setAttribute("aria-label",label);
  if(mode!=="image"){
   element.classList.remove("library-slot-image");
   element.textContent=label;continue;
  }
  element.classList.add("library-slot-image");
  const asset=slot.replaceAll("_","-");
  const src="/web-galerias/assets/"+asset+
    (S.identityRefreshVersion?"?v="+S.identityRefreshVersion:"");
  const value=Number(d[slot+"_image_height"]);
  const height=Number.isFinite(value)?value:
    (slot==="library_brand"?60:slot==="library_eyebrow"?44:100);
  const current=element.querySelector("img[data-library-image]");
  if(current&&current.getAttribute("src")===src){
   current.alt=label;
   current.style.height=height+"px";
   current.style.maxHeight=height+"px";
   continue;
  }
  const image=new Image();
  image.setAttribute("data-library-image",slot);
  image.alt=label;image.style.height=height+"px";
  image.style.maxHeight=height+"px";image.style.maxWidth="100%";
  image.style.width="auto";image.style.objectFit="contain";
  image.onload=()=>{
   if(S.libraryIdentityGeneration===generation)element.replaceChildren(image);
  };
  image.onerror=()=>{
   if(S.libraryIdentityGeneration===generation)element.textContent=label;
  };
  image.src=src;
 }
}
function applyDesign(d){const r=document.documentElement.style;
 r.setProperty("--accent",d.accent);r.setProperty("--bg",d.background);r.setProperty("--text",d.text_color);
 r.setProperty("--button",d.button_color);r.setProperty("--tile-aspect",d.tile_shape==="rectangle"?"4 / 3":"1");
 renderViewerWatermark(d);
 const headline=$("intro-headline");
 const titleGeneration=(S.titleImageGeneration||0)+1;
 S.titleImageGeneration=titleGeneration;
 // Keep the exact server-rendered image until it finishes decoding; replacing
 // its DOM would cause a second request and the visible title to disappear.
 const fastTitle=headline.querySelector?.("img[data-fast-title]");
 const reusable=!!(fastTitle&&d.header_mode==="image"&&
   headline.dataset.fastTitleLabel===d.title&&
   Number(headline.dataset.fastTitleHeight)===Number(d.title_image_height??120));
 if(!reusable)headline.textContent=d.title;
 headline.setAttribute("aria-label",d.title);
 applyLibraryIdentity(d);
 $("intro-subtitle").textContent=d.intro_caption;
 $("intro-placeholder").lastElementChild.textContent=d.intro_label;
 document.title=d.title;
 for(const [id,key] of [["brand-logo","logo"]]){
   const image=$(id),icon=$("brand-icon");image.onload=()=>{image.hidden=false;icon.hidden=true;};
   image.onerror=()=>{image.hidden=true;icon.hidden=false;};image.src="/web-galerias/assets/"+key+"?t="+Date.now();}
 let titleReady=Promise.resolve();
 if(d.header_mode==="image"){
  titleReady=new Promise(resolve=>{
   if(reusable){
    if(fastTitle.complete){
     if(!fastTitle.naturalWidth)headline.textContent=d.title;
     resolve();return;
    }
    fastTitle.addEventListener("load",resolve,{once:true});
    fastTitle.addEventListener("error",()=>{
     headline.textContent=d.title;resolve();
    },{once:true});
    return;
   }
   const image=new Image();
   image.alt=d.title;
   const requested=Number(d.title_image_height??120);
   const height=Number.isFinite(requested)?Math.min(220,Math.max(40,requested)):120;
   image.style.height=height+"px";
   image.style.maxHeight=height+"px";
   image.style.maxWidth="min(92vw,600px)";
   image.style.width="auto";
   image.style.objectFit="contain";
   image.onload=()=>{
    if(S.titleImageGeneration===titleGeneration)headline.replaceChildren(image);
    resolve();
   };
   // On network failure, keep the saved title without revealing a
   // temporary default while an image is still pending.
   image.onerror=()=>resolve();
   image.src="/web-galerias/assets/header-fast"+(S.identityRefreshVersion?"?v="+S.identityRefreshVersion:"");
  });
 }
 const video=$("intro-video"),placeholder=$("intro-placeholder");
 const intro=$("intro"),sound=$("intro-sound"),poster=$("intro-hero-poster");
 const mode=["photo","gif"].includes(d.intro_media)?"photo":"video";
 const fit=d.intro_fit==="contain"?"contain":"cover";
 video.style.objectFit=fit;poster.style.objectFit=fit;
 const posterReady=()=>poster.complete&&poster.naturalWidth>0;
 poster.onload=()=>{
  poster.hidden=false;placeholder.hidden=true;
  intro.classList.add("has-intro-video");
 };
 poster.onerror=()=>{
  poster.hidden=true;
  // While a video is still loading, keep its full-screen background.
  if(mode==="photo"||video.error){
   intro.classList.remove("has-intro-video");
   placeholder.hidden=false;
  }
 };
 const revision=S.heroNeedsReload?"?v="+Date.now():"";
 S.heroNeedsReload=false;
 if(revision)poster.src="/web-galerias/assets/intro-poster"+revision;
 if(posterReady()){poster.hidden=false;placeholder.hidden=true;}
 else if(mode==="photo"){poster.hidden=false;placeholder.hidden=true;}
 intro.classList.add("has-intro-video");
 video.pause();video.classList.remove("intro-playing");
 video.onloadedmetadata=null;video.onplaying=null;video.onerror=null;
 if(mode==="photo"){
  video.hidden=true;
  video.removeAttribute("src");video.load();
  sound.classList.add("hidden");
  return titleReady;
 }
 video.muted=true;video.autoplay=true;video.loop=true;video.playsInline=true;
 video.preload="auto";
 video.onloadedmetadata=()=>{
  video.hidden=false;placeholder.hidden=true;
  sound.classList.remove("hidden");
  sound.textContent="🔊 Activar sonido";
  video.play().catch(()=>{sound.textContent="▶ Reproducir video";});
 };
 video.onplaying=()=>{
  // The real video draws only after its first frame is ready. Until then
  // the already-preloaded JPEG stays visible, with no black flash.
  video.hidden=false;video.classList.add("intro-playing");
 };
 video.onerror=()=>{
  video.hidden=true;video.classList.remove("intro-playing");
  sound.classList.add("hidden");
  if(!posterReady()){
   intro.classList.remove("has-intro-video");
   placeholder.hidden=false;
  }
 };
 video.src="/web-galerias/assets/intro"+revision;
 return titleReady;
}
async function api(op,data={}){
 const raw=ADMIN_ACCOUNT_VIEW?
   (ADMIN_ACCOUNT_INITDATA||tg?.initData||""):(tg?.initData||"");
 if(!raw&&!BROWSER_ADMIN)
  throw Error("Debes abrir Web Galerías desde el botón del bot en Telegram.");
 const headers={"Content-Type":"application/json"};
 if(raw)headers["X-Telegram-Init-Data"]=raw;
 else headers["X-Gallery-Web"]="1";
 let payload={op,...data};
 if(ADMIN_ACCOUNT_VIEW){
  if(!Number.isSafeInteger(ADMIN_ACCOUNT_UID)||ADMIN_ACCOUNT_UID<=0)
   throw Error("Cuenta de usuario no válida.");
  const reads={
   bootstrap:"admin-view-bootstrap",
   "user-collections":"admin-view-collections",
   "favorites-items":"admin-view-favorites",
   items:"admin-view-items",
   "locate-media":"admin-view-locate",
   "favorite-set":"admin-view-favorite-set",
   "media-status":"admin-view-media-status",
   "media-priority":"admin-view-media-priority",
   "quota-plus-checkout":"admin-view-quota-plus-checkout",
   "notices-count":"admin-view-notices-count",
   "notices-list":"admin-view-notices-list",
   "notices-mark-read":"admin-view-notices-mark-read",
   "chat-unread":"admin-view-chat-unread"
  };
  const adminOps=new Set([
   "admin-media-remove","admin-mirror-download-dates",
   "admin-mirror-download-items","admin-user-personal-delete",
   "admin-user-personal-remove","admin-view-remove-favorites"
  ]);
  if(reads[op])payload={...data,op:reads[op],telegram_id:ADMIN_ACCOUNT_UID};
  else if(op.startsWith("personal-"))
   payload={...data,op:"admin-view-"+op,telegram_id:ADMIN_ACCOUNT_UID};
  else if(adminOps.has(op))
   payload={...data,op,telegram_id:ADMIN_ACCOUNT_UID};
  else if(op==="reserve"){
   // Downloads here belong to the administrator; never debit the customer.
   payload={...data,op};
  }else if(op==="progress-save"||op==="presence")return {ok:true};
  else throw Error("Esta acción no está disponible en la vista administrativa.");
 }
 const r=await fetch(ENDPOINT,{method:"POST",cache:"no-store",credentials:"same-origin",
  headers,body:JSON.stringify(payload)});
 if(!r.ok){let text=await r.text();text=text.replace(/<[^>]*>/g," ").trim();
  throw Error(text.slice(0,240)||"Servicio no disponible.");}
 return r.json();
}
async function refresh(lite=false){
 // The promo background is available immediately. Never show an out-of-date
 // title or CTA while bootstrap or custom images are still loading.
 const intro=$("intro");
 const generation=!lite?((S.introRenderGeneration||0)+1):0;
 if(!lite){
  S.introRenderGeneration=generation;
  intro.classList.add("intro-loading");
  intro.setAttribute("aria-busy","true");
 }
 S.me=await api("bootstrap",{name:tg?.initDataUnsafe?.user?.first_name||""});
 // The first-paint entry button was disabled in HTML; only signed,
 // authenticated bootstrap may make it interactive.
 $("enter").disabled=false;
 const titleReady=!lite?applyDesign(S.me.design):Promise.resolve();
 S.folders=S.me.folders;
 S.personal=S.me.personal_folders||[];renderFolders();
 $("admin-open").classList.toggle("hidden",!S.me.admin||ADMIN_ACCOUNT_VIEW);
 $("stat-folders").textContent=pretty(S.folders.length);
 $("stat-photos").textContent=pretty(S.me.photos);
 $("stat-videos").textContent=pretty(S.me.videos);
 updateQuotaStatus();
 let buttonReady=Promise.resolve();
 if(!S.me.allowed){
  if(S.me.checkout_enabled){
   buttonReady=renderEntryButtonLabel("COMPRAR MEMBRESÍA",true);
   $("enter").textContent="COMPRAR MEMBRESÍA";
   // Provider branding only appears when the user opens hosted checkout.
   $("intro-notice").textContent="";
  }else{
   buttonReady=renderEntryButtonLabel(S.me.design?.button_label||"ENTRAR A LA GALERÍA");
   $("intro-notice").textContent=S.me.pilot?
    "Prueba privada: solamente usuarios autorizados.":"Membresía necesaria para entrar";
  }
 }else{
  buttonReady=renderEntryButtonLabel(S.me.design?.button_label||"ENTRAR A LA GALERÍA");
  $("intro-notice").textContent=S.me.admin?"Acceso de administrador":"Acceso verificado";
 }
 $("intro-notice").classList.add("hidden");
 if(!lite){
  if(S.me.allowed)refreshCollections().catch(()=>{});
  await Promise.all([titleReady,buttonReady]);
  if(generation===S.introRenderGeneration){
   intro.classList.remove("intro-loading");
   intro.setAttribute("aria-busy","false");
  }
 }
}
function renderLibraryExtras(){
 if(!S.me?.allowed)return;
 // The resume shortcut now belongs only below its matching gallery card.
 $("favorites-counter").textContent=S.collectionsReady?
  pretty(S.favoritesCount)+" archivos ›":"Fotos y videos guardados ›";
}
async function refreshCollections(force=false){
 if(!S.me?.allowed||S.collectionsPending)return;
 if(!force&&S.collectionsReady&&Date.now()-S.collectionsAt<30000)return;
 S.collectionsPending=true;
 try{
  const data=await api("user-collections");
  if(!S.me?.allowed)return;
  S.resumes=Array.isArray(data.resume)?data.resume:[];
  S.favoritesCount=Number(data.favorites_count)||0;
  S.collectionsReady=true;
  S.collectionsAt=Date.now();
  renderLibraryExtras();
  renderFolders();
  if(!ADMIN_ACCOUNT_VIEW)getCustomerOffersLink().catch(()=>{}); // Optional warmup.
 }catch(_){/* A gallery must remain usable if optional shortcuts fail. */}
 finally{S.collectionsPending=false;}
}
async function resumeMedia(media){
 if(S.resuming)return;
 S.resuming=true;
 try{
  const data=await api("locate-media",{media});
  ++S.epoch;
  S.folder=data.folder;S.items=data.items;S.next=data.next;
  S.search="";S.filter="all";S.selection=false;S.selected.clear();
  $("query").value="";$("album-title").textContent=data.folder.name;
  $("album-subtitle").textContent="Retomando tu última foto o video";
  $("filter-toggle").textContent="Mostrar todo ▾";
  $("filter-menu").classList.add("hidden");
  renderDropTargets();updateSelection();show("album");renderItems();
  $("load-more").classList.toggle("hidden",!S.next);
  const index=S.items.findIndex(item=>Number(item.id)===Number(data.focus));
  if(index>=0)openViewer(index);
 }catch(e){
  notice("No se puede retomar ese archivo. Puede que ya no esté disponible.");
  S.resumes=S.resumes.filter(p=>Number(p.media)!==Number(media));
  renderLibraryExtras();
 }finally{S.resuming=false;}
}
function scheduleViewProgress(m){
 // Never overwrite customer's watch position while admin inspects their account.
 if(ADMIN_ACCOUNT_VIEW||!S.me?.allowed||!m?.id)return;
 clearTimeout(S.progressTimer);
 const media=m.id;
 S.progressTimer=setTimeout(async()=>{
  try{
   const result=await api("progress-save",{media});
   S.resumes=S.resumes.filter(p=>Number(p.folder)!==Number(result.folder));
   const folder=S.folders.find(f=>Number(f.id)===Number(result.folder));
   S.resumes.unshift({folder:result.folder,media:result.media,
     name:folder?.name||"Tu última galería",updated:Date.now()/1000});
   renderLibraryExtras();
  }catch(_){/* Do not interrupt photo/video playback on a slow connection. */}
 },350);
}
function updateViewerFavorite(){
 const item=S.items[S.viewer],button=$("viewer-favorite");
 button.textContent=item?.favorite?"♥":"♡";
 button.setAttribute("aria-pressed",String(!!item?.favorite));
 button.setAttribute("aria-label",item?.favorite?
  "Quitar de mis favoritos":"Agregar a mis favoritos");
 button.title=item?.favorite?"Quitar de mis favoritos":"Guardar en mis favoritos";
}
async function toggleViewerFavorite(){
 const item=S.items[S.viewer],button=$("viewer-favorite");
 if(!item||button.disabled)return;
 button.disabled=true;
 const next=!item.favorite;
 try{
  await api("favorite-set",{media:item.id,favorite:next});
  item.favorite=next;
  if(S.folder?.favorites&&!next)S.favoritesDirty=true;
  updateViewerFavorite();
  renderItems();
  S.collectionsAt=0;
  refreshCollections(true).catch(()=>{});
  notice(next?"Guardado en Mis favoritos.":"Quitado de Mis favoritos.");
 }catch(e){notice(e.message);}
 finally{button.disabled=false;}
}
// Double tap adds a favorite. A second double tap never removes it;
 // removal remains an explicit action on the viewer heart.
const favoriteTapPending=new Set();
async function saveFavoriteFromDoubleTap(item){
 if(!item||!S.me?.allowed||item.favorite||favoriteTapPending.has(item.id))return;
 favoriteTapPending.add(item.id);
 try{
  await api("favorite-set",{media:item.id,favorite:true});
  item.favorite=true;
  for(const current of S.items)if(current.id===item.id)current.favorite=true;
  if(S.items.some(current=>current.id===item.id)){
   renderItems();
   if(!$("viewer").classList.contains("hidden")&&S.items[S.viewer]?.id===item.id)
    updateViewerFavorite();
  }
  S.collectionsAt=0;
  refreshCollections(true).catch(()=>{});
  tg?.HapticFeedback?.notificationOccurred?.("success");
  notice("♥ Guardado en Mis favoritos.");
 }catch(e){notice(e.message);}
 finally{favoriteTapPending.delete(item.id);}
}

function renderFolders(){
 $("folder-counter").textContent=pretty(S.folders.length)+" carpetas";
 const root=$("folders");root.replaceChildren();
 for(const folder of S.folders){
  const card=document.createElement("article");card.className="folder-card";card.tabIndex=0;
  const cover=document.createElement("div");cover.className="folder-cover";
  if(folder.cover){const img=document.createElement("img");img.src=folder.cover;img.loading="lazy";img.decoding="async";
   img.onerror=()=>{img.remove();cover.append(document.createTextNode("▧"));};cover.append(img);}
  else{const icon=document.createElement("span");icon.className="folder-icon";icon.textContent="▧";cover.append(icon);}
  const title=document.createElement("h3");title.textContent=folder.name;
  const stats=document.createElement("p");
  stats.textContent=pretty(folder.photos)+" fotos · "+pretty(folder.videos)+" videos"+
   (S.me?.admin && folder.pending?" · respaldando…":"");
  card.append(cover,title,stats);
  const resume=S.resumes.find(p=>Number(p.folder)===Number(folder.id));
  if(resume){
   const continueButton=document.createElement("button");
   continueButton.type="button";continueButton.className="folder-continue";
   continueButton.textContent="↪ Continuar viendo";
   continueButton.onclick=e=>{e.stopPropagation();resumeMedia(resume.media);};
   continueButton.onkeydown=e=>e.stopPropagation();
   card.append(continueButton);
  }
  card.onclick=()=>openFolder(folder);
  card.onkeydown=e=>{if(e.key==="Enter")openFolder(folder);};
  root.append(card);
 }
 if(!S.folders.length)root.innerHTML='<div class="empty">El administrador todavía no ha publicado carpetas.</div>';
 renderPersonalFolders();
}
function setPersonalMenuOpen(open){
 S.personalMenuOpen=!!open;
 $("personal-folders").classList.toggle("hidden",!open);
 $("personal-folders-toggle").setAttribute("aria-expanded",String(!!open));
}
function setDropMenuOpen(open){
 S.dropMenuOpen=!!open;
 $("folder-drop-menu").classList.toggle("hidden",!open);
 $("folder-drop-toggle").setAttribute("aria-expanded",String(!!open));
}
function makeFolderThumbnail(url){
 const wrap=document.createElement("span");wrap.className="personal-folder-thumb";
 if(url){
  const img=document.createElement("img");img.src=url;img.alt="";
  img.loading="lazy";img.decoding="async";
  img.onerror=()=>{img.remove();wrap.textContent="📁";};
  wrap.append(img);
 }else wrap.textContent="📁";
 return wrap;
}
function renderPersonalFolders(){
 $("personal-section").classList.toggle("hidden",!S.me?.allowed);
 $("personal-folder-count").textContent=pretty(S.personal.length);
 $("folder-drop-count").textContent=pretty(S.personal.length);
 const root=$("personal-folder-entries");root.replaceChildren();
 for(const folder of S.personal){
  const line=document.createElement("div");line.className="personal-folder-entry";
  const main=document.createElement("button");main.type="button";
  main.className="personal-folder-open";
  main.append(makeFolderThumbnail(folder.cover));
  const text=document.createElement("span");text.className="personal-folder-open-text";
  const title=document.createElement("strong");title.textContent=folder.name;
  const count=document.createElement("small");count.textContent=pretty(folder.count)+" archivos";
  text.append(title,count);main.append(text);
  main.onclick=()=>{setPersonalMenuOpen(false);openFolder({...folder,personal:true});};
  const tools=document.createElement("div");tools.className="personal-folder-tools";
  const change=document.createElement("button");change.type="button";
  change.textContent="▧";change.title="Cambiar portada";change.setAttribute("aria-label","Cambiar portada de "+folder.name);
  change.onclick=()=>{S.coverFolder=folder;$("personal-cover-file").click();};
  const rename=document.createElement("button");rename.type="button";rename.textContent="✎";
  rename.title="Renombrar";rename.setAttribute("aria-label","Cambiar nombre de "+folder.name);
  rename.onclick=()=>renamePersonalFolder(folder);
  const erase=document.createElement("button");erase.type="button";erase.textContent="×";
  erase.title="Eliminar";erase.setAttribute("aria-label","Eliminar "+folder.name);
  erase.onclick=()=>deletePersonalFolder(folder);
  tools.append(change,rename);
  if(folder.has_custom_cover){
   const reset=document.createElement("button");reset.type="button";
   reset.textContent="↺";reset.title="Portada automática";
   reset.setAttribute("aria-label","Restablecer portada automática de "+folder.name);
   reset.onclick=()=>resetPersonalCover(folder);
   tools.append(reset);
  }
  tools.append(erase);line.append(main,tools);root.append(line);
 }
 setPersonalMenuOpen(S.personalMenuOpen);
 renderDropTargets();
}
async function createPersonalFolder(ids=[]){
 const selected=Array.isArray(ids)?[...new Set(ids)]:[];
 if(selected.length>selectionLimit())return notice("Puedes agregar hasta "+selectionLimit()+" archivos.");
 if(S.personalSaving)return;
 const name=prompt(selected.length?"Nombre de la carpeta para estos "+selected.length+" archivos:":
                            "Nombre de tu nueva carpeta:");
 if(!name)return;
 S.personalSaving=true;
 try{
  const data=await api("personal-create",{name,ids:selected});
  if(selected.length){
   S.selection=false;S.selected.clear();updateSelection();renderItems();
  }
  await refresh(true);
  setDropMenuOpen(false);
  notice(selected.length?data.name+": "+selected.length+" archivos guardados.":
                        "Carpeta creada: "+data.name);
  return data;
 }catch(e){notice(e.message);}
 finally{S.personalSaving=false;}
}
async function resetPersonalCover(folder){
 try{
  await api("personal-cover-reset",{personal_folder:folder.id});
  await refresh(true);notice("Restablecida la miniatura automática.");
 }catch(e){notice(e.message);}
}
async function imageToCoverBase64(file){
 if(!file||!["image/jpeg","image/png","image/webp"].includes(file.type)||file.size>15*1024*1024)
  throw Error("Elige una foto JPG, PNG o WebP de hasta 15 MB.");
 const url=URL.createObjectURL(file);
 try{
  const image=new Image();
  await new Promise((resolve,reject)=>{
   image.onload=resolve;image.onerror=()=>reject(Error("No se puede abrir la foto."));
   image.src=url;
  });
  if(!image.naturalWidth||!image.naturalHeight||image.naturalWidth*image.naturalHeight>45e6)
   throw Error("La foto tiene dimensiones demasiado grandes.");
  const scale=Math.min(1,900/Math.max(image.naturalWidth,image.naturalHeight));
  const canvas=document.createElement("canvas");
  canvas.width=Math.max(1,Math.round(image.naturalWidth*scale));
  canvas.height=Math.max(1,Math.round(image.naturalHeight*scale));
  const ctx=canvas.getContext("2d",{alpha:false});
  if(!ctx)throw Error("No se pudo preparar la portada.");
  ctx.fillStyle="#ffffff";ctx.fillRect(0,0,canvas.width,canvas.height);
  ctx.drawImage(image,0,0,canvas.width,canvas.height);
  for(const quality of [.82,.70,.55,.42]){
   const jpg=canvas.toDataURL("image/jpeg",quality).split(",")[1];
   if(jpg&&jpg.length<=340000)return jpg;
  }
  throw Error("Esta foto es demasiado grande incluso después de reducirla.");
 }finally{URL.revokeObjectURL(url);}
}
async function uploadPersonalCover(file){
 const folder=S.coverFolder;if(!folder||!file)return;
 try{
  const image=await imageToCoverBase64(file);
  await api("personal-cover-upload",{personal_folder:folder.id,image});
  await refresh(true);
  notice("Foto de presentación actualizada.");
 }catch(e){notice(e.message);}
 finally{$("personal-cover-file").value="";S.coverFolder=null;}
}
async function renamePersonalFolder(folder){
 const name=prompt("Nuevo nombre de la carpeta:",folder.name);
 if(!name||name===folder.name)return;
 try{
  await api("personal-rename",{personal_folder:folder.id,name});
  if(S.folder?.personal&&S.folder.id===folder.id){S.folder.name=name;$("album-title").textContent=name;}
  await refresh(true);
 }catch(e){notice(e.message);}
}
async function deletePersonalFolder(folder){
 if(!confirm("¿Eliminar la carpeta '"+folder.name+"'? Las fotos originales no se borrarán."))return;
 try{await api("personal-delete",{personal_folder:folder.id});await refresh(true);notice("Carpeta personal eliminada.");}
 catch(e){notice(e.message);}
}
function renderDropTargets(){
 const tray=$("folder-drop-tray"),root=$("folder-drop-targets");
 root.replaceChildren();
 tray.classList.toggle("hidden",!S.me?.allowed);
 $("folder-drop-count").textContent=pretty(S.personal.length);
 for(const folder of S.personal){
  const b=document.createElement("button");b.type="button";
  b.className="folder-drop-option";b.setAttribute("role","menuitem");
  b.dataset.folderDrop=String(folder.id);
  const thumb=document.createElement("span");thumb.className="folder-target-cover";
  if(folder.cover){
   const img=document.createElement("img");img.src=folder.cover;img.alt="";
   img.loading="lazy";img.onerror=()=>{img.remove();thumb.textContent="📁";};
   thumb.append(img);
  }else thumb.textContent="📁";
  const title=document.createElement("span");title.className="folder-target-name";
  title.textContent=folder.name;
  b.append(thumb,title);
  b.ondragover=e=>{e.preventDefault();b.classList.add("drag-target");};
  b.ondragleave=()=>b.classList.remove("drag-target");
  b.ondrop=e=>{e.preventDefault();b.classList.remove("drag-target");
   const id=Number(e.dataTransfer.getData("text/plain"));
   const ids=selectedDragIds(id);
   if(ids.length)addToPersonalFolder(folder.id,ids);
  };
  b.onclick=()=>{
   if(S.selected.size)addToPersonalFolder(folder.id,[...S.selected]);
   else {setDropMenuOpen(false);openFolder({...folder,personal:true});}
  };
  root.append(b);
 }
 setDropMenuOpen(S.dropMenuOpen);
}
function selectedDragIds(id){
 // Arrastrar cualquiera de los elementos seleccionados mueve la selección
 // completa; sin selección previa, se mueve solamente el archivo arrastrado.
 const mediaId=Number(id);
 if(!Number.isSafeInteger(mediaId)||mediaId<=0)return [];
 const ids=S.selected.has(mediaId)?[...S.selected]:[mediaId];
 return ids.length<=selectionLimit()?ids:[];
}
async function addToPersonalFolder(folder,ids){
 const unique=[...new Set(ids)].filter(id=>Number.isSafeInteger(id)&&id>0);
 if(!unique.length)return;
 if(unique.length>selectionLimit())return notice("Puedes guardar hasta "+selectionLimit()+" archivos a la vez.");
 if(S.personalSaving)return;
 S.personalSaving=true;
 try{
  await api("personal-add",{personal_folder:folder,ids:unique});
  S.selection=false;S.selected.clear();updateSelection();renderItems();
  await refresh(true);
  setDropMenuOpen(false);
  notice(unique.length===1?"Archivo guardado en tu carpeta personal.":
     unique.length+" archivos guardados en tu carpeta personal.");
 }catch(e){notice(e.message);}
 finally{S.personalSaving=false;}
}

async function openFolder(folder){
 finishDrag();
 setDropMenuOpen(false);
 S.folder=folder;S.items=[];S.next=null;S.search="";S.filter="all";
 S.selection=false;S.selected.clear();
 $("query").value="";$("album-title").textContent=folder.name;
 $("album-subtitle").textContent=folder.favorites?
   "Tus fotos y videos guardados":
   folder.downloads?pretty(folder.count)+" archivos contabilizados":
   folder.personal?pretty(folder.count)+" archivos":
   pretty(folder.photos)+" fotos · "+pretty(folder.videos)+" videos";
 $("favorites-notice").classList.toggle("hidden",!folder.favorites);
 S.favoritesDirty=false;
 $("filter-toggle").textContent="Mostrar todo ▾";$("filter-menu").classList.add("hidden");
 $("filter-toggle").setAttribute("aria-expanded","false");
 renderDropTargets();
 const downloads=ADMIN_ACCOUNT_VIEW&&!!folder.downloads;
 $("query").parentElement.classList.toggle("hidden",downloads);
 $("filter-toggle").closest(".album-filter-bar").classList.toggle("hidden",downloads);
 $("folder-drop-tray").classList.toggle("hidden",downloads);
 $("select-mode").textContent="Seleccionar";updateSelection();show("album");await loadItems(false);
}
async function loadItems(older=false){
 if(!S.folder)return;
 const epoch=++S.epoch;
 $("empty-gallery").classList.add("hidden");
 if(!older){S.items=[];$("album-grid").replaceChildren();}
 try{
  const data=ADMIN_ACCOUNT_VIEW&&S.folder.downloads?
   await api("admin-mirror-download-items",{
    date:S.folder.date,offset:older?(S.next||0):0}):
   await api(S.folder.favorites?"favorites-items":"items",{
    ...(S.folder.favorites?{}:S.folder.personal?
      {collection:S.folder.id}:{folder:S.folder.id}),
    kind:S.filter,query:S.search,cursor:older?(S.next||0):0});
  if(epoch!==S.epoch)return;
  S.items.push(...data.items);S.next=data.next;
  renderItems();$("load-more").classList.toggle("hidden",!S.next);
  $("empty-gallery").classList.toggle("hidden",S.items.length>0);
  $("empty-gallery").textContent=S.search?"No se encontraron fotos o videos relacionados.":"Todavía no hay fotos o videos disponibles en esta carpeta.";
 }catch(e){if(epoch===S.epoch)notice(e.message);}
}
// Defer one touch click briefly so a second tap saves the photo/video
// without opening the viewer first. Keyboard and desktop clicks stay instant.
let pendingMediaTap=null;
function cancelPendingMediaTap(){
 if(pendingMediaTap)clearTimeout(pendingMediaTap.timer);
 pendingMediaTap=null;
}
function onMediaTileClick(item,index,event){
 if(S.suppressClick===item.id){S.suppressClick=null;cancelPendingMediaTap();return;}
 if(S.selection){cancelPendingMediaTap();toggleSelection(item.id);return;}
 const coarse=event?.pointerType==="touch"||
   (event?.detail!==0&&window.matchMedia?.("(pointer:coarse)")?.matches);
 if(!coarse){cancelPendingMediaTap();openViewer(index);return;}
 const now=Date.now();
 const isDouble=pendingMediaTap?.id===item.id&&
   now-pendingMediaTap.at<=330;
 cancelPendingMediaTap();
 if(isDouble){saveFavoriteFromDoubleTap(item);return;}
 const folder=S.folder,epoch=S.epoch;
 pendingMediaTap={id:item.id,at:now,timer:setTimeout(()=>{
  pendingMediaTap=null;
  const current=S.items.findIndex(m=>m.id===item.id);
  if(S.folder===folder&&S.epoch===epoch&&!S.selection&&current>=0)
   openViewer(current);
 },275)};
}

function renderItems(){
 const grid=$("album-grid");grid.replaceChildren();
 S.items.forEach((m,index)=>{
  const b=document.createElement("button");b.className="media-tile"+(S.selected.has(m.id)?" selected":"");
  b.type="button";b.dataset.mediaId=String(m.id);b.draggable=!!window.matchMedia?.("(pointer:fine)")?.matches;
  b.setAttribute("aria-label",m.kind==="video"?"Abrir video":"Abrir foto");
  if(m.thumb){const img=document.createElement("img");img.src=m.thumb;img.loading="lazy";img.decoding="async";img.alt="";
   img.onerror=()=>{img.remove();fallback(b,m);};b.append(img);}else fallback(b,m);
  if(m.kind==="video"){const mark=document.createElement("span");mark.className="video-mark";mark.textContent="▶";b.append(mark);}
  if(m.favorite){
   const heart=document.createElement("span");heart.className="favorite-mark";
   heart.textContent="♥";heart.setAttribute("aria-hidden","true");b.append(heart);
  }
  attachLongPress(b,m,index);
  b.onclick=e=>onMediaTileClick(m,index,e);
  grid.append(b);
 });
}
function showDragPreview(m,ids,x,y){
 const host=$("drag-preview"),img=$("drag-preview-image");
 const fallback=$("drag-preview-fallback");
 img.classList.toggle("hidden",!m.thumb);
 fallback.classList.toggle("hidden",!!m.thumb);
 if(m.thumb)img.src=m.thumb;
 fallback.textContent=m.kind==="video"?"▶":"▧";
 $("drag-preview-count").textContent=String(ids.length);
 host.classList.remove("hidden");
 moveDragPreview(x,y);
}
function moveDragPreview(x,y){
 const ghost=$("drag-preview");
 ghost.style.left=x+"px";ghost.style.top=y+"px";
}
function hideDragPreview(){
 $("drag-preview").classList.add("hidden");
 $("folder-drop-toggle").classList.remove("drag-menu-armed");
}
function dragMenuScroll(){
 if(!S.drag)return;
 const menu=$("folder-drop-menu");
 const x=S.drag.x,y=S.drag.y;
 let insideMenu=false;
 if(S.dropMenuOpen&&!menu.classList.contains("hidden")){
  const rect=menu.getBoundingClientRect();
  insideMenu=x>=rect.left-12&&x<=rect.right+12&&y>=rect.top-30&&y<=rect.bottom+30;
  if(insideMenu){
   const zone=57;
   let delta=0;
   if(y<rect.top+zone)delta=-Math.min(16,Math.ceil((rect.top+zone-y)/3));
   else if(y>rect.bottom-zone)delta=Math.min(16,Math.ceil((y-(rect.bottom-zone))/3));
   if(delta){menu.scrollTop+=delta;dragHoverAt(x,y);}
  }
 }
 if(!insideMenu){
  // Mantener el dedo cerca del borde desplaza la galería para poder llevar
  // una selección de fotos lejanas hasta el menú sin soltarla.
  const edge=65;
  if(y<edge)window.scrollBy(0,-13);
  else if(y>window.innerHeight-edge)window.scrollBy(0,13);
  dragHoverAt(x,y);
 }
 S.drag.raf=requestAnimationFrame(dragMenuScroll);
}
function dragHoverAt(x,y){
 if(!S.drag)return;
 S.drag.x=x;S.drag.y=y;
 const element=document.elementFromPoint(x,y);
 const toggle=element?.closest("[data-folder-drop-trigger]");
 if(toggle){
  setDropMenuOpen(true);
  $("folder-drop-toggle").classList.add("drag-menu-armed");
 }else $("folder-drop-toggle").classList.remove("drag-menu-armed");
 const option=element?.closest("[data-folder-drop]");
 const next=option&&$("folder-drop-menu").contains(option)?option:null;
 if(S.drag.hover!==next){
  if(S.drag.hover)S.drag.hover.classList.remove("drag-target");
  S.drag.hover=next;
  if(next)next.classList.add("drag-target");
 }
}
function finishDrag(){
 if(S.drag){
  if(S.drag.raf)cancelAnimationFrame(S.drag.raf);
  if(S.drag.hover)S.drag.hover.classList.remove("drag-target");
  S.drag=null;
 }
 hideDragPreview();
}
function attachLongPress(b,m,index){
 let timer=null,held=false,active=false,startX=0,startY=0;
 const clear=()=>{clearTimeout(timer);timer=null;};
 b.addEventListener("contextmenu",e=>e.preventDefault());
 b.addEventListener("dragstart",e=>{
  // Escritorio: el elemento arrastrado representa la selección completa.
  active=false;held=false;clear();
  const ids=selectedDragIds(m.id);
  if(!ids.length){e.preventDefault();return;}
  e.dataTransfer.setData("text/plain",String(m.id));
  e.dataTransfer.effectAllowed="copy";
  showDragPreview(m,ids,60,60);
  e.dataTransfer.setDragImage?.($("drag-preview"),43,43);
  setTimeout(()=>hideDragPreview(),0);
 });
 b.addEventListener("dragend",()=>{
  active=false;held=false;clear();b.classList.remove("dragging");
  finishDrag();
 });
 b.addEventListener("pointerdown",e=>{
  if(e.pointerType==="mouse"&&e.button!==0)return;
  startX=e.clientX;startY=e.clientY;held=false;active=true;
  clear();
  timer=setTimeout(()=>{
   if(!active)return;
   // In search results and personal folders, holding a touch opens actions.
   // Regular galleries keep the existing drag-to-folder gesture unchanged.
   if((S.folder?.favorites||S.folder?.personal||S.search.trim())&&e.pointerType!=="mouse"){
    active=false;held=false;clear();
    S.suppressClick=m.id;
    setTimeout(()=>{if(S.suppressClick===m.id)S.suppressClick=null;},600);
    window.CubanitasTools?.mediaActions?.(m);
    tg?.HapticFeedback?.selectionChanged?.();
    return;
   }
   if(!S.selected.has(m.id)&&S.selected.size>=selectionLimit()){
    notice("Puedes seleccionar hasta "+selectionLimit()+" archivos.");return;
   }
   held=true;S.selection=true;
   S.selected.add(m.id);
   if(e.pointerType!=="mouse"){try{b.setPointerCapture(e.pointerId);}catch(_){}}
   updateSelection();b.classList.add("selected","dragging");
   const ids=selectedDragIds(m.id);
   S.drag={hover:null,x:startX,y:startY,raf:0};
   showDragPreview(m,ids,startX,startY);
   S.drag.raf=requestAnimationFrame(dragMenuScroll);
   tg?.HapticFeedback?.selectionChanged?.();
  },450);
 });
 b.addEventListener("pointermove",e=>{
  if(!active)return;
  if(!held&&Math.hypot(e.clientX-startX,e.clientY-startY)>12){
   clear();return;
  }
  if(held){
   moveDragPreview(e.clientX,e.clientY);
   dragHoverAt(e.clientX,e.clientY);
  }
 });
 b.addEventListener("touchmove",e=>{
  // Evitar scroll de la página solo durante arrastre; la lista de carpetas
  // tiene desplazamiento automático hacia arriba y abajo.
  if(held)e.preventDefault();
 },{passive:false});
 b.addEventListener("pointerup",e=>{
  if(!active)return;active=false;clear();
  if(!held)return;
  const destination=document.elementFromPoint(e.clientX,e.clientY)?.closest("[data-folder-drop]");
  S.suppressClick=m.id;setTimeout(()=>{if(S.suppressClick===m.id)S.suppressClick=null;},450);
  const ids=selectedDragIds(m.id);
  finishDrag();
  b.classList.remove("dragging");
  if(destination&&$("folder-drop-menu").contains(destination)&&ids.length){
   if(destination.dataset.folderDrop==="new")createPersonalFolder(ids);
   else addToPersonalFolder(Number(destination.dataset.folderDrop),ids);
  }else renderItems();
 });
 b.addEventListener("pointercancel",()=>{
  active=false;held=false;clear();finishDrag();b.classList.remove("dragging");
 });
 b.addEventListener("pointerleave",()=>{if(!held)clear();});
}
function fallback(b,m){const p=document.createElement("div");p.className="fallback";p.textContent=m.kind==="video"?"▶":"▧";b.prepend(p);}
function updateSelection(){
 $("selection-bar").classList.toggle("hidden",!S.selection);
 $("album-actions").classList.toggle("hidden",S.selection);
 $("selection-count").textContent=S.selected.size+" de "+selectionLimit();
 $("selection-admin-delete").classList.toggle("hidden",!(S.me?.admin||S.me?.admin_controls));
 $("selection-admin-delete").disabled=!S.selected.size;
 const trayText=$("folder-drag-help");
 if(trayText)trayText.textContent=S.selected.size>1?
    "Arrastra cualquiera de los "+S.selected.size+" archivos seleccionados a una carpeta:":
    "Mantén pulsada una foto o video y arrástralo a tu carpeta:";
 $("select-mode").textContent=S.selection?"Listo":"Seleccionar";
 $("selection-download").disabled=!S.selected.size||
   [...S.selected].some(id=>!S.items.find(x=>x.id===id)?.available);
 $("selection-organize").disabled=!S.selected.size;
 const inFavorites=!!S.folder?.favorites;
 $("selection-organize").classList.toggle("hidden",inFavorites);
 $("selection-original").classList.toggle("hidden",
   !(inFavorites||(ADMIN_ACCOUNT_VIEW&&S.me?.admin_controls)));
 $("selection-original").disabled=S.selected.size!==1;
}
function toggleSelection(id){
 const media=S.items.find(m=>m.id===id);
 if(!media)return;
 if(S.selected.has(id))S.selected.delete(id);
 else if(S.selected.size<selectionLimit())S.selected.add(id);
 else return notice("Puedes seleccionar hasta "+selectionLimit()+" archivos por operación.");
 updateSelection();renderItems();
}
function showViewerMedia(){
  if(!S.items.length)return;
  const m=S.items[S.viewer];$("viewer-counter").textContent=(S.viewer+1)+" / "+S.items.length;
  renderViewerWatermark();
  const img=$("view-image"),video=$("view-video");
  video.pause();video.removeAttribute("src");img.style.transform="scale(1)";
  const full=!!m.available&&!!m.view;
  if(m.kind==="video"&&full&&m.mime!=="image/gif"){
    img.hidden=true;video.hidden=false;video.src=m.view;
  }else{
    video.hidden=true;img.hidden=false;
    if(full||m.thumb)img.src=full?m.view:m.thumb;
    else img.removeAttribute("src");
    img.alt=full?"Vista completa":"Vista previa; original en preparación";
  }
  const canCover=!!(S.folder?.personal&&m.kind==="photo"&&m.thumb);
  $("viewer-set-cover").classList.toggle("hidden",!canCover);
  document.querySelector("#viewer .viewer-bottom").classList.toggle("with-cover",canCover);
  $("viewer-download").disabled=!full;
  $("viewer-download").title=full?"Guardar archivo":"Original en preparación";
  $("viewer-admin-delete").classList.toggle("hidden",!(S.me?.admin||S.me?.admin_controls));
  $("viewer-admin-original")?.classList.toggle("hidden",
    !(ADMIN_ACCOUNT_VIEW&&S.me?.admin_controls));
  updateViewerFavorite();
  scheduleViewProgress(m);
  $("viewer-prev").disabled=S.viewer===0;$("viewer-next").disabled=S.viewer===S.items.length-1;
}
// Requests for a selected video interrupt background copies on its source.
// Only the viewer's polling is cancelled when moving away; the backup is durable.
let videoPriorityTimer=null,videoPriorityGeneration=0;
function stopVideoPriority(){
 ++videoPriorityGeneration;
 if(videoPriorityTimer)clearTimeout(videoPriorityTimer);
 videoPriorityTimer=null;
 const indicator=$("viewer-priority-status");
 if(indicator)indicator.classList.add("hidden");
}
function priorityText(message){
 const indicator=$("viewer-priority-status");
 if(!indicator)return;
 indicator.textContent=message||"";
 indicator.classList.toggle("hidden",!message);
}
function beginVideoPriority(expected){
 stopVideoPriority();
 if(!expected||expected.kind!=="video"||expected.available)return;
 const generation=videoPriorityGeneration;
 const unchanged=()=>generation===videoPriorityGeneration&&
   !$("viewer").classList.contains("hidden")&&
   S.items[S.viewer]?.id===expected.id;
 const started=Date.now();
 let requested=false;
 priorityText("Preparando este video con prioridad…");
 const poll=async()=>{
  if(!unchanged())return;
  try{
   const result=await api(requested?"media-status":"media-priority",
                          {media:expected.id});
   requested=true;
   if(!unchanged())return;
   if(result.available&&result.view){
    expected.available=true;
    expected.view=result.view;
    expected.kind=result.kind||expected.kind;
    expected.mime=result.mime||expected.mime;
    if(result.thumb)expected.thumb=result.thumb;
    stopVideoPriority();
    showViewerMedia();
    const player=$("view-video");
    if(!player.hidden){
     try{player.load();player.play().catch(()=>{});}catch(_){}
    }
    return;
   }
   const state=result.priority_state||"queued";
   if(state==="running")priorityText("Descargando el video seleccionado…");
   else if(state==="waiting")priorityText(
     "La cuenta Telegram está ocupada; reintentando este video…");
   else priorityText("Tu video tiene prioridad de reproducción…");
   if(Date.now()-started>=300000){
    priorityText("El video sigue en preparación. Puedes volver a abrirlo después.");
    return;
   }
   videoPriorityTimer=setTimeout(poll,state==="waiting"?5000:2500);
  }catch(e){
   if(!unchanged())return;
   if(!requested){
    priorityText("No se pudo priorizar este video. Inténtalo de nuevo al abrirlo.");
    return;
   }
   if(Date.now()-started>=300000){
    priorityText("El video sigue pendiente. Vuelve a intentarlo más tarde.");
    return;
   }
   videoPriorityTimer=setTimeout(poll,8000);
  }
 };
 void poll();
}
async function refreshAdminViewerMedia(index,expected){
 // Admin's embedded user view intentionally does not refresh every list while
 // scrolling. A preview may already have become a playable original.
 if(!ADMIN_ACCOUNT_VIEW||!expected?.id)return;
 try{
  const result=await api("media-status",{media:expected.id});
  if($("viewer").classList.contains("hidden")||
     S.items[S.viewer]?.id!==expected.id)return;
  const item=S.items[S.viewer];
  const wasFull=!!item.available&&!!item.view;
  item.available=!!result.available;
  item.kind=result.kind||item.kind;
  item.mime=result.mime||item.mime;
  item.view=result.view||null;
  if(result.thumb)item.thumb=result.thumb;
  const video=$("view-video");
  // Don't interrupt an already-playing video merely to renew its URL.
  if(!wasFull||!item.available||video.error){
   showViewerMedia();
   if(item.available&&item.kind==="video"&&item.mime!=="image/gif"){
    try{$("view-video").load();}catch(_){}
   }
  }
  if(!item.available)notice("La miniatura está disponible; el original aún se está preparando.");
 }catch(e){
  // A temporary status error must not erase a previously available video.
  if(!expected.available)notice("No se pudo comprobar el original. Inténtalo nuevamente.");
 }
}
function openViewer(index){
 S.viewer=index;showViewerMedia();$("viewer").classList.remove("hidden");
 document.body.classList.add("gallery-viewer-open");setDropMenuOpen(false);lastPresence=0;galleryPresence();
 if(ADMIN_ACCOUNT_VIEW){
  void refreshAdminViewerMedia(index,S.items[index]);
 }
 beginVideoPriority(S.items[index]);
 if(S.items[index].kind!=="video"&&!S.items[index].available)
  notice("El original se está preparando; la miniatura se muestra si está disponible.");
}
function closeViewer(){
 stopVideoPriority();
 $("viewer").classList.add("hidden");
 document.body.classList.remove("gallery-viewer-open");
 $("view-video").pause();
 if(S.folder?.favorites&&S.favoritesDirty){
  S.favoritesDirty=false;
  return loadItems(false).catch(()=>{});
 }
}
function backToViewedMedia(){
 // The viewer is a temporary overlay, never an implicit jump to page one.
 const folder=S.folder,media=S.items[S.viewer];
 const mediaId=Number(media?.id);
 Promise.resolve(closeViewer()).then(()=>{
  if(S.folder!==folder||!Number.isSafeInteger(mediaId)||mediaId<=0)return;
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
   if(S.folder!==folder)return;
   const tile=$("album-grid").querySelector('[data-media-id="'+mediaId+'"]');
   if(!tile)return;
   tile.scrollIntoView({block:"center",inline:"nearest",behavior:"instant"});
   tile.classList.add("located-media");
   setTimeout(()=>tile.classList.remove("located-media"),1800);
  }));
 });
}
function moveViewer(delta){
 const i=S.viewer+delta;if(i<0||i>=S.items.length)return;
 stopVideoPriority();
 S.viewer=i;showViewerMedia();
 if(ADMIN_ACCOUNT_VIEW)void refreshAdminViewerMedia(i,S.items[i]);
 beginVideoPriority(S.items[i]);
}
function nativeDownload(data){
 const url=location.origin+data.url;
 if(tg?.downloadFile&&tg?.isVersionAtLeast?.("8.0")){
  tg.downloadFile({url,file_name:data.name||"web-galerias.zip"},accepted=>{
   if(accepted)notice("Descarga solicitada en Telegram. Para guardarla en Fotos, ábrela y elige Guardar.");
  });
 }else{
  const link=document.createElement("a");link.href=url;
  link.download=data.name||"web-galerias.zip";
  document.body.append(link);link.click();link.remove();
  notice("Archivo solicitado. En iPhone puedes guardarlo desde el menú Compartir.");
 }
}
function closeSaveSheet(){
 $("save-sheet").classList.add("hidden");S.prepared=null;S.saveData=null;
}
async function downloadIds(ids){
 if(!ids.length)return;
 if(ids.some(id=>!S.items.find(x=>x.id===id)?.available))
  return notice("Espera a que los originales seleccionados estén disponibles.");
 try{
  const data=await api("reserve",{ids});
  S.me.quota_unlimited=!!data.quota_unlimited;
  S.me.quota_daily_limit=data.quota_daily_limit;
  S.me.quota_tier=data.quota_tier;
  S.me.remaining=data.remaining;
  updateQuotaStatus();
  S.selected.clear();S.selection=false;updateSelection();renderItems();
  S.saveData=data;S.prepared=null;
  const total=(data.files||[]).reduce((sum,x)=>sum+Number(x.size||0),0);
  // Web Share sirve para pasar fotos/videos a la hoja nativa de iOS.
  // Por seguridad de memoria, los vídeos grandes y ZIP usan descarga nativa.
  if(!navigator.share||!window.File||total>64*1024*1024){
    nativeDownload(data);S.saveData=null;return;
  }
  try{
   const files=[];
   for(const entry of data.files){
    const r=await fetch(entry.url,{cache:"no-store"});
    if(!r.ok)throw Error("No se pudo preparar el archivo.");
    const blob=await r.blob();
    files.push(new File([blob],entry.name,{type:entry.mime||blob.type||
     "application/octet-stream"}));
   }
   if(navigator.canShare&&!navigator.canShare({files})){
    nativeDownload(data);S.saveData=null;return;
   }
   S.prepared=files;
   $("save-sheet-message").textContent=files.length===1?
     "Elige Guardar imagen o Guardar video en el menú de tu iPhone. La web no puede escribir en Fotos sin tu autorización.":
     "Elige Guardar imágenes o Guardar videos en el menú de tu iPhone. Si no aparece, usa la descarga ZIP.";
   $("save-share").classList.remove("hidden");
   $("save-sheet").classList.remove("hidden");
  }catch(e){
   notice("Usaremos la descarga de Telegram para este archivo.");
   nativeDownload(data);S.saveData=null;
  }
 }catch(e){notice(e.message);}
}
function openFolderPicker(){
 if(!S.selected.size)return;
 const root=$("folder-picker-list");root.replaceChildren();
 const newFolder=document.createElement("button");newFolder.type="button";
 newFolder.textContent="＋ Crear carpeta nueva";
 newFolder.onclick=async()=>{$("folder-picker").classList.add("hidden");await createPersonalFolder([...S.selected]);};
 root.append(newFolder);
 for(const folder of S.personal){
  const b=document.createElement("button");b.type="button";b.textContent="📁 "+folder.name;
  b.onclick=async()=>{
   $("folder-picker").classList.add("hidden");
   await addToPersonalFolder(folder.id,[...S.selected]);
  };
  root.append(b);
 }
 $("folder-picker").classList.remove("hidden");
}

function previewAdmin(){
 if(!S.admin)return;
 const root=$("admin-folders");root.replaceChildren();
 root.classList.add("folders","admin-gallery-grid");
 const readableReason={
  "no_disponible_en_origen":"Clasificación antigua: causa no verificada",
  "message_absent_pending_recheck":"Mensaje ausente temporalmente; se volverá a comprobar",
  "media_removed_pending_recheck":"Mensaje encontrado sin multimedia; se volverá a comprobar",
  "message_absent_after_rechecks":"Mensaje no recuperado tras tres comprobaciones",
  "media_removed_after_rechecks":"Multimedia no recuperado tras tres comprobaciones",
  "protected_or_temporary":"Contenido restringido o temporal",
  "en_revalidacion":"En cola para revalidación",
  "sin_motivo":"Sin motivo registrado"
 };
 for(const f of S.admin.folders){
  const card=document.createElement("article");card.className="folder-card admin-gallery-card";
  const trigger=document.createElement("button");trigger.type="button";
  trigger.className="admin-gallery-trigger";trigger.setAttribute("aria-label",
      "Mostrar datos de "+(f.public_name||f.name));
  const cover=document.createElement("div");cover.className="folder-cover";
  if(f.cover){
   const img=document.createElement("img");img.src=f.cover;img.alt="";
   img.loading="lazy";img.decoding="async";
   img.onerror=()=>{img.remove();cover.textContent="▧";};
   cover.append(img);
  }else{
   const icon=document.createElement("span");icon.className="folder-icon";
   icon.textContent="▧";cover.append(icon);
  }
  const title=document.createElement("h3");
  title.textContent=f.public_name||("Galería "+f.id);
  const count=document.createElement("p");
  // En la tarjeta, mostrar exactamente lo que ve el usuario.
  // El inventario histórico completo permanece dentro de los detalles.
  count.textContent=pretty(f.visible_photos)+" fotos · "+pretty(f.visible_videos)+" videos";
  // El origen declara mucho más contenido que el catálogo actual.
  // Nunca presentar un dato aún no consultado como cero.
  const hasTelegramTotals=f.telegram_photos_total!==null&&f.telegram_photos_total!==undefined
    &&f.telegram_videos_total!==null&&f.telegram_videos_total!==undefined;
  const originCount=document.createElement("p");originCount.className="admin-origin-count";
  originCount.textContent=hasTelegramTotals?
    "En Telegram: "+pretty(f.telegram_photos_total)+" fotos · "+pretty(f.telegram_videos_total)+" videos":
    "En Telegram: consultando totales…";
  const chevron=document.createElement("span");chevron.className="admin-gallery-chevron";
  chevron.textContent="Detalles ▾";
  trigger.append(cover,title,count,originCount,chevron);
  const details=document.createElement("div");details.className="admin-gallery-detail hidden";
  details.id="admin-folder-details-"+f.id;
  trigger.setAttribute("aria-controls",details.id);
  const info=document.createElement("div");info.className="admin-folder-data";
  const state=!f.active?"Pausada":S.admin.sync_enabled?"Respaldo activo":"Esperando sincronizador";
  function addAdminDatum(label,value,description=""){
   const row=document.createElement("div");row.className="admin-data-row";
   const name=document.createElement("span");name.className="admin-data-label";
   name.textContent=label;
   const result=document.createElement("strong");result.className="admin-data-value";
   result.textContent=String(value);
   row.append(name,result);
   if(description){
    const help=document.createElement("small");help.className="admin-data-help";
    help.textContent=description;row.append(help);
   }
   info.append(row);
  }
  addAdminDatum("Nombre público",f.public_name||("Galería "+f.id));
  addAdminDatum("Nombre interno",f.name||"Sin nombre");
  addAdminDatum("Origen de Telegram",f.peer,"Identificador del grupo o canal de origen.");
  addAdminDatum("Cuenta Telegram para el historial",
    f.preferred_uid?gallerySourceLabel(f.preferred_uid):"No asignada",
    "Solo esta cuenta vinculada se utilizará cuando la establezcas. Debe poder ver todo el historial.");
  if(!f.preferred_uid&&f.source_uid)
    addAdminDatum("Cuenta detectada hasta ahora",gallerySourceLabel(f.source_uid),
      "Cuenta elegida automáticamente; puedes fijar otra con acceso al historial completo.");
  addAdminDatum("Fotos totales en Telegram",
    hasTelegramTotals?pretty(f.telegram_photos_total):"Pendiente de consulta",
    "Cantidad reportada por Telegram, sin descargar archivos ni esperar la catalogación.");
  addAdminDatum("Videos totales en Telegram",
    hasTelegramTotals?pretty(f.telegram_videos_total):"Pendiente de consulta",
    "Cantidad reportada por Telegram, independiente de los videos ya respaldados.");
  addAdminDatum("Fotos + videos en Telegram",
    hasTelegramTotals?pretty(Number(f.telegram_photos_total)+Number(f.telegram_videos_total)):"Pendiente de consulta",
    "Total de los filtros de fotos y videos. Los GIF y archivos enviados como documentos pueden contabilizarse aparte.");
  addAdminDatum("Totales consultados",
    f.telegram_counts_at?new Date(Number(f.telegram_counts_at)*1000).toLocaleString("es-US"):"Todavía sin consultar",
    "Se actualizan periódicamente usando una cuenta que ya tiene acceso al grupo.");
  if(f.telegram_counts_error)
    addAdminDatum("Último error al consultar Telegram",f.telegram_counts_error,
      "Se conserva el último total conocido; la consulta se reintentará.");
  addAdminDatum("Fotos normales",pretty(f.photo_native),"Incluye registros antiguos aún sin clasificar.");
  addAdminDatum("Videos normales",pretty(f.video_native),"Incluye registros antiguos aún sin clasificar.");
  addAdminDatum("GIF",pretty(f.gifs),"Se incluyen en la galería de videos.");
  addAdminDatum("Fotos enviadas como archivo",pretty(f.photo_files));
  addAdminDatum("Videos enviados como archivo",pretty(f.video_files));
  addAdminDatum("Multimedia registrado",pretty(f.media_total),
    "Inventario único identificado hasta ahora. Puede aumentar si la catalogación continúa.");
  if(f.legacy_uncategorized){
   addAdminDatum("Registros antiguos sin distinguir formato",pretty(f.legacy_uncategorized),
      "Ya están incluidos arriba; se clasificarán mejor cuando se revaliden.");
  }
  addAdminDatum("Estado del respaldo",state,
     "Estado general del sincronizador; no necesariamente una transferencia activa de este grupo.");
  addAdminDatum("Archivos visibles solo con vista previa",pretty(f.preview_only),
    "Aparecen en la galería antes de que se guarde su original en Cloudflare.");
  addAdminDatum("Vistas previas con miniatura",pretty(f.previews_with_images),
    "Archivos con imagen de muestra, pero todavía sin respaldo completo.");
  addAdminDatum("Archivos respaldados",pretty(f.ready),"Originales guardados en Cloudflare R2.");
  addAdminDatum("Archivos pendientes",pretty(f.pending),
     "Falta respaldar el original; algunos ya tienen miniatura.");
  if(f.rechecking)addAdminDatum("Revalidación solicitada",pretty(f.rechecking),
     "Forman parte de los pendientes; se comprobarán sin bloquear las descargas.");
  const legacyMissing=Math.max(0,Number(f.missing||0)-Number(f.missing_rechecked||0));
  addAdminDatum("Incidencias sin verificar",pretty(legacyMissing),
    "No significa que estén borrados; incluye errores de clasificación anteriores.");
  addAdminDatum("No recuperados tras tres comprobaciones",pretty(f.missing_rechecked),
    "Pueden estar borrados, haber cambiado o no ser accesibles; no se borró nada de R2.");
  addAdminDatum("Restringidos o temporales",pretty(f.skipped),
    "Separados de los archivos que realmente faltan.");
  addAdminDatum("Mensajes recorridos",pretty(f.scan_count),
    "Contador de entradas examinadas por Telegram; se indexan únicamente los multimedia y sus captions.");
  addAdminDatum("Historial inicial",f.complete?"Recorrido completado":"Recorrido en curso",
    "Solo indica que se alcanzó el final del historial inicial; el respaldo sigue aparte.");
  if(Array.isArray(f.missing_reasons)&&f.missing_reasons.length){
   for(const reason of f.missing_reasons){
    const label=readableReason[reason.code]||reason.code||"Motivo desconocido";
    addAdminDatum("Motivo: "+label,pretty(reason.count));
   }
  }
  if(f.error)addAdminDatum("Última incidencia técnica",f.error);
  const buttons=document.createElement("div");buttons.className="row-actions";
  const edit=document.createElement("button");edit.type="button";
  edit.className="small-btn";edit.textContent="Nombre interno";
  edit.onclick=async()=>{const value=prompt("Nombre que solo ve el administrador",f.name);
   if(!value||value===f.name)return;
   try{await api("admin-folder-update",{folder:f.id,field:"name",name:value});
       await loadAdmin();}catch(e){notice(e.message);}};
  const publicEdit=document.createElement("button");publicEdit.type="button";
  publicEdit.className="small-btn";publicEdit.textContent="Nombre público";
  publicEdit.onclick=async()=>{const value=prompt("Nombre visible para los clientes",
      f.public_name||("Galería "+f.id));
   if(!value||value===f.public_name)return;
   try{await api("admin-folder-update",{folder:f.id,field:"public_name",name:value});
       await loadAdmin();await refresh();
   }catch(e){notice(e.message);}};
  const remove=document.createElement("button");remove.type="button";remove.className="small-btn";
  remove.textContent=f.active?"Ocultar":"Restaurar";
  remove.onclick=async()=>{
   try{
    if(f.active)await api("admin-folder-remove",{folder:f.id});
    else await api("admin-folder",{peer:f.peer,name:f.name});
    await loadAdmin();
   }catch(e){notice(e.message);}
  };
  const reset=document.createElement("button");reset.type="button";
  reset.className="small-btn";reset.textContent="Revisar origen";
  reset.onclick=async()=>{try{await api("admin-sync-reset",{folder:f.id});
    notice("Revisión solicitada");await loadAdmin();}catch(e){notice(e.message);}};
  const pick=document.createElement("input");pick.type="file";
  pick.accept="image/jpeg,image/png,image/webp";
  pick.className="hidden";pick.setAttribute("aria-label","Elegir nueva portada de galería");
  const choose=document.createElement("button");choose.type="button";choose.className="small-btn";
  choose.textContent=f.has_custom_cover?"Cambiar foto":"Agregar foto";
  choose.onclick=()=>pick.click();
  pick.onchange=async()=>{
   const file=pick.files?.[0];if(!file)return;
   choose.disabled=true;
   const oldLabel=choose.textContent;choose.textContent="Subiendo…";
   try{if(await uploadAsset("folder-cover",file,f.id)){await loadAdmin();await refresh(true);}}
   finally{choose.disabled=false;choose.textContent=oldLabel;}
  };
  const sourceButton=document.createElement("button");sourceButton.type="button";
  sourceButton.className="small-btn";sourceButton.textContent="Cuenta del historial";
  sourceButton.onclick=async()=>{
    const current=gallerySourceLabel(f.preferred_uid||f.source_uid);
    const value=prompt("Cuenta del historial. Escribe DESCARGAS:telegram_id o DATOS:telegram_id:",current==="No asignada"?"":current);
    if(value===null)return;
    const parsed=gallerySourceInput(value);
    if(!parsed)return notice("Indica un Telegram ID válido. Para DATOS escribe DATOS:123456789.");
    if(gallerySourceLabel(f.preferred_uid)===gallerySourceLabel(
        parsed.account_system==="datos"?-Number(parsed.account_uid):Number(parsed.account_uid)))
      return notice("Esa cuenta ya está asignada.");
    if(!confirm("El historial se volverá a revisar desde esta cuenta. Los archivos guardados en Cloudflare permanecerán intactos. ¿Continuar?"))return;
    sourceButton.disabled=true;
    try{
      await api("admin-folder-source",{folder:f.id,...parsed});
      await loadAdmin();
      notice("Cuenta asignada. El sistema buscará el historial que puede ver esa cuenta.");
    }catch(e){notice(e.message);}
    finally{sourceButton.disabled=false;}
  };
  const browse=document.createElement("button");browse.type="button";browse.className="small-btn strong";
  browse.textContent="Abrir galería y buscar";
  browse.onclick=()=>{S.returnFromAdmin=true;openFolder({
    id:f.id,name:f.public_name||("Galería "+f.id),photos:f.visible_photos,videos:f.visible_videos
  });};
  buttons.append(browse,edit,publicEdit,sourceButton,choose);
  if(f.has_custom_cover){
   const resetPhoto=document.createElement("button");resetPhoto.type="button";
   resetPhoto.className="small-btn";resetPhoto.textContent="Quitar foto";
   resetPhoto.onclick=async()=>{
    if(!confirm("¿Quitar la portada personalizada? Se mostrará la miniatura automática."))return;
    try{await api("admin-folder-cover-remove",{folder:f.id});await loadAdmin();await refresh(true);
        notice("Portada quitada. Se mostrará la miniatura automática.");}
    catch(e){notice(e.message);}
   };
   buttons.append(resetPhoto);
  }
  buttons.append(remove,reset);
  const audit=document.createElement("button");audit.type="button";audit.className="small-btn";
  audit.textContent="Revalidar 10 incidencias";
  audit.disabled=!f.missing;
  audit.onclick=async()=>{
   audit.disabled=true;
   try{
    const response=await api("admin-recheck-missing",{folder:f.id});
    notice(response.scheduled?response.scheduled+" archivos enviados a revisión gradual.":
         "No quedan incidencias pendientes de revalidación.");
    await loadAdmin();
   }catch(e){notice(e.message);}
   finally{audit.disabled=false;}
  };
  buttons.append(audit);
  const examples=document.createElement("div");
  examples.className="admin-missing-examples hidden";
  const samples=document.createElement("button");samples.type="button";
  samples.className="small-btn";samples.textContent="Ver 10 ejemplos";
  samples.disabled=!f.missing;
  samples.onclick=async()=>{
   samples.disabled=true;examples.classList.remove("hidden");
   examples.textContent="Consultando incidencias…";
   try{
    const data=await api("admin-missing-samples",{folder:f.id});
    examples.replaceChildren();
    if(!data.items.length){examples.textContent="Sin incidencias registradas.";return;}
    const heading=document.createElement("h4");heading.textContent="Ejemplos de archivos por verificar";
    examples.append(heading);
    for(const item of data.items){
     const row=document.createElement("p");
     const kind=item.source_type||item.kind||"tipo desconocido";
     const code=item.error||"sin motivo";
     row.textContent="Mensaje "+item.message+" · "+kind+" · "+item.mime+
       " · "+(readableReason[code]||code);
     examples.append(row);
    }
   }catch(e){examples.textContent=e.message;}
   finally{samples.disabled=false;}
  };
  buttons.append(samples);
  const note=document.createElement("p");
  note.className="admin-reindex-hint";
  note.textContent="Para volver a recorrer el historial y detectar archivos antiguos"+
    " que antes no se reconocían, utiliza Revisar origen. No borra los originales respaldados.";
  details.append(info,pick,buttons,note,examples);
  function setExpanded(open){
    card.classList.toggle("expanded",open);
    details.classList.toggle("hidden",!open);
    trigger.setAttribute("aria-expanded",String(open));
    chevron.textContent=open?"Cerrar detalles ▴":"Detalles ▾";
  }
  trigger.onclick=()=>{
   const open=!S.adminExpanded.has(f.id);
   if(open)S.adminExpanded.add(f.id);else S.adminExpanded.delete(f.id);
   setExpanded(open);
  };
  setExpanded(S.adminExpanded.has(f.id));
  card.append(trigger,details);root.append(card);
 }
 const users=$("admin-users");
 // Personal records are now loaded on demand in bounded pages by admin_tools.js.
 if(!window.CubanitasTools?.usersLoaded?.())users.replaceChildren();
 const h=$("admin-health");
 h.innerHTML="<h2>Estado del respaldo</h2><p>"+(S.admin.storage_ready?
  "✓ Almacenamiento R2 conectado":"⚠ Almacenamiento privado pendiente")+
  "</p><p>"+(S.admin.sync_enabled?
   "✓ Copia masiva automática: "+esc(S.admin.sync_phase||"en marcha"):
   "⚠ Sincronizador detenido o pendiente de conexión")+
  "</p><p>Agregar carpeta → comenzar; ocultar carpeta → pausar; restaurar → continuar. No se borran archivos de R2.</p>";
}
async function loadAdmin(){
 try{
  S.admin=await api("admin-overview");
  previewAdmin();
  window.CubanitasTools?.adminRefreshed?.(S.admin);
  await window.CubanitasTools?.refreshUserStats?.();
 }catch(e){notice(e.message);}
}
function switchAdminTab(tab){
 document.querySelectorAll(".admin-tabs button").forEach(b=>b.classList.toggle("active",b.dataset.tab===tab));
 document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("hidden",x.id!=="tab-"+tab));
 if(tab==="usuarios")window.CubanitasTools?.showUserSectionMenu?.();
 if(tab==="comercio")window.CubanitasTools?.loadCommercial?.();
}
function populateEditor(){
 const d=S.me.design;
 for(const k of ["title","intro_label","intro_caption","button_label","header_mode","button_mode","tile_shape",
                 "library_brand","library_brand_mode","library_eyebrow","library_eyebrow_mode",
                 "library_heading","library_heading_mode",
                 "intro_fit","intro_media","button_color","accent","background","text_color",
                 "title_image_height","button_image_height",
                 "library_brand_image_height","library_eyebrow_image_height",
                 "library_heading_image_height"])
  $("edit-"+k).value=d[k]??(k==="button_mode"?"text":"");
 updateEditorLabelChoices();
 loadEditorImagePreview("title");
 loadEditorImagePreview("button");
 for(const slot of LIBRARY_SLOTS)loadLibraryEditorPreview(slot);
 populateWatermarkEditor(d);
}
async function saveDesign(){
 const design={};
 for(const k of ["title","intro_label","intro_caption","button_label","header_mode","button_mode","tile_shape",
                 "library_brand","library_brand_mode","library_eyebrow","library_eyebrow_mode",
                 "library_heading","library_heading_mode",
                 "intro_fit","intro_media","button_color","accent","background","text_color"])
  design[k]=$("edit-"+k).value;
 for(const k of ["title_image_height","button_image_height",
                 "library_brand_image_height","library_eyebrow_image_height",
                 "library_heading_image_height"])
  design[k]=Number($("edit-"+k).value);
 design.daily_limit=50;
 try{await api("admin-design",{design});notice("Diseño actualizado");await refresh();}catch(e){notice(e.message);}
}
async function loadPermissions(){
 const uid=$("permission-uid").value.trim();
 if(!/^[0-9]{5,18}$/.test(uid))return notice("Introduce un Telegram ID válido.");
 try{
  const data=await api("admin-user-access",{user:Number(uid)});
  $("permission-mode").value=data.mode;
  const div=$("permission-folders");div.replaceChildren();
  for(const f of S.admin.folders.filter(x=>x.active)){
   const label=document.createElement("label"),check=document.createElement("input");
   check.type="checkbox";check.value=f.id;check.checked=data.folders.includes(f.id);
   label.append(check,document.createTextNode(f.name));div.append(label);
  }
 }catch(e){notice(e.message);}
}
async function savePermissions(){
 const uid=$("permission-uid").value.trim(),mode=$("permission-mode").value;
 if(!/^[0-9]{5,18}$/.test(uid))return notice("Telegram ID incorrecto.");
 const folders=[...$("permission-folders").querySelectorAll("input:checked")].map(x=>Number(x.value));
 try{await api("admin-access",{user:Number(uid),mode,folders});notice("Permisos actualizados.");}
 catch(e){notice(e.message);}
}
async function uploadAsset(key,file,folder=null){
 if(!file)return false;
 const allowed=key==="intro"?25_000_000:
   (key==="header"||key==="folder-cover"||key==="intro-photo"||key.startsWith("library-"))?5_000_000:3_000_000;
 const progress=key==="folder-cover"?$("new-cover-status"):$("upload-progress");
 if(!file.size||file.size>allowed){
  progress.textContent="Archivo demasiado grande. Límite: "+(allowed/1_000_000)+" MB.";
  notice("El archivo es demasiado grande.");return false;
 }
 const mime=file.type;
 if(key==="folder-cover"&&!["image/jpeg","image/png","image/webp"].includes(mime)){
  progress.textContent="Elige una imagen JPEG, PNG o WebP.";
  notice("Formato de foto no admitido.");return false;
 }
 const upload=Array.from(crypto.getRandomValues(new Uint8Array(16)),
     b=>b.toString(16).padStart(2,"0")).join("");
 const size=192*1024,total=Math.ceil(file.size/size);
 if(total>130)return notice("El archivo excede el máximo de bloques."),false;
 try{
  for(let i=0;i<total;i++){
   const block=new Uint8Array(await file.slice(i*size,(i+1)*size).arrayBuffer());
   let chunk="";for(let j=0;j<block.length;j+=8192)
     chunk+=String.fromCharCode(...block.subarray(j,j+8192));
   await api("admin-upload",{upload,key,mime,part:i,total,data:btoa(chunk),
     ...(key==="folder-cover"?{folder}:{})});
   progress.textContent="Subiendo "+(i+1)+" de "+total+" bloques…";
  }
  progress.textContent=key==="folder-cover"?"Foto de presentación guardada.":"Archivo guardado correctamente";
  notice(key==="folder-cover"?"Portada actualizada.":"Imagen o video actualizado");
  if(key!=="folder-cover")await refresh();
  return true;
 }catch(e){
  progress.textContent=e.message;notice(e.message);
  return false;
 }
}
function introMime(file){
 const ext=String(file?.name||"").split(".").pop().toLowerCase();
 if(ext==="mov")return "video/quicktime";
 if(ext==="mp4"||ext==="m4v")return "video/mp4";
 if(ext==="webm")return "video/webm";
 return file?.type||"";
}
async function uploadIntroVideo(file,kind="intro"){
 if(!file)return false;
 const mime=introMime(file),limit=80*1024*1024;
 if(!["video/mp4","video/quicktime","video/webm"].includes(mime))
  throw Error("Selecciona un video MP4, MOV o WebM.");
 if(!file.size||file.size>limit)
  throw Error("El video supera los 80 MB; utiliza un video más corto o comprimido.");
 const progress=$("upload-progress"),raw=tg?.initData||"";
 return await new Promise((resolve,reject)=>{
  const xhr=new XMLHttpRequest();
  xhr.open("POST","/web-galerias/admin/intro-video",true);
  xhr.withCredentials=true;
  xhr.setRequestHeader("Content-Type",mime);
  xhr.setRequestHeader("X-Intro-Mime",mime);
  xhr.setRequestHeader("X-Intro-Kind",kind);
  if(raw)xhr.setRequestHeader("X-Telegram-Init-Data",raw);
  else xhr.setRequestHeader("X-Gallery-Web","1");
  xhr.upload.onprogress=e=>{
   if(e.lengthComputable){
    const percent=Math.min(100,Math.floor(e.loaded/e.total*100));
    progress.textContent="Subiendo video: "+percent+" %";
    if(percent===100)progress.textContent="Video subido. Convirtiendo a MP4 y verificando Cloudflare…";
   }
  };
  xhr.onerror=()=>reject(Error("No se pudo conectar durante la subida del video."));
  xhr.onabort=()=>reject(Error("Se canceló la subida del video."));
  xhr.onload=()=>{
   if(xhr.status<200||xhr.status>=300){
    reject(Error((xhr.responseText||"Error de subida").replace(/<[^>]*>/g," ").slice(0,220)));
    return;
   }
   try{
    const data=JSON.parse(xhr.responseText);
    if(!data.complete)throw Error("No se confirmó la subida del video.");
    progress.textContent=kind==="web-promo"?
     "✓ Video promo Web publicado. Reemplaza la foto solo en Nuevas ofertas.":
     "✓ Video MP4 guardado y verificado en Cloudflare.";
    resolve(true);
   }catch(e){reject(e);}
  };
  xhr.send(file);
 });
}
function pendingEditorFiles(){
 return ["logo","header","button-label-image",
          "library-brand","library-eyebrow","library-heading",
          "intro-photo","intro","web-promo"]
  .map(key=>({key,file:$("upload-"+key).files?.[0]}))
  .filter(entry=>!!entry.file);
}
function editorPendingFilesMessage(){
 const files=pendingEditorFiles();
 $("upload-progress").textContent=files.length?
  "Pendiente de confirmar: "+files.map(x=>x.file.name).join(" · ")+
  ". Pulsa Confirmar cambios para publicar.":
  "Sin archivos nuevos seleccionados. También puedes confirmar textos, colores y marca de agua.";
}
async function confirmEditorChanges(){
 if(S.editorSaving)return;
 const button=$("confirm-editor"),status=$("upload-progress");
 S.editorSaving=true;button.disabled=true;button.textContent="Guardando cambios…";
 try{
  const files=pendingEditorFiles();
  for(const {key,file} of files){
   status.textContent="Preparando "+file.name+"…";
   if(key==="intro"||key==="web-promo"){
    await uploadIntroVideo(file,key);
   }else{
    if(!await uploadAsset(key,file))throw Error("No se pudo guardar "+file.name+".");
   }
  }
  const design={};
  for(const key of ["title","intro_label","intro_caption","button_label",
                    "header_mode","button_mode","tile_shape","intro_fit","intro_media","button_color",
                    "library_brand","library_brand_mode","library_eyebrow","library_eyebrow_mode",
                    "library_heading","library_heading_mode",
                    "accent","background","text_color"])
   design[key]=$("edit-"+key).value;
  for(const key of ["title_image_height","button_image_height",
                 "library_brand_image_height","library_eyebrow_image_height",
                 "library_heading_image_height"])
   design[key]=Number($("edit-"+key).value);
  design.daily_limit=50;
  status.textContent="Guardando identidad y marca de agua…";
  await api("admin-design",{design});
  await api("admin-watermark",{watermark:watermarkFromEditor()});
  for(const key of ["logo","header","button-label-image",
                     "library-brand","library-eyebrow","library-heading",
                     "intro","intro-photo","web-promo"])$("upload-"+key).value="";
  S.heroNeedsReload=files.some(({key})=>key==="intro"||key==="intro-photo");
  if(files.some(({key})=>key==="header"||key==="button-label-image"||key.startsWith("library-")))
   S.identityRefreshVersion=Date.now();
  await refresh();
  populateEditor();
  status.textContent="✓ Cambios confirmados. La portada ya está publicada.";
  notice("Cambios guardados y publicados correctamente.");
 }catch(e){
  status.textContent="No se pudieron confirmar todos los cambios: "+e.message+
   " Los archivos confirmados anteriormente permanecen guardados.";
  notice("Revisa el error antes de volver a confirmar.");
 }finally{
  S.editorSaving=false;button.disabled=false;button.textContent="✓ Confirmar cambios";
 }
}
let searchTimer=null;
$("enter").onclick=async()=>{
 if(!S.me)return notice("La Mini App aún se está configurando.");
 if(S.me.allowed){show("library");return;}
 if(!S.me.checkout_enabled)return notice("Acceso reservado a los usuarios de prueba.");
 if(!S.me.checkout_ready)return notice("Los pagos de Web Galerías todavía no están configurados.");
 const enter=$("enter");enter.disabled=true;
 try{const data=await api("checkout");if(data.allowed){await refresh();show("library");}
  else if(data.url){tg?.openLink?tg.openLink(data.url):location.assign(data.url);}}
 catch(e){notice(e.message);}
 finally{enter.disabled=false;}
};
$("go-intro").onclick=()=>show("intro");
$("open-support-chat").onclick=()=>openSupportChat();
// Swiftgram can ignore a t.me?startapp link inside an existing Mini App,
// even after Customer Service returns HTTP 200. Use the signed in-webview
// navigation already established by Mi Cuenta -> Web Galerías, in reverse.
let customerOffersLink=null,customerOffersWebUrl=null,customerOffersRequest=null;
// Only small navigation coordinates are stored, never signed Telegram data,
// media URLs, access keys, or contents. The browser keeps the full gallery
// document in history when supported; this recovers it if Swiftgram reloads.
const CROSS_APP_RETURN_KEY="wg_cross_app_return_v1";
function saveGalleryReturnPosition(){
 if(!S.me?.allowed||typeof document==="undefined")return;
 const inViewer=!$("viewer").classList.contains("hidden");
 const inAlbum=!$("album").classList.contains("hidden");
 const screen=inViewer?"viewer":inAlbum?"album":
     !$("library").classList.contains("hidden")?"library":"intro";
 let media=inViewer?Number(S.items[S.viewer]?.id||0):0;
 if(inAlbum&&!inViewer){
  const tiles=document.querySelectorAll("#album-grid [data-media-id]");
  for(const tile of tiles){
   const rect=tile.getBoundingClientRect();
   if(rect.bottom>170&&rect.top<innerHeight-90){
    media=Number(tile.dataset.mediaId||0);
    break;
   }
  }
 }
 const folder=S.folder;
 try{
  sessionStorage.setItem(CROSS_APP_RETURN_KEY,JSON.stringify({
   v:1,at:Date.now(),screen,folderId:Number(folder?.id||0),
   favorites:!!folder?.favorites,personal:!!folder?.personal,
   media:Number.isSafeInteger(media)&&media>0?media:0,
   scrollY:Math.max(0,Math.floor(window.scrollY||0))
  }));
 }catch(_){}
}
async function restoreGalleryReturnPosition(){
 let data=null;
 try{
  const raw=sessionStorage.getItem(CROSS_APP_RETURN_KEY);
  sessionStorage.removeItem(CROSS_APP_RETURN_KEY);
  if(raw)data=JSON.parse(raw);
 }catch(_){}
 if(!data||data.v!==1||!Number.isFinite(data.at)||
    Date.now()-data.at<0||Date.now()-data.at>30*60*1000||!S.me?.allowed)
  return false;
 try{
  if(data.screen==="viewer"&&Number.isSafeInteger(data.media)&&data.media>0){
   await resumeMedia(data.media);return true;
  }
  if(data.screen==="album"){
   let folder=null;
   if(data.favorites)folder={favorites:true,name:"Mis favoritos"};
   else if(data.personal){
    const personal=S.personal.find(f=>Number(f.id)===data.folderId);
    if(personal)folder={...personal,personal:true};
   }else folder=S.folders.find(f=>Number(f.id)===data.folderId);
   if(folder){
    await openFolder(folder);
    const media=Number(data.media),offset=Number(data.scrollY);
    requestAnimationFrame(()=>requestAnimationFrame(()=>{
     const tile=Number.isSafeInteger(media)&&media>0?
       $("album-grid").querySelector('[data-media-id="'+media+'"]'):null;
     if(tile)tile.scrollIntoView({block:"center",inline:"nearest",behavior:"instant"});
     else window.scrollTo(0,Number.isFinite(offset)?Math.max(0,offset):0);
    }));
    return true;
   }
  }
  if(data.screen==="library"){
   show("library");
   requestAnimationFrame(()=>requestAnimationFrame(()=>
    window.scrollTo(0,Number.isFinite(data.scrollY)?Math.max(0,data.scrollY):0)));
   return true;
  }
 }catch(_){/* Gallery remains available even when a previous media is gone. */}
 return false;
}
if(window.addEventListener)window.addEventListener("pageshow",event=>{
 if(event.persisted){try{sessionStorage.removeItem(CROSS_APP_RETURN_KEY);}catch(_){}}
});
function navigateCustomerScreen(telegramUrl,webUrl,destination){
 if(webUrl&&tg?.initData&&!BROWSER_ADMIN){
  let target;
  try{target=new URL(webUrl);}catch(_){target=null;}
  if(!target||target.protocol!=="https:"||target.pathname!=="/cubanas"||
     target.username||target.password||target.search||target.hash)
   throw Error("Destino de Atención al Cliente inválido.");
  // Only the navigation hint enters the query. Signed Telegram initData
  // stays in the URL fragment and is reverified by CUSTOMER-SERVICE.
  target.searchParams.set("tgWebAppStartParam",destination);
  target.hash=new URLSearchParams({
   tgWebAppData:tg.initData,
   tgWebAppVersion:String(tg.version||"8.0"),
   tgWebAppPlatform:String(tg.platform||"unknown")
  }).toString();
  saveGalleryReturnPosition();
  window.location.assign(target.href);
  return;
 }
 // For an entry bot other than CUSTOMER-SERVICE, never swallow a tap.
 // A normal navigation is more reliable in nonofficial Telegram clients.
 saveGalleryReturnPosition();
 window.location.assign(telegramUrl);
}
async function getCustomerOffersLink(){
 if(customerOffersLink)return customerOffersLink;
 if(customerOffersRequest)return customerOffersRequest;
 customerOffersRequest=api("offers-link").then(data=>{
  const url=String(data.url||"");
  if(!/^https:\/\/t\.me\/[A-Za-z][A-Za-z0-9_]{4,31}(?:\/[A-Za-z0-9_]{1,64})?\?startapp=offers$/.test(url))
   throw Error("La Mini App de Mi Cuenta aún no está disponible.");
  customerOffersWebUrl=String(data.web_url||"");
  customerOffersLink=url;return url;
 }).finally(()=>{customerOffersRequest=null;});
 return customerOffersRequest;
}
async function openMoreContent(){
 if(ADMIN_ACCOUNT_VIEW)
  return notice("Las ofertas privadas se abren desde la sesión Telegram del propio usuario.");
 const button=$("more-content");
 if(button.disabled)return;
 button.disabled=true;
 try{
  const url=await getCustomerOffersLink();
  navigateCustomerScreen(url,customerOffersWebUrl,"offers");
 }catch(e){
  notice("No se pudo abrir Nuevas Ofertas. Inténtalo nuevamente.");
 }finally{button.disabled=false;}
}
$("more-content").onclick=openMoreContent;
// Customer support opens a separate, signed Telegram Mini App chat screen.
let customerChatLink=null,customerChatWebUrl=null,customerChatRequest=null;
async function getCustomerChatLink(){
 if(customerChatLink)return customerChatLink;
 if(customerChatRequest)return customerChatRequest;
 customerChatRequest=api("chat-link").then(data=>{
  const url=String(data.url||"");
  if(!/^https:\/\/t\.me\/[A-Za-z][A-Za-z0-9_]{4,31}(?:\/[A-Za-z0-9_]{1,64})?\?startapp=chat$/.test(url))
   throw Error("El chat de Atención al Cliente no está disponible.");
  customerChatWebUrl=String(data.web_url||"");
  customerChatLink=url;return url;
 }).finally(()=>{customerChatRequest=null;});
 return customerChatRequest;
}
async function openSupportChat(){
 if(ADMIN_ACCOUNT_VIEW)
  return notice("El chat privado requiere la sesión del propio usuario de Telegram.");
 const button=$("open-support-chat");
 if(button?.disabled)return;
 if(button)button.disabled=true;
 try{
  const url=await getCustomerChatLink();
  navigateCustomerScreen(url,customerChatWebUrl,"chat");
 }catch(e){notice("No se pudo abrir el chat de Atención al Cliente.");}
 finally{if(button)button.disabled=false;}
}
function updateSupportVisibility(screen){
 // Administrator: Portada to inspect the landing page. Members: fixed blue support chat.
 // Keep both hidden until signed bootstrap establishes the real account role.
 const admin=!!S.me?.admin;
 const chat=$("open-support-chat");
 const portada=$("go-intro");
 if(chat)chat.classList.toggle("hidden",!S.me?.allowed||admin);
 if(portada)portada.classList.toggle("hidden",!admin);
}

if($("quota-connect"))$("quota-connect").onclick=()=>{
 if(ADMIN_ACCOUNT_VIEW)
  return notice("La vinculación a DESCARGAS debe completarla este usuario con su propia cuenta de Telegram.");
 // Same bot: open the original DESCARGAS account Mini App in this WebView.
 // Signed Telegram identity stays in the URL fragment, never the HTTP query.
 const miniapp=String(S.me?.quota_link_miniapp||"");
 if(miniapp&&tg?.initData&&!BROWSER_ADMIN){
  let target;
  try{target=new URL(miniapp);}catch(_){target=null;}
  if(target&&target.protocol==="https:"&&!target.username&&!target.password&&
     !target.search&&!target.hash&&["/","/descargas","/descargas/"].includes(target.pathname)){
   target.hash=new URLSearchParams({
    tgWebAppData:tg.initData,
    tgWebAppVersion:String(tg.version||"8.0"),
    tgWebAppPlatform:String(tg.platform||"unknown")
   }).toString();
   saveGalleryReturnPosition();
   window.location.assign(target.href);
   return;
  }
 }
 // When the entry bot differs, use the existing safe bot-chat fallback.
 const url=S.me?.quota_link_bot;
 if(!url)return notice("Abre el bot DESCARGAS, vincula tu cuenta de Telegram y vuelve aquí.");
 if(!/^https:\/\/t\.me\/[A-Za-z][A-Za-z0-9_]{4,31}$/.test(url))
  return notice("Bot DESCARGAS no disponible.");
 if(tg?.openTelegramLink)tg.openTelegramLink(url);
 else window.location.assign(url);
};
if($("quota-plus"))$("quota-plus").onclick=async()=>{
 const button=$("quota-plus");
 if(button.disabled)return;
 button.disabled=true;
 try{
  const data=await api("quota-plus-checkout");
  if(data.allowed){await refresh(true);notice("Ya tienes descargas ilimitadas.");}
  else if(data.url){if(tg?.openLink)tg.openLink(data.url);else window.location.assign(data.url);}
 }catch(e){notice(e.message);}
 finally{button.disabled=false;}
};
$("open-favorites").onclick=()=>openFolder({favorites:true,name:"Mis favoritos"});
$("viewer-favorite").onclick=toggleViewerFavorite;
$("personal-folders-toggle").onclick=()=>setPersonalMenuOpen(!S.personalMenuOpen);
$("create-personal-folder").onclick=()=>createPersonalFolder();
$("personal-cover-file").onchange=()=>{
 const file=$("personal-cover-file").files?.[0];
 if(file)uploadPersonalCover(file);
};
$("folder-drop-toggle").onclick=()=>setDropMenuOpen(!S.dropMenuOpen);
$("folder-drop-toggle").ondragenter=e=>{
 e.preventDefault();setDropMenuOpen(true);$("folder-drop-toggle").classList.add("drag-menu-armed");
};
$("folder-drop-toggle").ondragover=e=>{
 e.preventDefault();setDropMenuOpen(true);e.dataTransfer.dropEffect="copy";
};
$("folder-drop-toggle").ondragleave=()=>$("folder-drop-toggle").classList.remove("drag-menu-armed");
$("folder-drop-menu").ondragover=e=>{
 e.preventDefault();
 const menu=$("folder-drop-menu"),rect=menu.getBoundingClientRect();
 if(e.clientY<rect.top+48)menu.scrollTop-=15;
 else if(e.clientY>rect.bottom-48)menu.scrollTop+=15;
};
$("album-create-folder").onclick=()=>createPersonalFolder([...S.selected]);
$("album-create-folder").ondragover=e=>{e.preventDefault();$("album-create-folder").classList.add("drag-target");};
$("album-create-folder").ondragleave=()=>$("album-create-folder").classList.remove("drag-target");
$("album-create-folder").ondrop=e=>{
 e.preventDefault();$("album-create-folder").classList.remove("drag-target");
 const id=Number(e.dataTransfer.getData("text/plain"));
 const ids=selectedDragIds(id);
 if(ids.length)createPersonalFolder(ids);
};
$("selection-organize").onclick=openFolderPicker;
$("folder-picker-cancel").onclick=()=>$("folder-picker").classList.add("hidden");
$("save-cancel").onclick=closeSaveSheet;
$("save-native").onclick=()=>{if(S.saveData)nativeDownload(S.saveData);closeSaveSheet();};
$("save-share").onclick=()=>{
 if(!S.prepared?.length)return;
 const files=S.prepared;
 // Compartir directamente en respuesta al toque para conservar la activación iOS.
 navigator.share({files}).then(()=>{
  notice("Menú de Fotos cerrado. Comprueba que hayas elegido guardar.");
  closeSaveSheet();
 }).catch(e=>{
  if(e?.name!=="AbortError")notice("Si Fotos no está disponible, usa Guardar en Archivos.");
 });
};
$("filter-toggle").onclick=()=>{
 const menu=$("filter-menu");const open=menu.classList.toggle("hidden")===false;
 $("filter-toggle").setAttribute("aria-expanded",String(open));
};
document.querySelectorAll("#filter-menu [data-kind]").forEach(b=>b.onclick=()=>{
 S.filter=b.dataset.kind;
 const labels={all:"Mostrar todo",photo:"Solo fotos",video:"Solo videos"};
 $("filter-toggle").textContent=labels[S.filter]+" ▾";
 $("filter-menu").classList.add("hidden");
 $("filter-toggle").setAttribute("aria-expanded","false");
 S.selected.clear();S.selection=false;updateSelection();loadItems(false);
});
$("back-library").onclick=()=>{
 const fromDownloads=ADMIN_ACCOUNT_VIEW&&!!S.folder?.downloads;
 S.folder=null;S.selected.clear();$("filter-menu").classList.add("hidden");
 $("query").parentElement.classList.remove("hidden");
 $("filter-toggle").closest(".album-filter-bar").classList.remove("hidden");
 if(fromDownloads&&window.GalleryAccountAdmin?.showDownloadDays)
  window.GalleryAccountAdmin.showDownloadDays();
 else if(S.returnFromAdmin){S.returnFromAdmin=false;show("admin");switchAdminTab("carpetas");}
 else show("library");
};
$("load-more").onclick=()=>loadItems(true);
$("query").oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{
  S.search=$("query").value.trim();S.next=null;S.selected.clear();updateSelection();loadItems(false);
 },300);};
$("select-mode").onclick=()=>{S.selection=!S.selection;S.selected.clear();updateSelection();renderItems();};
$("selection-cancel").onclick=()=>{S.selection=false;S.selected.clear();updateSelection();renderItems();};
$("selection-download").onclick=()=>downloadIds([...S.selected]);
$("selection-original").onclick=()=>{
 if(!(S.folder?.favorites||(ADMIN_ACCOUNT_VIEW&&S.me?.admin_controls))||
    S.selected.size!==1)return;
 const id=[...S.selected][0],item=S.items.find(m=>m.id===id);
 if(item)window.CubanitasTools?.locateOriginal?.(item);
};
$("selection-admin-delete").onclick=()=>window.CubanitasTools?.adminDeleteSelection?.([...S.selected]);
$("viewer-prev").onclick=()=>moveViewer(-1);
$("viewer-next").onclick=()=>moveViewer(1);
$("close-viewer").onclick=backToViewedMedia;
$("viewer-download").onclick=()=>downloadIds([S.items[S.viewer]?.id].filter(Boolean));
$("viewer-admin-delete").onclick=()=>window.CubanitasTools?.adminDeleteSelection?.([S.items[S.viewer]?.id].filter(Boolean));
$("viewer-admin-original").onclick=()=>{
 const item=S.items[S.viewer];
 if(ADMIN_ACCOUNT_VIEW&&S.me?.admin_controls&&item?.id)
  window.CubanitasTools?.locateOriginal?.(item);
};
$("viewer-set-cover").onclick=async()=>{
 const media=S.items[S.viewer];
 if(!S.folder?.personal||media?.kind!=="photo"||!media.thumb)return;
 const folder=S.folder;
 try{
  await api("personal-set-cover",{personal_folder:folder.id,media:media.id});
  await refresh(true);
  notice("Esta foto es ahora la portada de "+folder.name+".");
 }catch(e){notice(e.message);}
};
let touchStart=null,lastDistance=0,scale=1,lastViewerTap=null;
const stage=$("viewer-stage");
stage.addEventListener("touchstart",e=>{
 if(e.touches.length===1)touchStart={x:e.touches[0].clientX,y:e.touches[0].clientY};
 if(e.touches.length===2){
  lastViewerTap=null;
  lastDistance=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);
 }
},{passive:true});
stage.addEventListener("touchmove",e=>{
 if(e.touches.length===2&&!$("view-image").hidden){
  const d=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);
  if(lastDistance>0){scale=Math.max(1,Math.min(4,scale*d/lastDistance));$("view-image").style.transform="scale("+scale+")";}
  lastDistance=d;
 }},{passive:true});
stage.addEventListener("touchend",e=>{
 if(e.touches.length===0&&touchStart&&scale===1){
  const dx=e.changedTouches[0].clientX-touchStart.x,dy=e.changedTouches[0].clientY-touchStart.y;
  if(Math.abs(dx)>65&&Math.abs(dx)>Math.abs(dy)*1.3){
   lastViewerTap=null;moveViewer(dx<0?1:-1);
  }else if(Math.abs(dx)<18&&Math.abs(dy)<18&&e.changedTouches.length===1&&
           (e.target===$("view-image")||e.target===$("view-video"))){
   const point=e.changedTouches[0],item=S.items[S.viewer],now=Date.now();
   const rect=e.target.getBoundingClientRect?.();
   const controls=e.target===$("view-video")&&rect&&point.clientY>rect.bottom-75;
   if(!controls&&item){
    const twice=lastViewerTap?.id===item.id&&now-lastViewerTap.at<=330&&
     Math.hypot(point.clientX-lastViewerTap.x,point.clientY-lastViewerTap.y)<45;
    lastViewerTap=twice?null:{id:item.id,at:now,x:point.clientX,y:point.clientY};
    if(twice)saveFavoriteFromDoubleTap(item);
   }
  }else lastViewerTap=null;
 }
 if(e.touches.length===0){touchStart=null;lastDistance=0;}
},{passive:true});
async function openAdminScreen(){
 populateEditor();show("admin");switchAdminTab("carpetas");await loadAdmin();
}
$("admin-open").onclick=openAdminScreen;
$("admin-back").onclick=()=>show("library");
$("admin-refresh").onclick=loadAdmin;
document.querySelectorAll(".admin-tabs button").forEach(b=>b.onclick=()=>switchAdminTab(b.dataset.tab));
$("new-cover").onchange=()=>{
 const file=$("new-cover").files?.[0];
 const preview=$("new-cover-preview");
 if(preview.dataset.objectUrl){URL.revokeObjectURL(preview.dataset.objectUrl);delete preview.dataset.objectUrl;}
 preview.classList.toggle("hidden",!file);
 if(file){
  const url=URL.createObjectURL(file);
  preview.dataset.objectUrl=url;preview.src=url;
 }else preview.removeAttribute("src");
};
$("add-folder").onclick=async()=>{
 const peer=$("new-peer").value.trim(),name=$("new-name").value.trim(),
  public_name=$("new-public-name").value.trim(),cover=$("new-cover").files?.[0],
  source_value=$("new-source-uid").value.trim(),
  source_account=gallerySourceInput(source_value);
 if(!/^-[0-9]{3,19}$/.test(peer)||!name||!public_name)
   return notice("Escribe el ID, el nombre interno y el nombre público por separado.");
 if(!source_account)
   return notice("Indica un Telegram ID vinculado, o DATOS:123456789 para la segunda fuente.");
 if(cover&&(!["image/jpeg","image/png","image/webp"].includes(cover.type)||
             !cover.size||cover.size>5_000_000))
   return notice("La foto debe ser JPEG, PNG o WebP, hasta 5 MB.");
 const button=$("add-folder");button.disabled=true;
 try{
   const created=await api("admin-folder",{peer,name,public_name,...source_account});
   if(cover&&!(await uploadAsset("folder-cover",cover,created.id))){
     notice("Galería creada, pero la portada no pudo guardarse. Puedes volver a intentarlo.");
     await loadAdmin();await refresh(true);return;
   }
   $("new-peer").value="";$("new-name").value="";$("new-public-name").value="";
   $("new-source-uid").value="";
   $("new-cover").value="";$("new-cover").onchange();
   $("new-cover-status").textContent="";
   notice("Carpeta agregada correctamente.");await loadAdmin();await refresh(true);
 }catch(e){notice(e.message);}
 finally{button.disabled=false;}
};
$("load-permissions").onclick=loadPermissions;
$("save-permissions").onclick=savePermissions;
$("save-design").onclick=saveDesign;
$("save-watermark").onclick=saveWatermark;
$("confirm-editor").onclick=confirmEditorChanges;
$("intro-sound").onclick=()=>{
 const video=$("intro-video"),button=$("intro-sound");
 video.muted=!video.muted;
 button.textContent=video.muted?"🔊 Activar sonido":"🔇 Silenciar video";
 video.play().catch(()=>notice("No se pudo reproducir el video automáticamente."));
};
for(const key of ["enabled","line1","line2","position","opacity","size","color"]){
 const input=$("edit-wm-"+key);
 input.addEventListener("input",previewWatermark);
 input.addEventListener("change",previewWatermark);
}
for(const selector of ["header_mode","button_mode",
                      "library_brand_mode","library_eyebrow_mode","library_heading_mode"])
 $("edit-"+selector).onchange=updateEditorLabelChoices;
for(const key of ["title_image_height","button_image_height",
                 "library_brand_image_height","library_eyebrow_image_height",
                 "library_heading_image_height"])
 $("edit-"+key).oninput=updateEditorImageSizes;
for(const key of ["logo","header","button-label-image",
                   "library-brand","library-eyebrow","library-heading",
                   "intro","intro-photo","web-promo"])
 $("upload-"+key).onchange=()=>{
  const file=$("upload-"+key).files?.[0];
  if(key==="header"&&file)$("edit-header_mode").value="image";
  if(key==="button-label-image"&&file)$("edit-button_mode").value="image";
   if(key.startsWith("library-")&&file){
    const slot=key.replaceAll("-","_");
    $("edit-"+slot+"_mode").value="image";
    loadLibraryEditorPreview(slot);
   }
  if(key==="header"||key==="button-label-image")
   loadEditorImagePreview(key==="header"?"title":"button");
  if(key==="intro"&&file){
   $("upload-intro-photo").value="";
   $("edit-intro_media").value="video";
  }
  if(key==="intro-photo"&&file){
   $("upload-intro").value="";
   $("edit-intro_media").value=file.type==="image/gif"?"gif":"photo";
  }
  updateEditorLabelChoices();
  editorPendingFilesMessage();
 };
async function browserLogin(uid,password){
 const response=await fetch("/web-galerias/admin/login",{
  method:"POST",cache:"no-store",credentials:"same-origin",
  headers:{"Content-Type":"application/json","X-Gallery-Web":"1"},
  body:JSON.stringify({uid,password})});
 if(!response.ok)throw Error((await response.text()).slice(0,230)||"Acceso incorrecto.");
 return response.json();
}
$("browser-login-form").onsubmit=async event=>{
 event.preventDefault();
 if(!BROWSER_ADMIN)return;
 const button=$("browser-login-button"),error=$("browser-login-error");
 button.disabled=true;error.textContent="";
 try{
  const uid=$("browser-admin-id").value.trim();
  const password=$("browser-admin-password").value;
  await browserLogin(uid,password);
  $("browser-admin-password").value=""; // La contraseña nunca se conserva en JS.
  await refresh();
  if(!S.me.admin)throw Error("Acceso restringido al administrador.");
  $("browser-logout").classList.remove("hidden");
  await openAdminScreen();
 }catch(e){error.textContent=e.message;}
 finally{button.disabled=false;}
};
$("browser-logout").onclick=async()=>{
 if(!BROWSER_ADMIN)return;
 try{
  const r=await fetch("/web-galerias/admin/logout",{
   method:"POST",credentials:"same-origin",cache:"no-store",
   headers:{"X-Gallery-Web":"1"}});
  if(!r.ok)throw Error("No se pudo cerrar la sesión. Intenta de nuevo.");
  S.me=null;S.admin=null;S.folders=[];
  $("browser-admin-password").value="";
  $("browser-login-error").textContent="Sesión cerrada en este dispositivo.";
  show("admin-login");
 }catch(e){notice(e.message);}
};
(async()=>{
 if(ADMIN_ACCOUNT_VIEW){
  try{
   if(!Number.isSafeInteger(ADMIN_ACCOUNT_UID)||ADMIN_ACCOUNT_UID<=0)
    throw Error("Cuenta de usuario inválida.");
   await refresh();
   if(!S.me?.admin_view)throw Error("Acceso de administrador no autorizado.");
   $("enter").disabled=true; // Never offer a checkout in an admin look-through.
   show("library");
  }catch(e){notice(e.message);$("intro-notice").textContent=e.message;show("intro");}
  return;
 }
 if(BROWSER_ADMIN){
  try{
   await refresh();
   if(!S.me?.admin)throw Error("Acceso restringido a administradores.");
   $("browser-logout").classList.remove("hidden");
   await openAdminScreen();
  }catch(e){
   show("admin-login");
   if(e.message.includes("PASSWORD"))
      $("browser-login-error").textContent=e.message;
  }
  return;
 }
 try{tg?.ready();tg?.expand();if(tg?.requestFullscreen)try{tg.requestFullscreen();}catch(_){}
  show("intro");await refresh();
  await restoreGalleryReturnPosition();}
 catch(e){$("intro-notice").textContent=e.message;notice(e.message);}
})();
/* Actualizar las vistas mientras llegan los originales. No interrumpir el visor,
   la selección de fotos ni las páginas antiguas. */
setInterval(()=>{
  if(!S.me||document.hidden||BROWSER_ADMIN||ADMIN_ACCOUNT_VIEW)return;
  if(!$("album").classList.contains("hidden")){
    if(S.folder&&!S.selection&&!S.search&&S.items.length<=60
       &&$("viewer").classList.contains("hidden"))
      loadItems(false).catch(()=>{});
  }else if(!$("library").classList.contains("hidden")){
    refresh(true).catch(()=>{});
  }
},20000);


/* Actualización automática del panel admin: consulta solo nuestra base de datos.
   Los totales remotos se consultan en el sincronizador con su cadencia propia.
   No perder valores que el administrador está editando. */
setInterval(()=>{
  if(!S.me?.admin||document.hidden||$("admin").classList.contains("hidden"))return;
  if(document.querySelector("#admin input:focus,#admin textarea:focus,#admin select:focus"))return;
  loadAdmin().catch(()=>{});
},60000);
