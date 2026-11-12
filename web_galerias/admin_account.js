"use strict";
/* Admin-only additions to the actual unchanged customer gallery document.
   This DOES NOT render a second imitation of the galleries or their viewer. */
(()=>{
 if(!ADMIN_ACCOUNT_VIEW)return;
 const $=id=>document.getElementById(id);
 const state={before:null,loading:false,day:null,days:[],generation:0};
 const create=(tag,cls,text)=>{
  const node=document.createElement(tag);
  if(cls)node.className=cls;
  if(text!==undefined)node.textContent=String(text);
  return node;
 };
 const dateLabel=value=>new Date(value+"T12:00:00Z").toLocaleDateString(
  "es-ES",{day:"2-digit",month:"long",year:"numeric",timeZone:"UTC"});
 const library=$("library")?.querySelector(".container");
 if(!library)return;
 const indicator=create("div","admin-account-notice",
   "Vista administrativa · cuenta seleccionada · las descargas no descuentan su cuota");
 library.insertBefore(indicator,library.firstChild);
 const historyButton=create("button","favorites-entry admin-account-history-link");
 historyButton.type="button";historyButton.id="admin-account-history-link";
 historyButton.append(create("span",null,"⇩ Mis descargas"));
 historyButton.append(create("span","admin-account-chevron","Ver historial por fecha ›"));
 $("open-favorites").after(historyButton);
 const history=create("section","screen hidden admin-account-dates");
 history.id="admin-account-download-days";
 history.setAttribute("aria-label","Historial de descargas del usuario seleccionado");
 const header=create("header","topbar album-topbar");
 const h=create("div","album-heading");
 h.append(create("strong",null,"Mis descargas"),
          create("small",null,"Archivos solicitados por fecha"));
 header.append(h);history.append(header);
 const container=create("main","container");
 const quota=create("div","admin-account-quota");
 quota.id="admin-account-download-quota";container.append(quota);
 const disclaimer=create("p","admin-account-disclaimer",
  "El registro contabiliza archivos solicitados para descargar. " +
  "No confirma si Telegram o el iPhone los guardó completamente.");
 container.append(disclaimer);
 const dates=create("div","admin-account-date-grid");dates.id="admin-account-date-grid";
 container.append(dates);
 const more=create("button","load-more hidden","Mostrar fechas anteriores");
 more.type="button";more.id="admin-account-more-dates";
 container.append(more);history.append(container);
 const nav=create("nav","mobile-actions");
 const back=create("button","mobile-action mobile-action-primary","‹ Volver a galerías");
 back.type="button";back.onclick=()=>show("library");
 nav.append(back);history.append(nav);
 document.querySelector("#app").append(history);
 function quotaDisplay(){
  const me=S.me||{};
  const label=me.quota_unlimited?"♾ Descargas ilimitadas":
     me.quota_tier==="linked"?"200 archivos diarios":"50 archivos diarios";
  const number=me.quota_unlimited?"Sin límite diario":
     pretty(me.quota_daily_limit-(me.remaining||0))+" de "+pretty(me.quota_daily_limit)+" utilizadas hoy";
  quota.replaceChildren(create("strong",null,label),create("span",null,number));
 }
 async function loadDates(older=false){
  if(state.loading)return;
  state.loading=true;
  const gen=older?state.generation:++state.generation;
  if(!older){state.before=null;state.days=[];dates.replaceChildren();}
  more.disabled=true;
  try{
   const result=await api("admin-mirror-download-dates",{
    before:older?state.before:null});
   if(gen!==state.generation)return;
   state.days.push(...(result.dates||[]));
   state.before=result.next||null;
   dates.replaceChildren();
   for(const item of state.days){
    const button=create("button","admin-account-date");
    button.type="button";
    button.append(create("strong",null,"📅 "+dateLabel(item.date)));
    button.append(create("small",null,
      pretty(item.files)+" archivos · "+pretty(item.requests)+" solicitudes ›"));
    button.onclick=()=>openDownloadDay(item);
    dates.append(button);
   }
   if(!state.days.length)
    dates.append(create("p","admin-account-no-dates","Aún no se registran descargas."));
   more.classList.toggle("hidden",!state.before);
  }catch(e){notice(e.message);}
  finally{state.loading=false;more.disabled=false;}
 }
 function openDownloadDay(item){
  state.day=item.date;
  openFolder({
   id:0,date:item.date,downloads:true,
   name:"📅 "+dateLabel(item.date),
   count:item.files,
  });
 }
 function showDownloadDays(){
  quotaDisplay();
  show("admin-account-download-days");
  loadDates(false);
 }
 historyButton.onclick=showDownloadDays;
 more.onclick=()=>loadDates(true);
 // The host carries this iframe; users never see these administration helpers.
 window.GalleryAccountAdmin={showDownloadDays,openDownloadDay};
})();
