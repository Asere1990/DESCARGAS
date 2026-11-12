"use strict";
/* Small add-on to Web Galerías: private unread support badge, real-time notice
   inbox and an admin-only mass-notification composer. No source bot polling. */
(()=>{
 const $=id=>document.getElementById(id);
 const pretty=n=>Number(n||0).toLocaleString("es-ES");
 const node=(tag,cls,text)=>{
  const el=document.createElement(tag);
  if(cls)el.className=cls;
  if(text!==undefined)el.textContent=String(text);
  return el;
 };
 const urlValid=value=>{
  try{
   const u=new URL(value);
   return u.protocol==="https:"&&!!u.hostname&&!u.username&&!u.password;
  }catch(_){return false;}
 };
 const LINK=/\[([^\]\r\n]{1,120})\]\((https:\/\/[^\s)]{1,1200})\)|(https:\/\/[^\s<>]{1,1200})/g;
 function richLinks(parent,message){
  parent.replaceChildren();
  const value=String(message||"");
  let cursor=0;
  for(const match of value.matchAll(LINK)){
   if(match.index>cursor)parent.append(document.createTextNode(value.slice(cursor,match.index)));
   const url=match[2]||match[3];
   if(urlValid(url)){
    const a=node("a",null,match[1]||url);
    a.href=url;a.target="_blank";a.rel="noopener noreferrer";
    parent.append(a);
   }else parent.append(document.createTextNode(match[0]));
   cursor=match.index+match[0].length;
  }
  if(cursor<value.length)parent.append(document.createTextNode(value.slice(cursor)));
 }
 const dateText=sec=>{
  const time=Number(sec)*1000;
  return Number.isFinite(time)?new Date(time).toLocaleString("es-US"):"";
 };
 let unreadSupport=0;
 async function refreshSupportUnread(){
  if(!S.me?.allowed||S.me.admin||document.hidden)return;
  const badge=$("support-unread");
  if(!badge)return;
  try{
   const response=await api("chat-unread");
   const n=Math.max(0,Math.min(9999,Number(response.count)||0));
   unreadSupport=n;
   badge.textContent=n>99?"99+":String(n);
   badge.classList.toggle("hidden",n<1);
   badge.setAttribute("aria-label",
     n===1?"1 mensaje de soporte sin leer":n+" mensajes de soporte sin leer");
   $("open-support-chat").setAttribute("aria-label",
     n?"Abrir chat con soporte · "+n+" mensajes sin leer":"Abrir chat con soporte");
  }catch(_){/* The regular support link must never become unavailable. */}
 }
 let unreadNotices=0;
 let noticesCountLoaded=false;
 let feedNext=null;
 let feedBusy=false;
 function updateNoticeCount(n){
  const label=$("notices-unread"),trigger=$("notices-open");
  if(!label||!trigger)return;
  unreadNotices=Math.max(0,Math.min(9999,Number(n)||0));
  label.textContent=unreadNotices?
     (unreadNotices>99?"99+":pretty(unreadNotices))+" nuevos ›":
     "Revisar notificaciones ›";
  trigger.classList.toggle("has-unread",unreadNotices>0);
 }
 async function refreshNoticeCount(){
  if(!S.me?.allowed||document.hidden)return;
  try{
   const reply=await api("notices-count");
   const last=unreadNotices;
   updateNoticeCount(reply.unread);
   if(noticesCountLoaded&&unreadNotices>last&&
       $("notices-screen").classList.contains("hidden"))
    notice("📣 Tienes un aviso nuevo de Web Galerías.");
   noticesCountLoaded=true;
  }catch(_){/* Optional notifications do not break galleries. */}
 }
 function noticeCard(msg){
  const card=node("article","notice-card"+(msg.read?"":" unread"));
  const header=node("div","notice-head");
  header.append(node("strong",null,"📣 Aviso de Web Galerías"),
                node("small",null,dateText(msg.created)));
  card.append(header);
  if(msg.media){
   if(msg.media_kind==="video"){
    const video=node("video","notice-media");
    video.src=msg.media;video.controls=true;video.playsInline=true;
    video.preload="metadata";card.append(video);
   }else{
    const image=node("img","notice-media");
    image.src=msg.media;image.alt="Multimedia del aviso";
    image.loading="lazy";card.append(image);
   }
  }
  const body=node("p","notice-body");
  richLinks(body,msg.message);card.append(body);
  if(!msg.read){
   const seen=node("button","notice-read-btn","✓ Marcar como leído");
   seen.type="button";
   seen.onclick=async()=>{
    if(seen.disabled)return;seen.disabled=true;
    try{
     await api("notices-mark-read",{ids:[msg.id]});
     seen.remove();card.classList.remove("unread");
     await refreshNoticeCount();
    }catch(e){seen.disabled=false;notice(e.message);}
   };
   card.append(seen);
  }
  return card;
 }
 async function loadNoticeFeed(more=false){
  if(feedBusy)return;
  feedBusy=true;
  const holder=$("notices-list"),button=$("notices-more");
  if(!more){feedNext=null;holder.replaceChildren();}
  button.disabled=true;
  try{
   const data=await api("notices-list",{cursor:more?feedNext:0});
   for(const record of data.notices||[])holder.append(noticeCard(record));
   feedNext=data.next||null;
   button.classList.toggle("hidden",!feedNext);
   if(!holder.children.length)
    holder.append(node("p","muted","Todavía no hay avisos para tu cuenta."));
  }catch(e){notice("No se pudieron cargar los avisos: "+e.message);}
  finally{feedBusy=false;button.disabled=false;}
 }
 if($("notices-open")){
  $("notices-open").onclick=()=>{show("notices-screen");loadNoticeFeed(false);};
  $("notices-back").onclick=()=>{show("library");refreshNoticeCount();};
  $("notices-more").onclick=()=>loadNoticeFeed(true);
 }
 // Low-rate, visible-page polling never blocks gallery first paint or scan.
 const refreshAll=()=>{void refreshSupportUnread();void refreshNoticeCount();};
 setTimeout(refreshAll,1200);
 setInterval(refreshAll,45000);
 // Refresh as soon as the signed user enters/returns to the gallery, not
 // only on the periodic cadence; this does not delay gallery first paint.
 if(typeof MutationObserver!=="undefined"){
  const library=$("library");
  const observer=new MutationObserver(()=>{
   if(library&&!library.classList.contains("hidden")&&S.me?.allowed)refreshAll();
  });
  if(library)observer.observe(library,{attributes:true,attributeFilter:["class"]});
 }
 document.addEventListener("visibilitychange",()=>{
  if(!document.hidden)setTimeout(refreshAll,350);
 });
 window.addEventListener("pageshow",event=>{
  if(event.persisted)setTimeout(refreshAll,350);
 });

 const admin={users:0,busy:false,previewUrl:null};
 const message=$("admin-notice-message");
 const input=$("admin-notice-file");
 const preview=$("admin-notice-preview-text");
 const attachment=$("admin-notice-preview-media");
 if(!message||!input||!preview)return;
 function selectedFile(){
  return input.files?.length?input.files[0]:null;
 }
 function resolveKind(file){
  if(!file)return null;
  const mime=file.type||(/\.mov$/i.test(file.name)?"video/quicktime":
     /\.mp4$/i.test(file.name)?"video/mp4":"");
  if(mime==="image/gif")return {kind:"gif",mime,max:25*1024*1024};
  if(["video/mp4","video/quicktime"].includes(mime))
   return {kind:"video",mime,max:60*1024*1024};
  if(["image/jpeg","image/png","image/webp"].includes(mime))
   return {kind:"photo",mime,max:25*1024*1024};
  return null;
 }
 function previewAdminNotice(){
  richLinks(preview,message.value||"Escribe un mensaje para visualizarlo.");
  attachment.replaceChildren();
  if(admin.previewUrl)URL.revokeObjectURL(admin.previewUrl);
  admin.previewUrl=null;
  const file=selectedFile();if(!file)return;
  const info=resolveKind(file);if(!info)return;
  admin.previewUrl=URL.createObjectURL(file);
  const media=node(info.kind==="video"?"video":"img");
  media.src=admin.previewUrl;
  if(info.kind==="video"){media.controls=true;media.playsInline=true;}
  else media.alt="Vista previa del aviso";
  attachment.append(media);
 }
 message.addEventListener("input",previewAdminNotice);
 input.addEventListener("change",previewAdminNotice);
 function localStatus(text){$("admin-notice-status").textContent=text;}
 async function refreshAdminNotices(){
  if(!S.me?.admin)return;
  try{
   const [audience,history]=await Promise.all([
    api("admin-notices-audience"),api("admin-notices-list")]);
   admin.users=Number(audience.users)||0;
   $("admin-notices-audience").textContent=
    pretty(admin.users)+" usuarios registrados recibirán el aviso en la web.";
   const list=$("admin-notices-history");list.replaceChildren();
   for(const n of history.notices||[]){
    const card=node("div","admin-notice-history-card");
    card.append(node("strong",null,dateText(n.created)+" · "+(n.media_kind||"texto")));
    card.append(node("small",null,
     pretty(n.audience)+" destinatarios · "+
     pretty(n.delivered)+" enviados por Telegram · "+
     pretty(n.pending)+" pendientes · "+
     pretty(n.unreachable)+" no accesibles en Telegram · "+
     pretty(n.read_in_web)+" leídos en web"));
    const body=node("small",null,String(n.message||"").slice(0,200));
    card.append(body);list.append(card);
   }
   if(!list.children.length)list.append(node("p","muted","Todavía no hay notificaciones publicadas."));
  }catch(e){
   $("admin-notices-audience").textContent="No se pudo consultar el público: "+e.message;
  }
 }
 const send=$("admin-notice-send");
 send.onclick=async()=>{
  if(admin.busy||!S.me?.admin)return;
  const file=selectedFile();
  const meta=resolveKind(file);
  const text=message.value.trim();
  if(file&&(!meta||file.size>meta.max||file.size<16))
   return localStatus("Adjunto inválido o superior al tamaño permitido.");
  if(!text&&!file)return localStatus("Añade un texto o archivo.");
  if(text.length>(file?800:3000))
   return localStatus("El texto es demasiado largo para este aviso.");
  if(!admin.users)return localStatus("No hay destinatarios. Actualiza el listado.");
  const description=pretty(admin.users)+" usuarios registrados.\n"+
   "El aviso aparecerá en la web inmediatamente. El envío de Telegram "+
   "continuará gradualmente y solo llegará a cuentas donde el bot pueda escribir.\n"+
   "¿Confirmas ENVIAR A TODOS?";
  if(!confirm(description))return;
  admin.busy=true;send.disabled=true;
  try{
   let uploadToken=null;
   if(file){
    localStatus("Subiendo el archivo multimedia a Cloudflare…");
    const params=new URLSearchParams({kind:meta.kind,filename:file.name});
    const headers={"Content-Type":meta.mime};
    if(tg?.initData)headers["X-Telegram-Init-Data"]=tg.initData;
    else headers["X-Gallery-Web"]="1";
    const res=await fetch("/web-galerias/admin/notice-upload?"+params.toString(),{
     method:"POST",credentials:"same-origin",cache:"no-store",headers,body:file});
    if(!res.ok)throw Error((await res.text()).slice(0,220)||"No se pudo subir el archivo.");
    const uploaded=await res.json();
    uploadToken=uploaded.upload_token;
   }
   localStatus("Publicando para todos los usuarios…");
   const result=await api("admin-notices-publish",{
    message:text,upload_token:uploadToken,confirm:"ENVIAR_A_TODOS"});
   localStatus("✓ Aviso publicado para "+pretty(result.audience)+
    " usuarios. El envío privado de Telegram continuará en segundo plano.");
   message.value="";input.value="";previewAdminNotice();
   await refreshAdminNotices();
  }catch(e){localStatus("No se pudo publicar: "+e.message);}
  finally{admin.busy=false;send.disabled=false;}
 };
 $("admin-notices-refresh").onclick=refreshAdminNotices;
 document.querySelector('.admin-tabs [data-tab="avisos"]')?.addEventListener("click",()=>{
  setTimeout(refreshAdminNotices,0);
 });
 window.GalleryBroadcast={refreshAdminNotices,refreshNoticeCount,refreshSupportUnread};
})();
