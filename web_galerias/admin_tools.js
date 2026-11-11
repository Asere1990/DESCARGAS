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
 function usersLoaded(){return state.usersLoaded;}
 function renderUserRows(){
  const root=$("admin-users");root.replaceChildren();
  for(const u of state.userItems){
   const b=document.createElement("button");b.type="button";
   b.className="admin-user-list-row";
   b.setAttribute("role","listitem");
   b.setAttribute("aria-label",u.name+(u.paid?" · Activo":"")+(u.online?" · En línea":""));
   const name=document.createElement("span");name.className="admin-user-list-name";
   name.textContent=u.name||"Usuario";
   const right=document.createElement("span");right.className="admin-user-indicators";
   if(u.blocked){
    const badge=document.createElement("small");badge.className="admin-user-retired";
    badge.textContent="Retirado";right.append(badge);
   }else if(u.paid){
    const badge=document.createElement("small");badge.className="admin-user-active";
    badge.textContent="Activo";right.append(badge);
   }
   if(u.online&&!u.blocked){
    const dot=document.createElement("i");dot.className="admin-user-online";
    dot.title="Conectado ahora a Web Galerías";
    dot.setAttribute("aria-label","Conectado");right.append(dot);
   }
   b.append(name,right);
   b.onclick=()=>showUserProfile(u.telegram_id);
   root.append(b);
  }
  if(!state.userItems.length){
   const empty=document.createElement("p");empty.className="muted";empty.textContent="No hay usuarios para esta búsqueda.";
   root.append(empty);
  }
  $("admin-users-page").textContent="Página "+state.userPage;
  $("admin-user-back-page").disabled=state.userPage===1;
  $("admin-user-more").disabled=!state.usersNext;
 }
 async function loadUserPage(reset=false){
  if(state.usersBusy)return;
  state.usersBusy=true;
  if(reset){state.userPage=1;state.userPages=[null];state.selectedUid=null;
   $("admin-user-profile").classList.add("hidden");}
  const requestQuery=$("admin-users-query").value.trim();
  const page=state.userPage;
  const after=state.userPages[page-1]||null;
  try{
   const result=await api("admin-users-page",{query:requestQuery,after});
   if(page!==state.userPage||requestQuery!==$("admin-users-query").value.trim())return;
   state.userItems=result.users||[];state.usersNext=result.next||null;
   state.userPages[page]=state.usersNext;
   state.usersLoaded=true;renderUserRows();
  }catch(e){notice(e.message);}
  finally{state.usersBusy=false;}
 }
 function openUsers(){if(!state.usersLoaded)loadUserPage(true);}
 function rowDatum(root,label,value){
  const row=document.createElement("div");row.className="admin-data-row";
  const key=document.createElement("span");key.className="admin-data-label";key.textContent=label;
  const val=document.createElement("strong");val.className="admin-data-value";
  val.textContent=String(value===null||value===undefined?"—":value);
  val.style.whiteSpace="pre-line";row.append(key,val);root.append(row);
 }
 function renderUserProfile(profile){
  const root=$("admin-user-central");if(!root)return;
  root.replaceChildren();
  const h=document.createElement("h3");h.textContent="Ficha central · CUSTOMER-SERVICE";root.append(h);
  if(!profile.found){
   root.append(document.createTextNode("No existe una ficha central para este usuario."));
   return;
  }
  for(const [label,value] of [
   ["Nombre de Stripe",profile.stripe_name||"—"],
   ["Nombre de Telegram",profile.telegram_name||"—"],
   ["Telegram ID",profile.telegram_id],
   ["Username",profile.telegram_user||"—"],
   ["Correos",(profile.emails||[]).join("\n")||"—"],
   ["Teléfonos",(profile.phones||[]).join("\n")||"—"],
   ["Notas",profile.notes||"Sin notas registradas"],
   ["Disputas",profile.disputed?"Cuenta bloqueada":"No"]])
   rowDatum(root,label,value);
  const section=document.createElement("h3");section.textContent="Membresías y estado";root.append(section);
  const list=document.createElement("div");list.className="admin-membership-list";
  for(const m of profile.memberships||[]){
   const item=document.createElement("div");item.className="admin-member-status";
   item.textContent=m.name+" · "+m.status+
    (m.access_type==="Web"?" · Tipo de acceso: Web":"")+
    (m.expires?" · Vencimiento: "+m.expires:"");
   list.append(item);
  }
  if(!list.children.length)list.textContent="Sin membresías registradas.";
  root.append(list);
 }
 async function showUserProfile(uid){
  if(state.selectedUid===uid&&state.userProfileOpened)return;
  state.selectedUid=uid;state.userProfileOpened=false;
  const root=$("admin-user-profile");root.classList.remove("hidden");
  root.replaceChildren();
  const title=document.createElement("h3");title.textContent="Ficha del usuario";
  const close=document.createElement("button");close.type="button";close.className="small-btn";
  close.textContent="Cerrar ficha";
  close.onclick=()=>{state.selectedUid=null;root.classList.add("hidden");};
  root.append(title,close);
  const status=document.createElement("p");status.className="muted";
  status.textContent="Consultando los datos…";root.append(status);
  root.scrollIntoView?.({block:"nearest",behavior:"smooth"});
  try{
   const local=await api("admin-user-access-status",{telegram_id:uid});
   if(state.selectedUid!==uid)return;
   state.userProfileOpened=true;status.remove();
   rowDatum(root,"Nombre de Telegram",local.name||"Usuario");
   rowDatum(root,"Telegram ID",uid);
   rowDatum(root,"Membresía pagada",
     ["active","trialing"].includes(local.state)&&local.paid_until>Date.now()/1000?"Activo":"Sin pago vigente");
   rowDatum(root,"Estado en la web",local.blocked?"Retirado del sistema":"Autorización según membresía");
   rowDatum(root,"Último acceso",local.last_seen?
     new Date(local.last_seen*1000).toLocaleString("es-US"):"—");
   const actions=document.createElement("div");actions.className="admin-profile-actions";
   const folders=document.createElement("button");folders.className="small-btn strong";
   folders.textContent="📁 Ver carpetas personalizadas";
   folders.onclick=()=>loadUserFolders(uid);
   const perms=document.createElement("button");perms.className="small-btn";
   perms.textContent="Asignar carpetas de acceso";
   perms.onclick=()=>{$("permission-uid").value=uid;loadPermissions();};
   const toggle=document.createElement("button");toggle.className="small-btn admin-user-access-action";
   toggle.textContent=local.blocked?"Volver a agregar al sistema":"Eliminar usuario del sistema";
   toggle.onclick=async()=>{
    const withdraw=!local.blocked;
    const warning=withdraw?
      "¿Retirar a este usuario de Web Galerías? Perderá el acceso inmediatamente.\n"+
      "La suscripción en Stripe y sus carpetas se conservarán para permitir restaurarlo.":
      "¿Volver a agregar a este usuario a Web Galerías?\n"+
      "Recuperará acceso si su membresía o autorización de prueba sigue vigente.";
    if(!confirm(warning))return;
    toggle.disabled=true;
    try{
     await api("admin-user-access-change",{telegram_id:uid,blocked:withdraw,
       confirm:"CAMBIAR_ACCESO_GALERIAS"});
     state.usersLoaded=false;state.userProfileOpened=false;
     await loadUserPage(false);await showUserProfile(uid);
     notice(withdraw?"Usuario retirado del sistema.":"Usuario restaurado.");
    }catch(e){notice(e.message);}
    finally{toggle.disabled=false;}
   };
   actions.append(folders,perms,toggle);root.append(actions);
   const personal=document.createElement("section");personal.id="admin-user-personal";
   personal.className="admin-user-personal";root.append(personal);
   const central=document.createElement("section");central.id="admin-user-central";
   central.className="admin-user-central";
   central.append(document.createTextNode("Consultando ficha de CUSTOMER-SERVICE…"));
   root.append(central);
   // The central CRM can be unavailable independently of gallery moderation.
   try{
    const profile=await api("admin-user-profile",{telegram_id:uid});
    if(state.selectedUid===uid)renderUserProfile(profile);
   }catch(e){
    if(state.selectedUid===uid)central.textContent="La ficha central no está disponible en este momento. "+
      "Las carpetas y el acceso de Web Galerías siguen disponibles.";
   }
  }catch(e){if(state.selectedUid===uid){status.textContent=e.message;notice(e.message);}}
 }
 async function loadUserFolders(uid){
  if(state.selectedUid!==uid)return;
  const root=$("admin-user-personal");root.replaceChildren();
  const heading=document.createElement("h3");heading.textContent="Carpetas personalizadas";root.append(heading);
  const message=document.createElement("p");message.textContent="Cargando carpetas…";root.append(message);
  try{
   const data=await api("admin-user-personal-folders",{telegram_id:uid});
   if(state.selectedUid!==uid)return;
   state.folders=data.folders||[];message.remove();
   if(!state.folders.length){root.append(document.createTextNode("Este usuario no ha creado carpetas."));return;}
   const list=document.createElement("div");list.className="admin-personal-folder-list";root.append(list);
   for(const folder of state.folders){
    const row=document.createElement("div");row.className="admin-personal-folder-row";
    const open=document.createElement("button");open.className="small-btn";
    open.textContent="📁 "+folder.name+" · "+folder.visible+" archivos";
    open.onclick=()=>openUserFolder(uid,folder,root);
    const remove=document.createElement("button");remove.className="small-btn admin-user-access-action";
    remove.textContent="Eliminar carpeta";
    remove.onclick=async()=>{
     if(!confirm("¿Eliminar la carpeta personal «"+folder.name+"» de este usuario?\n"+
       "Solo se borrará la carpeta y sus referencias; los originales de las galerías y Cloudflare NO se borrarán."))return;
     remove.disabled=true;
     try{
      await api("admin-user-personal-delete",{telegram_id:uid,personal_folder:folder.id,
       confirm:"ELIMINAR_CARPETA_PERSONAL"});
      notice("Carpeta personal eliminada.");await loadUserFolders(uid);
     }catch(e){notice(e.message);remove.disabled=false;}
    };
    row.append(open,remove);list.append(row);
   }
  }catch(e){message.textContent="No se pudieron consultar las carpetas: "+e.message;}
 }
 async function openUserFolder(uid,folder,parent){
  if(state.selectedUid!==uid)return;
  const existing=$("admin-user-personal-items");if(existing)existing.remove();
  const box=document.createElement("section");box.id="admin-user-personal-items";
  box.className="admin-user-personal-items";
  const heading=document.createElement("h4");heading.textContent=folder.name;
  const list=document.createElement("div");list.className="admin-personal-items-grid";
  const more=document.createElement("button");more.className="small-btn";
  more.textContent="Más archivos";more.hidden=true;
  box.append(heading,list,more);parent.append(box);
  let cursor=0,loading=false;
  async function read(older=false){
   if(loading)return;loading=true;
   if(!older){cursor=0;list.replaceChildren();}
   try{
    const data=await api("admin-user-personal-items",{
      telegram_id:uid,personal_folder:folder.id,cursor:older?cursor:0});
    if(state.selectedUid!==uid||!box.isConnected)return;
    for(const item of data.items||[]){
     const row=document.createElement("div");row.className="admin-personal-item";
     const thumb=document.createElement("img");thumb.alt=item.kind==="video"?"Video":"Foto";
     if(item.thumb)thumb.src=item.thumb;else thumb.hidden=true;
     const note=document.createElement("small");note.textContent=item.kind==="video"?"Video":"Foto";
     const view=document.createElement("button");view.className="small-btn";view.textContent="Ver";
     view.disabled=!(item.view||item.thumb);
     view.onclick=()=>{const url=item.view||item.thumb;if(url)window.open(url,"_blank","noopener,noreferrer");};
     const take=document.createElement("button");take.className="small-btn";take.textContent="Quitar de carpeta";
     take.onclick=async()=>{
      if(!confirm("¿Quitar este archivo de la carpeta «"+folder.name+"»?\n"+
        "El original seguirá en la galería."))return;
      take.disabled=true;
      try{
       await api("admin-user-personal-remove",{telegram_id:uid,personal_folder:folder.id,
         ids:[item.id],confirm:"QUITAR_DE_CARPETA"});
       notice("Archivo retirado de la carpeta personal.");
       await loadUserFolders(uid);
      }catch(e){notice(e.message);take.disabled=false;}
     };
     const erase=document.createElement("button");erase.className="small-btn admin-user-access-action";
     erase.textContent="Eliminar de toda la web";
     erase.onclick=async()=>{if(await deleteMediaIds([item.id],false))await loadUserFolders(uid);};
     row.append(thumb,note,view,take,erase);list.append(row);
    }
    cursor=data.next||0;more.hidden=!data.next;
    if(!list.children.length)list.append(document.createTextNode("No hay contenido visible en esta carpeta."));
   }catch(e){notice(e.message);}
   finally{loading=false;}
  }
  more.onclick=()=>read(true);
  await read();
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
