"use strict";
const $=id=>document.getElementById(id);
const tg=window.Telegram?.WebApp;
const ENDPOINT="/web-galerias/api";
const BROWSER_ADMIN=/^\/web-galerias\/admin\/?$/.test(location.pathname)&&!tg?.initData;
const S={me:null,folders:[],personal:[],folder:null,filter:"all",items:[],next:null,search:"",selection:false,selected:new Set(),viewer:0,admin:null,epoch:0,toastTimer:null,prepared:null,saveData:null,suppressClick:null};
const pretty=n=>Number(n||0).toLocaleString("es-ES");
const esc=x=>String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const show=id=>{for(const node of document.querySelectorAll(".screen"))node.classList.add("hidden");$(id).classList.remove("hidden");window.scrollTo(0,0);};
function notice(message){const t=$("toast");t.textContent=message;t.classList.remove("hidden");clearTimeout(S.toastTimer);S.toastTimer=setTimeout(()=>t.classList.add("hidden"),4200);}
function applyDesign(d){const r=document.documentElement.style;
 r.setProperty("--accent",d.accent);r.setProperty("--bg",d.background);r.setProperty("--text",d.text_color);
 r.setProperty("--button",d.button_color);r.setProperty("--tile-aspect",d.tile_shape==="rectangle"?"4 / 3":"1");
 $("intro-headline").textContent=d.title;$("brand-name").textContent=d.title;
 $("intro-subtitle").textContent=d.intro_caption;$("enter").textContent=d.button_label;
 $("intro-placeholder").lastElementChild.textContent=d.intro_label;
 document.title=d.title;
 for(const [id,key] of [["brand-logo","logo"]]){
   const image=$(id),icon=$("brand-icon");image.onload=()=>{image.hidden=false;icon.hidden=true;};
   image.onerror=()=>{image.hidden=true;icon.hidden=false;};image.src="/web-galerias/assets/"+key+"?t="+Date.now();}
 if(d.header_mode==="image"){
   const image=new Image();image.onload=()=>{$("intro-headline").innerHTML="";
     image.alt=d.title;image.style.cssText="max-width:min(90vw,460px);max-height:120px;object-fit:contain";
     $("intro-headline").append(image);};
   image.src="/web-galerias/assets/header?t="+Date.now();}
 const video=$("intro-video"),placeholder=$("intro-placeholder");
 video.onloadedmetadata=()=>{video.hidden=false;placeholder.hidden=true;};
 video.onerror=()=>{video.hidden=true;placeholder.hidden=false;};
 video.src="/web-galerias/assets/intro?t="+Date.now();
}
async function api(op,data={}){
 const raw=tg?.initData||"";
 if(!raw&&!BROWSER_ADMIN)
   throw Error("Debes abrir Web Galerías desde el botón del bot en Telegram.");
 const headers={"Content-Type":"application/json"};
 if(raw)headers["X-Telegram-Init-Data"]=raw;
 else headers["X-Gallery-Web"]="1";
 const r=await fetch(ENDPOINT,{method:"POST",cache:"no-store",credentials:"same-origin",
  headers,body:JSON.stringify({op,...data})});
 if(!r.ok){let text=await r.text();text=text.replace(/<[^>]*>/g," ").trim();
  throw Error(text.slice(0,240)||"Servicio no disponible.");}
 return r.json();
}
async function refresh(lite=false){
 S.me=await api("bootstrap",{name:tg?.initDataUnsafe?.user?.first_name||""});
 if(!lite)applyDesign(S.me.design);S.folders=S.me.folders;
 S.personal=S.me.personal_folders||[];renderFolders();
 $("admin-open").classList.toggle("hidden",!S.me.admin);
 $("stat-folders").textContent=pretty(S.folders.length);
 $("stat-photos").textContent=pretty(S.me.photos);
 $("stat-videos").textContent=pretty(S.me.videos);
 $("quota-count").textContent=pretty(S.me.remaining)+" de 100";
 $("quota-fill").style.width=(S.me.remaining)+"%";
 if(!S.me.allowed)$("intro-notice").textContent=S.me.pilot?
  "Prueba privada: solamente usuarios autorizados.":"Membresía necesaria para entrar";
 else $("intro-notice").textContent=S.me.admin?"Acceso de administrador":"Acceso verificado";
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
  card.onclick=()=>openFolder(folder);
  card.onkeydown=e=>{if(e.key==="Enter")openFolder(folder);};
  root.append(card);
 }
 if(!S.folders.length)root.innerHTML='<div class="empty">El administrador todavía no ha publicado carpetas.</div>';
 renderPersonalFolders();
}
function renderPersonalFolders(){
 $("personal-section").classList.toggle("hidden",!S.me?.allowed);
 const root=$("personal-folders");root.replaceChildren();
 for(const folder of S.personal){
  const card=document.createElement("article");card.className="folder-card personal-folder-card";
  const cover=document.createElement("div");cover.className="folder-cover";
  const icon=document.createElement("span");icon.className="folder-icon";icon.textContent="📁";cover.append(icon);
  const title=document.createElement("h3");title.textContent=folder.name;
  const count=document.createElement("p");count.textContent=pretty(folder.count)+" archivos";
  const tools=document.createElement("div");tools.className="personal-folder-tools";
  const rename=document.createElement("button");rename.textContent="✎";rename.type="button";rename.title="Cambiar nombre";
  rename.onclick=e=>{e.stopPropagation();renamePersonalFolder(folder);};
  const erase=document.createElement("button");erase.textContent="×";erase.type="button";erase.title="Eliminar carpeta personal";
  erase.onclick=e=>{e.stopPropagation();deletePersonalFolder(folder);};
  tools.append(rename,erase);card.append(cover,tools,title,count);
  card.onclick=()=>openFolder({...folder,personal:true});
  root.append(card);
 }
 renderDropTargets();
}
async function createPersonalFolder(){
 const name=prompt("Nombre de tu nueva carpeta:");
 if(!name)return;
 try{
  const data=await api("personal-create",{name});
  await refresh(true);notice("Carpeta creada: "+data.name);
 }catch(e){notice(e.message);}
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
 for(const folder of S.personal){
  const b=document.createElement("button");b.type="button";b.textContent="📁 "+folder.name;
  b.dataset.folderDrop=String(folder.id);
  b.ondragover=e=>{e.preventDefault();b.classList.add("drag-target");};
  b.ondragleave=()=>b.classList.remove("drag-target");
  b.ondrop=e=>{e.preventDefault();b.classList.remove("drag-target");
   const id=Number(e.dataTransfer.getData("text/plain"));
   if(Number.isSafeInteger(id)&&id>0)addToPersonalFolder(folder.id,[id]);
  };
  b.onclick=()=>{if(S.selected.size)addToPersonalFolder(folder.id,[...S.selected]);};
  root.append(b);
 }
}
async function addToPersonalFolder(folder,ids){
 if(!ids.length)return;
 try{
  await api("personal-add",{personal_folder:folder,ids:ids.slice(0,10)});
  S.selection=false;S.selected.clear();updateSelection();renderItems();
  await refresh(true);notice("Guardado en tu carpeta personal.");
 }catch(e){notice(e.message);}
}
async function openFolder(folder){
 S.folder=folder;S.items=[];S.next=null;S.search="";S.filter="all";
 S.selection=false;S.selected.clear();
 $("query").value="";$("album-title").textContent=folder.name;
 $("album-subtitle").textContent=folder.personal?pretty(folder.count)+" archivos":
   pretty(folder.photos)+" fotos · "+pretty(folder.videos)+" videos";
 $("filter-toggle").textContent="Mostrar todo ▾";$("filter-menu").classList.add("hidden");
 $("filter-toggle").setAttribute("aria-expanded","false");
 renderDropTargets();
 $("select-mode").textContent="Seleccionar";updateSelection();show("album");await loadItems(false);
}
async function loadItems(older=false){
 if(!S.folder)return;
 const epoch=++S.epoch;
 $("empty-gallery").classList.add("hidden");
 if(!older){S.items=[];$("album-grid").replaceChildren();}
 try{
  const data=await api("items",{...(S.folder.personal?{collection:S.folder.id}:{folder:S.folder.id}),
    kind:S.filter,query:S.search,cursor:older?(S.next||0):0});
  if(epoch!==S.epoch)return;
  S.items.push(...data.items);S.next=data.next;
  renderItems();$("load-more").classList.toggle("hidden",!S.next);
  $("empty-gallery").classList.toggle("hidden",S.items.length>0);
  $("empty-gallery").textContent=S.search?"No se encontraron fotos o videos relacionados.":"Todavía no hay fotos o videos disponibles en esta carpeta.";
 }catch(e){if(epoch===S.epoch)notice(e.message);}
}
function renderItems(){
 const grid=$("album-grid");grid.replaceChildren();
 S.items.forEach((m,index)=>{
  const b=document.createElement("button");b.className="media-tile"+(S.selected.has(m.id)?" selected":"");
  b.type="button";b.draggable=!!window.matchMedia?.("(pointer:fine)")?.matches;
  b.setAttribute("aria-label",m.kind==="video"?"Abrir video":"Abrir foto");
  if(m.thumb){const img=document.createElement("img");img.src=m.thumb;img.loading="lazy";img.decoding="async";img.alt="";
   img.onerror=()=>{img.remove();fallback(b,m);};b.append(img);}else fallback(b,m);
  if(m.kind==="video"){const mark=document.createElement("span");mark.className="video-mark";mark.textContent="▶";b.append(mark);}
  attachLongPress(b,m,index);
  b.onclick=()=>{
   if(S.suppressClick===m.id){S.suppressClick=null;return;}
   S.selection?toggleSelection(m.id):openViewer(index);
  };
  grid.append(b);
 });
}
function attachLongPress(b,m,index){
 let timer=null,held=false,active=false,startX=0,startY=0,hover=null;
 const clear=()=>{clearTimeout(timer);timer=null;if(hover){hover.classList.remove("drag-target");hover=null;}};
 b.addEventListener("contextmenu",e=>e.preventDefault());
 b.addEventListener("dragstart",e=>{
  e.dataTransfer.setData("text/plain",String(m.id));e.dataTransfer.effectAllowed="copy";
 });
 b.addEventListener("pointerdown",e=>{
  if(e.pointerType==="mouse"&&e.button!==0)return;
  startX=e.clientX;startY=e.clientY;held=false;active=true;
  clearTimeout(timer);
  timer=setTimeout(()=>{
   if(!active)return;
   if(!S.selected.has(m.id)&&S.selected.size>=10){
    notice("Puedes seleccionar hasta 10 archivos.");return;
   }
   held=true;S.selection=true;
   S.selected.add(m.id);
   updateSelection();b.classList.add("selected","dragging");
   tg?.HapticFeedback?.selectionChanged?.();
  },450);
 });
 b.addEventListener("pointermove",e=>{
  if(!active)return;
  if(!held&&Math.hypot(e.clientX-startX,e.clientY-startY)>12){clearTimeout(timer);return;}
  if(held){
   const el=document.elementFromPoint(e.clientX,e.clientY)?.closest("[data-folder-drop]");
   if(hover!==el){if(hover)hover.classList.remove("drag-target");
    hover=el;if(hover)hover.classList.add("drag-target");}
  }
 });
 b.addEventListener("touchmove",e=>{
  // En iPhone mantener el dedo pulsado permite arrastrar en vez de
  // desplazarse por la página cuando ya se activó la selección.
  if(held)e.preventDefault();
 },{passive:false});
 b.addEventListener("pointerup",e=>{
  if(!active)return;active=false;clearTimeout(timer);
  if(!held)return;
  const destination=document.elementFromPoint(e.clientX,e.clientY)?.closest("[data-folder-drop]");
  S.suppressClick=m.id;setTimeout(()=>{if(S.suppressClick===m.id)S.suppressClick=null;},450);
  if(destination){addToPersonalFolder(Number(destination.dataset.folderDrop),[m.id]);}
  else{b.classList.remove("dragging");renderItems();}
  clear();
 });
 b.addEventListener("pointercancel",()=>{active=false;clear();b.classList.remove("dragging");});
 b.addEventListener("pointerleave",()=>{if(!held){clearTimeout(timer);}});
}
function fallback(b,m){const p=document.createElement("div");p.className="fallback";p.textContent=m.kind==="video"?"▶":"▧";b.prepend(p);}
function updateSelection(){
 $("selection-bar").classList.toggle("hidden",!S.selection);
 $("album-actions").classList.toggle("hidden",S.selection);
 $("selection-count").textContent=S.selected.size+" de 10";
 $("select-mode").textContent=S.selection?"Listo":"Seleccionar";
 $("selection-download").disabled=!S.selected.size||
   [...S.selected].some(id=>!S.items.find(x=>x.id===id)?.available);
 $("selection-organize").disabled=!S.selected.size;
}
function toggleSelection(id){
 const media=S.items.find(m=>m.id===id);
 if(!media)return;
 if(S.selected.has(id))S.selected.delete(id);
 else if(S.selected.size<10)S.selected.add(id);
 else return notice("Puedes seleccionar hasta 10 archivos por operación.");
 updateSelection();renderItems();
}
function showViewerMedia(){
  if(!S.items.length)return;
  const m=S.items[S.viewer];$("viewer-counter").textContent=(S.viewer+1)+" / "+S.items.length;
  $("viewer-watermark").textContent="Búscame en Google\nhoo.be/Cubanas";
  const img=$("view-image"),video=$("view-video");
  video.pause();video.removeAttribute("src");img.style.transform="scale(1)";
  const full=!!m.available&&!!m.view;
  if(m.kind==="video"&&full){
    img.hidden=true;video.hidden=false;video.src=m.view;
  }else{
    video.hidden=true;img.hidden=false;
    if(full||m.thumb)img.src=full?m.view:m.thumb;
    else img.removeAttribute("src");
    img.alt=full?"Vista completa":"Vista previa; original en preparación";
  }
  $("viewer-download").disabled=!full;
  $("viewer-download").title=full?"Guardar archivo":"Original en preparación";
  $("viewer-prev").disabled=S.viewer===0;$("viewer-next").disabled=S.viewer===S.items.length-1;
}
function openViewer(index){
  S.viewer=index;showViewerMedia();$("viewer").classList.remove("hidden");
  if(!S.items[index].available)notice("El original se está preparando; la miniatura se muestra si está disponible.");
}
function closeViewer(){$("viewer").classList.add("hidden");$("view-video").pause();}
function moveViewer(delta){const i=S.viewer+delta;if(i<0||i>=S.items.length)return;S.viewer=i;showViewerMedia();}
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
  S.me.remaining=data.remaining;$("quota-count").textContent=pretty(data.remaining)+" de 100";
  $("quota-fill").style.width=data.remaining+"%";
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
 newFolder.onclick=async()=>{$("folder-picker").classList.add("hidden");await createPersonalFolder();openFolderPicker();};
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
 for(const f of S.admin.folders){
  const card=document.createElement("div");card.className="admin-row";
  const title=document.createElement("h3");title.textContent=(f.name||"Sin nombre interno")+(f.active?"":" (oculta)");
  const info=document.createElement("p");
  const state=!f.active?"Pausada":S.admin.sync_enabled?"Respaldo activo":"Esperando sincronizador";
  info.textContent="Nombre público: "+(f.public_name||("Galería "+f.id))+" · Origen: "+f.peer+" · "+state+" · "+
    pretty(f.ready)+" archivos respaldados · "+pretty(f.pending)+" pendientes · "+
    pretty(f.scan_count)+" mensajes revisados · "+
    (f.complete?"Historial catalogado":"Catalogación en progreso")+
    (f.missing?" · "+pretty(f.missing)+" no disponibles":"")+
    (f.skipped?" · "+pretty(f.skipped)+" omitidos":"")+
    (f.error?" · "+f.error:"");
  const buttons=document.createElement("div");buttons.className="row-actions";
  const edit=document.createElement("button");edit.className="small-btn";edit.textContent="Nombre interno";
  edit.onclick=async()=>{const value=prompt("Nombre que solo ve el administrador",f.name);
    if(!value||value===f.name)return;
    try{await api("admin-folder-update",{folder:f.id,field:"name",name:value});await loadAdmin();}catch(e){notice(e.message);}};
  const publicEdit=document.createElement("button");publicEdit.className="small-btn";publicEdit.textContent="Nombre público";
  publicEdit.onclick=async()=>{const value=prompt("Nombre visible para los clientes",f.public_name||("Galería "+f.id));
    if(!value||value===f.public_name)return;
    try{await api("admin-folder-update",{folder:f.id,field:"public_name",name:value});
        await loadAdmin();await refresh();
    }catch(e){notice(e.message);}};
  const remove=document.createElement("button");remove.className="small-btn";
  remove.textContent=f.active?"Ocultar":"Restaurar";
  remove.onclick=async()=>{
   try{
    if(f.active)await api("admin-folder-remove",{folder:f.id});
    else await api("admin-folder",{peer:f.peer,name:f.name});
    await loadAdmin();
   }catch(e){notice(e.message);}
  };
  const reset=document.createElement("button");reset.className="small-btn";reset.textContent="Revisar origen";
  reset.onclick=async()=>{try{await api("admin-sync-reset",{folder:f.id});notice("Revisión solicitada");await loadAdmin();}catch(e){notice(e.message);}};
  buttons.append(edit,publicEdit,remove,reset);card.append(title,info,buttons);root.append(card);
 }
 const users=$("admin-users");users.replaceChildren();
 for(const u of S.admin.users){
  const item=document.createElement("div");item.className="admin-row";
  const title=document.createElement("h3");title.textContent=(u.name||"Usuario")+" · "+u.uid;
  const p=document.createElement("p");
  const active=u.admin?"Administrador":u.tester?"Prueba gratuita":
    (u.state==="active"&&u.paid_until>Date.now()/1000)?"Membresía vigente":
      u.state||"Sin membresía";
  p.textContent=active+" · Último acceso: "+new Date(u.last_seen*1000).toLocaleString("es-US");
  const b=document.createElement("button");b.textContent="Asignar carpetas";b.className="small-btn";
  b.onclick=()=>{$("permission-uid").value=u.uid;loadPermissions();window.scrollTo({top:0,behavior:"smooth"});};
  item.append(title,p,b);users.append(item);
 }
 const h=$("admin-health");
 h.innerHTML="<h2>Estado del respaldo</h2><p>"+(S.admin.storage_ready?
  "✓ Almacenamiento R2 conectado":"⚠ Almacenamiento privado pendiente")+
  "</p><p>"+(S.admin.sync_enabled?
   "✓ Copia masiva automática: "+esc(S.admin.sync_phase||"en marcha"):
   "⚠ Sincronizador detenido o pendiente de conexión")+
  "</p><p>Agregar carpeta → comenzar; ocultar carpeta → pausar; restaurar → continuar. No se borran archivos de R2.</p>";
}
async function loadAdmin(){
 try{S.admin=await api("admin-overview");previewAdmin();}catch(e){notice(e.message);}
}
function switchAdminTab(tab){
 document.querySelectorAll(".admin-tabs button").forEach(b=>b.classList.toggle("active",b.dataset.tab===tab));
 document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("hidden",x.id!=="tab-"+tab));
}
function populateEditor(){
 const d=S.me.design;
 for(const k of ["title","intro_label","intro_caption","button_label","header_mode","tile_shape",
                 "button_color","accent","background","text_color"])$("edit-"+k).value=d[k]||"";
}
async function saveDesign(){
 const design={};for(const k of ["title","intro_label","intro_caption","button_label","header_mode","tile_shape",
               "button_color","accent","background","text_color"])design[k]=$("edit-"+k).value;
 design.daily_limit=100;
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
async function uploadAsset(key,file){
 if(!file)return;
 const allowed=key==="intro"?25_000_000:key==="header"?5_000_000:3_000_000;
 if(file.size>allowed)return notice("El archivo es demasiado grande.");
 const mime=file.type;
 const upload=Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,"0")).join("");
 const size=192*1024,total=Math.ceil(file.size/size);
 if(total>130)return notice("El archivo excede el máximo de bloques.");
 try{
  for(let i=0;i<total;i++){
   const block=new Uint8Array(await file.slice(i*size,(i+1)*size).arrayBuffer());
   let chunk="";for(let j=0;j<block.length;j+=8192)
     chunk+=String.fromCharCode(...block.subarray(j,j+8192));
   await api("admin-upload",{upload,key,mime,part:i,total,data:btoa(chunk)});
   $("upload-progress").textContent="Subiendo "+(i+1)+" de "+total+" bloques…";
  }
  $("upload-progress").textContent="Archivo guardado correctamente";
  notice("Imagen o video actualizado");await refresh();
 }catch(e){$("upload-progress").textContent=e.message;notice(e.message);}
}
let searchTimer=null;
$("enter").onclick=async()=>{
 if(!S.me)return notice("La Mini App aún se está configurando.");
 if(S.me.allowed){show("library");return;}
 if(S.me.pilot)return notice("Acceso reservado a los usuarios de prueba.");
 try{const data=await api("checkout");if(data.allowed){await refresh();show("library");}
  else if(data.url){tg?.openLink?tg.openLink(data.url):location.assign(data.url);}}
 catch(e){notice(e.message);}
};
$("go-intro").onclick=()=>show("intro");
$("create-personal-folder").onclick=createPersonalFolder;
$("album-create-folder").onclick=createPersonalFolder;
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
 S.folder=null;S.selected.clear();$("filter-menu").classList.add("hidden");show("library");
};
$("load-more").onclick=()=>loadItems(true);
$("query").oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{
  S.search=$("query").value.trim();S.next=null;S.selected.clear();updateSelection();loadItems(false);
 },300);};
$("select-mode").onclick=()=>{S.selection=!S.selection;S.selected.clear();updateSelection();renderItems();};
$("selection-cancel").onclick=()=>{S.selection=false;S.selected.clear();updateSelection();renderItems();};
$("selection-download").onclick=()=>downloadIds([...S.selected]);
$("viewer-prev").onclick=()=>moveViewer(-1);
$("viewer-next").onclick=()=>moveViewer(1);
$("close-viewer").onclick=closeViewer;
$("viewer-download").onclick=()=>downloadIds([S.items[S.viewer]?.id].filter(Boolean));
let touchStart=null,lastDistance=0,scale=1;
const stage=$("viewer-stage");
stage.addEventListener("touchstart",e=>{
 if(e.touches.length===1)touchStart={x:e.touches[0].clientX,y:e.touches[0].clientY};
 if(e.touches.length===2)lastDistance=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);
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
  if(Math.abs(dx)>65&&Math.abs(dx)>Math.abs(dy)*1.3)moveViewer(dx<0?1:-1);
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
$("add-folder").onclick=async()=>{
 const peer=$("new-peer").value.trim(),name=$("new-name").value.trim(),
  public_name=$("new-public-name").value.trim();
 if(!/^-[0-9]{3,19}$/.test(peer)||!name||!public_name)
   return notice("Escribe el ID, el nombre interno y el nombre público por separado.");
 try{await api("admin-folder",{peer,name,public_name});$("new-peer").value="";
   $("new-name").value="";$("new-public-name").value="";
  notice("Carpeta agregada: el respaldo se iniciará automáticamente si el sincronizador está conectado.");await loadAdmin();await refresh();}
 catch(e){notice(e.message);}
};
$("load-permissions").onclick=loadPermissions;
$("save-permissions").onclick=savePermissions;
$("save-design").onclick=saveDesign;
for(const key of ["logo","header","intro"])$("upload-"+key).onchange=e=>uploadAsset(key,e.target.files[0]);
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
  show("intro");await refresh();}
 catch(e){$("intro-notice").textContent=e.message;notice(e.message);}
})();
/* Actualizar las vistas mientras llegan los originales. No interrumpir el visor,
   la selección de fotos ni las páginas antiguas. */
setInterval(()=>{
  if(!S.me||document.hidden||BROWSER_ADMIN)return;
  if(!$("album").classList.contains("hidden")){
    if(S.folder&&!S.selection&&!S.search&&S.items.length<=60
       &&$("viewer").classList.contains("hidden"))
      loadItems(false).catch(()=>{});
  }else if(!$("library").classList.contains("hidden")){
    refresh(true).catch(()=>{});
  }
},20000);
