"use strict";
/* A separate read-only customer-view surface for the administrator.
   Never switches the Telegram identity, and never changes the customer's data. */
(()=>{
 const $=id=>document.getElementById(id);
 const root=$("admin-mirror");
 if(!root)return;
 const area=$("admin-mirror-body");
 const state={uid:0,name:"Usuario",section:"folders",view:"list",folder:null,day:null,
              kind:"all",query:"",items:[],next:null,busy:false,session:0,viewer:0,info:null};
 const fmt=n=>Number(n||0).toLocaleString("es-ES");
 const mk=(tag,cls,text)=>{
  const e=document.createElement(tag);
  if(cls)e.className=cls;
  if(text!==undefined)e.textContent=String(text);
  return e;
 };
 function btn(text,fn,cls="admin-mirror-btn"){
  const e=mk("button",cls,text);e.type="button";e.onclick=fn;return e;
 }
 function textMessage(text){
  const p=mk("p","admin-mirror-message",text);area.append(p);return p;
 }
 function setTitle(){
  $("admin-mirror-name").textContent=state.name;
  const suffix=state.section==="folders"?"Mis carpetas":state.section==="favorites"?"Mis favoritos":"Mis descargas";
  $("admin-mirror-subtitle").textContent=suffix+" · "+state.uid;
  for(const name of ["folders","favorites","downloads"]){
   const tab=$("admin-mirror-tab-"+name);
   tab.classList.toggle("active",name===state.section);
   tab.setAttribute("aria-current",name===state.section?"page":"false");
  }
 }
 function clear(nextView="list"){
  state.session++;state.busy=false;state.view=nextView;
  state.items=[];state.next=null;state.kind="all";state.query="";
  area.replaceChildren();area.scrollTop=0;setTitle();
 }
 function close(){
  state.session++;
  root.classList.add("hidden");
  document.documentElement.classList.remove("admin-mirror-open");
  area.replaceChildren();state.uid=0;state.info=null;
 }
 function back(){
  if(state.view==="viewer"){state.view="grid";paintGrid();return;}
  if(state.view==="grid"||state.view==="day"){
   if(state.section==="folders"){state.folder=null;listFolders();}
   else if(state.section==="downloads"){state.day=null;listDates();}
   else switchSection("favorites");
   return;
  }
  close();
 }
 async function open(uid,section="folders"){
  if(!Number.isSafeInteger(Number(uid))||Number(uid)<=0)return;
  state.uid=Number(uid);state.name="Usuario";state.info=null;
  state.section=["folders","favorites","downloads"].includes(section)?section:"folders";
  state.folder=null;state.day=null;
  root.classList.remove("hidden");
  document.documentElement.classList.add("admin-mirror-open");
  clear();textMessage("Abriendo cuenta del usuario…");
  try{
   const generation=state.session;
   const info=await api("admin-mirror-overview",{telegram_id:state.uid});
   if(generation!==state.session)return;
   state.info=info;state.name=info.name||"Usuario";
   setTitle();
   switchSection(state.section);
  }catch(e){area.replaceChildren();textMessage("No se pudo abrir la vista: "+e.message);}
 }
 function switchSection(section){
  if(!state.uid)return;
  state.section=section;state.folder=null;state.day=null;
  clear();
  if(section==="folders")listFolders();
  else if(section==="favorites")openGrid("favorites");
  else listDates();
 }
 function summary(title,subtitle=""){
  const section=mk("div","admin-mirror-summary");
  section.append(mk("h2",null,title));
  if(subtitle)section.append(mk("p",null,subtitle));
  area.append(section);
  return section;
 }
 function placeFolderCover(parent,cover){
  const img=mk("div","admin-mirror-cover");
  if(cover){
   const photo=mk("img");photo.loading="lazy";photo.alt="";
   photo.src=cover;
   photo.onerror=()=>{photo.remove();img.textContent="📁";};
   img.append(photo);
  }else img.textContent="📁";
  parent.append(img);
 }
 async function listFolders(){
  clear("list");
  summary("Mis carpetas","Vista del contenido que el usuario organizó.");
  const holder=mk("div","admin-mirror-folder-grid");area.append(holder);
  const note=textMessage("Cargando carpetas…");
  const gen=state.session, uid=state.uid;
  try{
   const data=await api("admin-mirror-folders",{telegram_id:uid});
   if(gen!==state.session)return;
   note.remove();holder.replaceChildren();
   if(!data.folders?.length){textMessage("Este usuario todavía no creó carpetas.");return;}
   for(const f of data.folders){
    const card=mk("div","admin-mirror-folder-card");
    const enter=btn("","", "admin-mirror-folder-open");
    enter.onclick=()=>{state.folder=f;openGrid("folder");};
    placeFolderCover(enter,f.cover);
    const name=mk("strong",null,f.name||"Carpeta");
    const count=mk("small",null,fmt(f.count)+" archivos");
    enter.append(name,count);
    card.append(enter);
    // Keep the existing reversible admin folder-management functionality.
    const erase=btn("Eliminar carpeta",async()=>{
     if(!confirm("¿Eliminar la carpeta «"+f.name+"»? Sus archivos originales se conservarán."))return;
     erase.disabled=true;
     try{
      await api("admin-user-personal-delete",{
       telegram_id:uid,personal_folder:f.id,confirm:"ELIMINAR_CARPETA_PERSONAL"});
      notice("Carpeta eliminada.");listFolders();
     }catch(e){notice(e.message);erase.disabled=false;}
    },"admin-mirror-danger");
    card.append(erase);holder.append(card);
   }
  }catch(e){if(gen===state.session)note.textContent="No se pudieron cargar las carpetas: "+e.message;}
 }
 function filterBar(){
  const controls=mk("div","admin-mirror-filterbar");
  const search=mk("input","admin-mirror-search");
  search.type="search";search.placeholder="Buscar en esta colección";search.maxLength=100;
  const kinds=mk("select","admin-mirror-kind");
  for(const [v,t] of [["all","Mostrar todo"],["photo","Fotos"],["video","Videos"]]){
   const opt=mk("option",null,t);opt.value=v;kinds.append(opt);
  }
  kinds.value=state.kind;search.value=state.query;
  let timer;
  search.oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>{
   state.query=search.value.trim();loadGrid(false);
  },300);};
  kinds.onchange=()=>{state.kind=kinds.value;loadGrid(false);};
  controls.append(search,kinds);area.append(controls);
 }
 function openGrid(scope){
  state.view="grid";state.scope=scope;
  state.kind="all";state.query="";state.items=[];state.next=null;
  state.session++;area.replaceChildren();area.scrollTop=0;
  setTitle();
  summary(scope==="folder"?(state.folder?.name||"Carpeta"):"Mis favoritos",
          scope==="folder"?"Archivos guardados por este usuario.":"Los elementos que este usuario marcó con ♥.");
  filterBar();
  const grid=mk("div","admin-mirror-media-grid");grid.id="admin-mirror-media-grid";
  area.append(grid);
  const more=btn("Mostrar más archivos",()=>loadGrid(true),"admin-mirror-load-more");
  more.id="admin-mirror-more";more.hidden=true;area.append(more);
  loadGrid(false);
 }
 async function loadGrid(more){
  if(!state.uid||state.busy)return;
  state.busy=true;
  const cursor=more?state.next:0;
  if(!more){state.items=[];state.next=null;$("admin-mirror-media-grid")?.replaceChildren();}
  const gen=state.session;
  const op=state.scope==="folder"?"admin-mirror-folder-items":"admin-mirror-favorites";
  const req={telegram_id:state.uid,kind:state.kind,query:state.query,cursor};
  if(state.scope==="folder")req.personal_folder=state.folder.id;
  const moreBtn=$("admin-mirror-more");
  if(moreBtn)moreBtn.disabled=true;
  try{
   const result=await api(op,req);
   if(gen!==state.session)return;
   state.items.push(...(result.items||[]));
   state.next=result.next||null;
   paintGrid();
  }catch(e){if(gen===state.session)notice(e.message);}
  finally{if(gen===state.session){state.busy=false;if(moreBtn)moreBtn.disabled=false;}}
 }
 function tile(item,index){
  const b=btn("","", "admin-mirror-media-tile");
  const caption=item.kind==="video"?"▶":"";
  if(item.thumb){
   const img=mk("img");img.src=item.thumb;img.loading="lazy";
   img.alt=item.kind==="video"?"Video":"Foto";
   img.onerror=()=>{img.remove();b.append(mk("span","admin-mirror-media-placeholder","▧"));};
   b.append(img);
  }else b.append(mk("span","admin-mirror-media-placeholder",item.kind==="video"?"▶":"▧"));
  if(caption)b.append(mk("span","admin-mirror-video-mark",caption));
  if(!item.available)b.classList.add("unavailable");
  b.setAttribute("aria-label",item.filename||"Ver archivo");
  b.onclick=()=>showViewer(index);
  return b;
 }
 function paintGrid(){
  const grid=$("admin-mirror-media-grid");
  if(!grid){
   // The viewer has replaced the grid; go back to the same loaded items.
   if(state.view==="viewer"){state.view="grid";}
   const snap=state.items.slice(),next=state.next;
   openGrid(state.scope);state.items=snap;state.next=next;
   return;
  }
  const more=$("admin-mirror-more");
  grid.replaceChildren();
  state.items.forEach((item,i)=>grid.append(tile(item,i)));
  if(!state.items.length&&!state.busy)
   grid.append(mk("p","admin-mirror-message","No hay archivos en esta vista."));
  if(more)more.hidden=!state.next;
 }
 function showViewer(index){
  if(!state.items[index])return;
  state.viewer=index;state.view="viewer";
  const dialog=$("admin-mirror-viewer");
  dialog.classList.remove("hidden");
  paintViewer();
 }
 function closeViewer(){
  $("admin-mirror-viewer").classList.add("hidden");
  $("admin-mirror-viewer-stage").replaceChildren();
  state.view="grid";
 }
 function moveViewer(step){
  const next=state.viewer+step;
  if(next<0||next>=state.items.length)return;
  state.viewer=next;paintViewer();
 }
 function paintViewer(){
  const item=state.items[state.viewer],stage=$("admin-mirror-viewer-stage");
  if(!item)return closeViewer();
  stage.replaceChildren();
  $("admin-mirror-viewer-counter").textContent=(state.viewer+1)+" / "+state.items.length;
  $("admin-mirror-viewer-caption").textContent=item.filename||"Archivo";
  $("admin-mirror-viewer-prev").disabled=state.viewer===0;
  $("admin-mirror-viewer-next").disabled=state.viewer===state.items.length-1;
  const url=item.view||item.thumb;
  if(!url){
   stage.append(mk("p","admin-mirror-message","Este archivo ya no está disponible."));
   return;
  }
  if(item.kind==="video"&&item.view){
   const v=mk("video");v.src=item.view;v.controls=true;v.playsInline=true;
   v.preload="metadata";v.setAttribute("webkit-playsinline","");
   stage.append(v);
  }else{
   const img=mk("img");img.src=url;img.alt=item.filename||"Foto";
   stage.append(img);
  }
 }
 function planBlock(){
  const d=state.info||{};
  const panel=mk("div","admin-mirror-plan");
  const label=d.unlimited?"♾ Descargas ilimitadas":
      d.tier==="linked"?"🔗 200 archivos diarios":"50 archivos diarios";
  panel.append(mk("h2",null,label));
  const daily=d.unlimited?"Sin límite diario":fmt(d.used)+" de "+fmt(d.daily_limit)+" contabilizadas hoy";
  panel.append(mk("p",null,daily));
  if(!d.unlimited){
   const bar=mk("div","admin-mirror-progress");
   const fill=mk("div");fill.style.width=Math.min(100,100*(d.used||0)/(d.daily_limit||50))+"%";
   bar.append(fill);panel.append(bar);
  }
  panel.append(mk("p","admin-mirror-plan-note",
     fmt(d.today_requested)+" archivos solicitados hoy · "+
     fmt(d.total_requested)+" en el historial · "+fmt(d.batches)+" solicitudes"));
  area.append(panel);
  const explain=mk("p","admin-mirror-hint",
    "El historial registra archivos descontados o solicitados para descargar. "+
    "No confirma que iPhone/Telegram los haya guardado completamente en el carrete.");
  area.append(explain);
 }
 async function listDates(more=false){
  if(!more){clear("list");summary("Mis descargas","Historial organizado por fecha.");planBlock();}
  const gen=state.session, uid=state.uid;
  let list=$("admin-mirror-date-list");
  if(!list){list=mk("div","admin-mirror-date-list");list.id="admin-mirror-date-list";area.append(list);}
  if(!more)list.replaceChildren();
  const foot=$("admin-mirror-date-more");if(foot)foot.remove();
  const empty=textMessage("Consultando fechas…");
  try{
   const data=await api("admin-mirror-download-dates",{
     telegram_id:uid,before:more?state.next:null});
   if(gen!==state.session)return;
   empty.remove();
   for(const day of data.dates||[]){
    const date=new Date(day.date+"T12:00:00Z").toLocaleDateString("es-ES",{
      day:"2-digit",month:"long",year:"numeric",timeZone:"UTC"});
    const row=btn("","", "admin-mirror-date-row");
    row.append(mk("strong",null,"📅 "+date));
    row.append(mk("span",null,fmt(day.files)+" archivos · "+fmt(day.requests)+" solicitudes ›"));
    row.onclick=()=>{state.day=day;openDay();};list.append(row);
   }
   state.next=data.next||null;
   if(state.next){
    const button=btn("Ver fechas anteriores",()=>listDates(true),"admin-mirror-load-more");
    button.id="admin-mirror-date-more";area.append(button);
   }
   if(!list.children.length)textMessage("Todavía no hay descargas registradas.");
  }catch(e){if(gen===state.session)empty.textContent="No se pudo cargar el historial: "+e.message;}
 }
 function openDay(){
  clear("day");
  const date=state.day?.date;
  const readable=date?new Date(date+"T12:00:00Z").toLocaleDateString("es-ES",{
   day:"2-digit",month:"long",year:"numeric",timeZone:"UTC"}):"Fecha";
  summary("📅 "+readable,fmt(state.day?.files)+" archivos contabilizados");
  const grid=mk("div","admin-mirror-media-grid");grid.id="admin-mirror-date-grid";
  area.append(grid);
  const more=btn("Mostrar más archivos",()=>loadDay(true),"admin-mirror-load-more");
  more.id="admin-mirror-day-more";more.hidden=true;area.append(more);
  state.items=[];state.next=0;loadDay(false);
 }
 async function loadDay(more){
  if(state.busy)return;state.busy=true;
  const gen=state.session,uid=state.uid;
  const offset=more?state.next:0;
  const moreBtn=$("admin-mirror-day-more");
  if(moreBtn)moreBtn.disabled=true;
  try{
   const d=await api("admin-mirror-download-items",{
     telegram_id:uid,date:state.day.date,offset});
   if(gen!==state.session)return;
   if(!more)state.items=[];
   state.items.push(...(d.items||[]));state.next=d.next;
   const grid=$("admin-mirror-date-grid");
   grid.replaceChildren();
   state.items.forEach((item,i)=>grid.append(tile(item,i)));
   if(!state.items.length)grid.append(mk("p","admin-mirror-message","Sin archivos registrados."));
   if(moreBtn)moreBtn.hidden=state.next===null;
  }catch(e){if(gen===state.session)notice(e.message);}
  finally{if(gen===state.session){state.busy=false;if(moreBtn)moreBtn.disabled=false;}}
 }
 $("admin-mirror-close").onclick=close;
 $("admin-mirror-back").onclick=()=>{
  if(!$("admin-mirror-viewer").classList.contains("hidden"))return closeViewer();
  back();
 };
 $("admin-mirror-viewer-close").onclick=closeViewer;
 $("admin-mirror-viewer-prev").onclick=()=>moveViewer(-1);
 $("admin-mirror-viewer-next").onclick=()=>moveViewer(1);
 for(const s of ["folders","favorites","downloads"])
  $("admin-mirror-tab-"+s).onclick=()=>{
   if(!$("admin-mirror-viewer").classList.contains("hidden"))closeViewer();
   switchSection(s);
  };
 document.addEventListener("keydown",event=>{
  if(root.classList.contains("hidden"))return;
  if(event.key==="Escape"){
   if(!$("admin-mirror-viewer").classList.contains("hidden"))closeViewer();
   else back();
  }else if(!$("admin-mirror-viewer").classList.contains("hidden")){
   if(event.key==="ArrowLeft")moveViewer(-1);
   if(event.key==="ArrowRight")moveViewer(1);
  }
 });
 window.GalleryUserMirror={open,close};
})();
