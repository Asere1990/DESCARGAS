"use strict";
/* Optional CUBANITAS administration and media actions. No changes to app.js billing. */
(()=>{
 const $=id=>document.getElementById(id);
 const state={folder:null,media:[],next:null,selected:new Set(),usersNext:null,usersLoaded:false,busy:false,
   userPage:1,userPages:[null],userQuery:"",usersBusy:false,userItems:[],selectedUid:null,
   folders:[],folderOpened:null,folderNext:null,folderItems:[],mediaBusy:false};
 const fmt=n=>Number(n||0).toLocaleString("es-ES");

 function adminRefreshed(admin){
  if(!admin)return;
  const select=$("admin-media-folder");
  const current=String(state.folder||select.value||"");
  select.replaceChildren(new Option("Elegir galería…",""));
  for(const f of admin.folders||[])
   select.add(new Option((f.public_name||f.name||"Galería")+" · "+f.id,String(f.id)));
  select.value=current;
 }
 function selectionStatus(){
  const n=state.selected.size;
  $("admin-media-selected").textContent=fmt(n)+" archivos seleccionados"+
    (n>100?" · máximo 100 por operación":"");
  $("admin-media-delete").disabled=n===0||state.busy;
 }
 function renderAdminMedia(){
  const root=$("admin-media-grid");root.replaceChildren();
  for(const m of state.media){
   const card=document.createElement("div");card.className="admin-media-card";
   card.classList.toggle("selected",state.selected.has(m.id));
   const box=document.createElement("input");box.type="checkbox";
   box.checked=state.selected.has(m.id);
   box.setAttribute("aria-label","Seleccionar "+(m.filename||"archivo"));
   box.onchange=()=>{
    if(box.checked&&state.selected.size>=100&&!state.selected.has(m.id)){
     box.checked=false;notice("Puedes seleccionar hasta 100 archivos.");return;
    }
    if(box.checked)state.selected.add(m.id);else state.selected.delete(m.id);
    card.classList.toggle("selected",box.checked);selectionStatus();
   };
   const visual=document.createElement("div");
   if(m.thumb){
    const img=document.createElement("img");img.src=m.thumb;img.loading="lazy";
    img.alt=m.kind==="video"?"Miniatura de video":"Miniatura de foto";
    visual.append(img);
   }else{
    const placeholder=document.createElement("span");placeholder.className="admin-media-placeholder";
    placeholder.textContent=m.kind==="video"?"▶":"▧";visual.append(placeholder);
   }
   const info=document.createElement("small");
   info.textContent=(m.kind==="video"?"Video":"Foto")+" · mensaje "+m.message;
   const view=document.createElement("button");view.type="button";view.textContent="Ver";
   view.className="admin-media-view";view.disabled=!(m.view||m.thumb);
   view.onclick=()=>{const url=m.view||m.thumb;if(url)window.open(url,"_blank","noopener,noreferrer");};
   card.onclick=e=>{if(e.target.closest("button,input"))return;
    box.checked=!box.checked;box.onchange();};
   card.append(box,visual,info,view);root.append(card);
  }
  $("admin-media-more").classList.toggle("hidden",!state.next);
  selectionStatus();
 }
 async function loadAdminMedia(older=false){
  const folder=Number($("admin-media-folder").value);
  if(!Number.isSafeInteger(folder)||folder<=0){notice("Selecciona una galería.");return;}
  const query=$("admin-media-query").value.trim();
  const kind=$("admin-media-kind").value;
  state.folder=folder;
  if(!older){state.media=[];state.next=null;state.selected.clear();}
  $("admin-media-load").disabled=true;
  try{
   const result=await api("admin-media-list",{folder,cursor:older?(state.next||0):0,query,kind});
   if(folder!==state.folder)return;
   const already=new Set(state.media.map(m=>m.id));
   for(const m of result.items||[])if(!already.has(m.id))state.media.push(m);
   state.next=result.next||null;
   renderAdminMedia();
   $("admin-media-status").textContent=fmt(state.media.length)+" archivos mostrados.";
  }catch(e){notice(e.message);}
  finally{$("admin-media-load").disabled=false;}
 }
 async function deleteMediaIds(ids,cloud=false){
  if(!S.me?.admin||state.mediaBusy)return false;
  ids=[...new Set(ids)].filter(x=>Number.isSafeInteger(x)&&x>0);
  if(!ids.length||ids.length>100){notice("Selecciona de 1 a 100 archivos.");return false;}
  const description=cloud?
    "También se borrarán los originales y miniaturas respaldados en Cloudflare R2.":
    "El respaldo de Cloudflare R2 se conservará.";
  if(!confirm("¿Eliminar "+ids.length+" archivos de TODAS las galerías de la web?\n"+
      description+"\nLos mensajes originales de Telegram permanecerán intactos."))return false;
  if(cloud&&prompt("Borrar los respaldos es irreversible. Escribe ELIMINAR:")!=="ELIMINAR")return false;
  state.mediaBusy=true;state.busy=true;selectionStatus();
  try{
   let removed=0,queued=0;
   for(let i=0;i<ids.length;i+=100){
    const batch=ids.slice(i,i+100);
    const result=await api("admin-media-remove",{
      ids:batch,remove_cloudflare:cloud,confirm:"CONFIRMAR_ELIMINACION"});
    removed+=result.removed||0;queued+=result.cloudflare_queued||0;
   }
   const gone=new Set(ids);
   state.media=state.media.filter(x=>!gone.has(x.id));
   ids.forEach(id=>{state.selected.delete(id);S.selected.delete(id);});
   S.items=S.items.filter(x=>!gone.has(x.id));
   renderAdminMedia();updateSelection();renderItems();
   if(!$("viewer").classList.contains("hidden")){
    if(!S.items.length)closeViewer();
    else {S.viewer=Math.min(S.viewer,S.items.length-1);showViewerMedia();}
   }
   $("admin-media-status").textContent=removed+" archivos eliminados de la web. "+
      (cloud?queued+" objetos programados para borrarse de R2.":"Respaldo R2 conservado.");
   closeActions();
   notice(removed+" archivos eliminados de la web.");
   await loadAdmin();await refresh(true);
   return true;
  }catch(e){notice(e.message);return false;}
  finally{state.mediaBusy=false;state.busy=false;selectionStatus();}
 }
 function adminDeleteSelection(ids){
  if(!S.me?.admin||!ids?.length)return;
  const root=$("media-context-actions");root.replaceChildren();
  $("media-context-title").textContent="Eliminar "+ids.length+" archivo"+(ids.length===1?"":"s");
  root.append(choice("Eliminar de la web · conservar Cloudflare",()=>deleteMediaIds(ids,false)));
  root.append(choice("Eliminar de la web y de Cloudflare R2",()=>deleteMediaIds(ids,true)));
  $("media-context-sheet").classList.remove("hidden");
 }
 async function removeAdminMedia(){
  const ids=[...state.selected];if(!ids.length)return;
  await deleteMediaIds(ids,$("admin-media-r2").checked);
 }
 async function activeUsers(older=false){
  const select=$("admin-user-selector");
  if(!older){state.usersNext=null;state.usersLoaded=false;
   select.replaceChildren(new Option("Seleccionar usuario…",""));}
  try{
   const data=await api("admin-active-users",{after:older?(state.usersNext||0):0});
   for(const u of data.users||[]){
    select.add(new Option((u.name||"Usuario")+" · "+u.telegram_id,String(u.telegram_id)));
   }
   state.usersNext=data.next||null;state.usersLoaded=true;
   $("admin-user-more").classList.toggle("hidden",!state.usersNext);
  }catch(e){notice(e.message);}
 }
 function renderUserProfile(profile){
  const root=$("admin-user-profile");root.replaceChildren();
  if(!profile.found){root.textContent="Este usuario no tiene ficha central registrada.";return;}
  const fields=[
   ["Nombre de Stripe",profile.stripe_name||"—"],
   ["Nombre de Telegram",profile.telegram_name||"—"],
   ["Telegram ID",profile.telegram_id],
   ["Username",profile.telegram_user||"—"],
   ["Correos",(profile.emails||[]).join("\n")||"—"],
   ["Teléfonos",(profile.phones||[]).join("\n")||"—"],
   ["Notas",profile.notes||"Sin notas registradas"],
   ["Disputas",profile.disputed?"Cuenta bloqueada":"No"]
  ];
  for(const [label,value] of fields){
   const div=document.createElement("div");div.className="admin-data-row";
   const left=document.createElement("span");left.className="admin-data-label";left.textContent=label;
   const right=document.createElement("strong");right.className="admin-data-value";
   right.textContent=String(value);right.style.whiteSpace="pre-line";
   div.append(left,right);root.append(div);
  }
  const h=document.createElement("h3");h.textContent="Membresías y estado";root.append(h);
  const list=document.createElement("div");list.className="admin-membership-list";
  for(const m of profile.memberships||[]){
   const div=document.createElement("div");div.className="admin-member-status";
   div.textContent=m.name+" · "+m.status+
     (m.access_type==="Web"?" · Tipo de acceso: Web":"")+
     (m.expires?" · Vencimiento: "+m.expires:"");
   list.append(div);
  }
  if(!list.children.length)list.textContent="Sin membresías registradas.";
  root.append(list);
 }
 async function showUserProfile(){
  const uid=Number($("admin-user-selector").value);
  if(!Number.isSafeInteger(uid)||uid<=0){notice("Selecciona un usuario.");return;}
  $("admin-user-profile").textContent="Consultando ficha de CUSTOMER-SERVICE…";
  try{renderUserProfile(await api("admin-user-profile",{telegram_id:uid}));}
  catch(e){$("admin-user-profile").textContent=e.message;notice(e.message);}
 }

 function closeActions(){
  $("media-context-sheet").classList.add("hidden");
 }
 function choice(title,callback){
  const b=document.createElement("button");b.type="button";b.textContent=title;
  b.onclick=callback;return b;
 }
 async function moveSelected(ids,folderId){
  const source=S.folder;
  try{
   if(source?.personal){
    await api("personal-move",{personal_folder:source.id,target_folder:folderId,ids});
    await loadItems(false);
   }else await api("personal-add",{personal_folder:folderId,ids});
   await refresh(true);
   notice(ids.length===1?"Archivo movido a tu carpeta.":"Archivos guardados en tu carpeta.");
  }catch(e){notice(e.message);}
 }
 function movePicker(ids){
  const root=$("media-context-actions");root.replaceChildren();
  $("media-context-title").textContent=ids.length>1?"Mover "+ids.length+" archivos":"Mover a una carpeta";
  const folders=S.personal.filter(p=>!S.folder?.personal||p.id!==S.folder.id);
  for(const folder of folders){
   root.append(choice("📁 "+folder.name,async()=>{
    closeActions();await moveSelected(ids,folder.id);
   }));
  }
  root.append(choice("＋ Crear carpeta nueva",async()=>{
   closeActions();
   // Existing personal-create still saves only references, never R2 originals.
   if(S.folder?.personal){
    const name=prompt("Nombre de la nueva carpeta:");if(!name)return;
    try{
     const result=await api("personal-create",{name,ids});
     await api("personal-remove",{personal_folder:S.folder.id,ids});
     await refresh(true);await loadItems(false);
     notice("Archivos movidos a "+result.name+".");
    }catch(e){notice(e.message);}
   }else await createPersonalFolder(ids);
  }));
  root.append(choice("‹ Volver",()=>mediaActions({id:ids[0]})));
 }
 async function locateOriginal(item){
  closeActions();
  try{
   const result=await api("locate-media",{media:item.id});
   ++S.epoch;
   S.folder=result.folder;S.items=result.items;S.next=result.next;S.search="";
   S.filter="all";S.selection=false;S.selected.clear();
   $("query").value="";$("album-title").textContent=result.folder.name;
   $("album-subtitle").textContent="Galería original · fotos y videos cercanos";
   $("filter-toggle").textContent="Mostrar todo ▾";
   $("filter-menu").classList.add("hidden");
   $("filter-toggle").setAttribute("aria-expanded","false");
   renderDropTargets();updateSelection();show("album");renderItems();
   $("load-more").classList.toggle("hidden",!S.next);
   const mediaId=String(result.focus);
   requestAnimationFrame(()=>{
    const tile=$("album-grid").querySelector('[data-media-id="'+mediaId+'"]');
    if(tile){
     tile.scrollIntoView({block:"center",behavior:"smooth"});
     tile.classList.add("located-media");
     setTimeout(()=>tile.classList.remove("located-media"),3500);
    }
   });
  }catch(e){notice(e.message);}
 }
 function mediaActions(item){
  if(!item?.id)return;
  const selected=S.selected.has(item.id)?[...S.selected]:[item.id];
  const ids=selected.length>1?selected.slice(0,10):[item.id];
  const root=$("media-context-actions");root.replaceChildren();
  $("media-context-title").textContent=ids.length>1?
   ids.length+" archivos seleccionados":"Opciones de foto o video";
  root.append(choice("↓ Descargar al teléfono",async()=>{
   closeActions();await downloadIds(ids);
  }));
  root.append(choice("📁 Mover a una carpeta",()=>movePicker(ids)));
  // Original search is only meaningful for one file. Multi-selection has
  // exactly two choices, regardless of source collection or search term.
  if(ids.length===1)root.append(choice("⌕ Buscar en galería original",
                                      ()=>locateOriginal(item)));
  $("media-context-sheet").classList.remove("hidden");
 }

 function initialize(){
  $("admin-media-load").onclick=()=>loadAdminMedia(false);
  $("admin-media-more").onclick=()=>loadAdminMedia(true);
  $("admin-media-delete").onclick=removeAdminMedia;
  $("admin-media-folder").onchange=()=>{
   state.folder=Number($("admin-media-folder").value)||null;
   state.media=[];state.next=null;renderAdminMedia();
  };
  $("admin-users-details").ontoggle=()=>{
   if($("admin-users-details").open&&!state.usersLoaded)activeUsers();
  };
  $("admin-user-more").onclick=()=>activeUsers(true);
  $("admin-user-open").onclick=showUserProfile;
  $("media-context-close").onclick=closeActions;
  $("media-context-sheet").onclick=e=>{if(e.target===$("media-context-sheet"))closeActions();};
 }
 window.CubanitasTools={adminRefreshed,mediaActions};
 initialize();
})();
