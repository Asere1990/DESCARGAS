"use strict";
/* Optional bulk transfer UI. Never modifies login, DM links or admin chat flows. */
(()=>{
  const byId=id=>document.getElementById(id);
  const has=id=>Boolean(byId(id));
  if(!has("bulk-open")||!has("bulk-user-panel"))return;
  const fmt=n=>Number(n||0).toLocaleString("es-ES");
  const names={
    photos:"Fotos",videos:"Videos",gifs:"GIF",
    photo_files:"Archivos de fotos",video_files:"Archivos de videos"
  };
  const kinds=Object.keys(names);
  const state={job:null,poll:null,loading:false,controls:{},admin:false,open:false,
               selectedId:null,jobs:[],lastRun:null};
  const adminMode=new URLSearchParams(location.search).get("mode")==="bulk-admin";
  function say(txt){byId("bulk-source-status").textContent=txt;}
  function mk(tag,cls,content){
    const el=document.createElement(tag);
    if(cls)el.className=cls;
    if(content!==undefined)el.textContent=String(content);
    return el;
  }
  function selectValues(id,groups,placeholder){
    const select=byId(id),previous=select.value;
    select.replaceChildren();
    const initial=mk("option","",placeholder);initial.value="";
    select.append(initial);
    for(const g of groups){
      const opt=mk("option","",g.title+" · "+g.kind);
      opt.value=String(g.id);
      select.append(opt);
    }
    if([...select.options].some(o=>o.value===previous))select.value=previous;
  }
  async function loadSelectors(){
    if(state.loading)return;
    state.loading=true;
    say("Consultando los grupos y canales disponibles…");
    try{
      const [source,destination]=await Promise.all([
        api("bulk-origins"),api("bulk-destinations")]);
      selectValues("bulk-source",source.groups||[],"Selecciona un origen");
      selectValues("bulk-target",destination.groups||[],"Selecciona un destino");
      say((source.groups||[]).length+" orígenes y "+
          (destination.groups||[]).length+" destinos disponibles.");
      if(!(destination.groups||[]).length){
        say("Agrega el bot como administrador de tu grupo destino y envía allí "+
            "un mensaje para que aparezca en el selector.");
      }
    }catch(error){say(error.message);}
    finally{state.loading=false;}
  }
  function choiceCard(kind,total,selected){
    const card=mk("div","bulk-range-card");
    const headline=mk("div","bulk-range-head");
    const n=mk("span","",names[kind]);
    const amount=mk("span","",fmt(total)+" disponibles");
    headline.append(n,amount);card.append(headline);
    const values=mk("div","bulk-range-values");
    const fromText=mk("span"),toText=mk("span");
    values.append(fromText,toText);
    const sliders=mk("div","bulk-range-inputs");
    const start=mk("input"),end=mk("input");
    for(const item of [start,end]){
      item.type="range";item.min="0";item.max=String(total);item.step="1";
      item.disabled=!total;sliders.append(item);
    }
    const numbers=mk("div","bulk-range-number");
    const left=mk("input"),right=mk("input");
    for(const input of [left,right]){
      input.type="number";input.min="0";input.max=String(total);input.step="1";
      input.inputMode="numeric";input.disabled=!total;
      numbers.append(input);
    }
    const valid=Array.isArray(selected)&&selected.length===2?
      selected:[0,total];
    let pair=[Math.max(0,Math.min(total,Number(valid[0])||0)),
              Math.max(0,Math.min(total,Number(valid[1])||0))];
    pair[1]=Math.max(pair[0],pair[1]);
    function sync(){
      start.value=left.value=String(pair[0]);
      end.value=right.value=String(pair[1]);
      fromText.textContent="Desde: "+fmt(pair[0]);
      toText.textContent="Hasta: "+fmt(pair[1]);
    }
    function update(index,raw){
      const n=Number(raw);
      if(!Number.isSafeInteger(n))return;
      pair[index]=Math.min(total,Math.max(0,n));
      if(pair[0]>pair[1]){
        pair[index===0?1:0]=pair[index];
      }
      sync();
    }
    start.addEventListener("input",()=>update(0,start.value));
    end.addEventListener("input",()=>update(1,end.value));
    left.addEventListener("change",()=>update(0,left.value));
    right.addEventListener("change",()=>update(1,right.value));
    sync();card.append(values,sliders,numbers);
    state.controls[kind]=()=>[pair[0],pair[1]];
    return card;
  }
  function ranges(counts,selected){
    const root=byId("bulk-range-list");root.replaceChildren();
    state.controls={};
    for(const kind of kinds){
      const total=Number(counts?.[kind]||0);
      root.append(choiceCard(kind,total,selected?.[kind]));
    }
  }
  function chosen(){
    const out={};
    for(const kind of kinds)out[kind]=state.controls[kind]();
    return out;
  }
  function render(job){
    state.job=job||null;
    const scanning=job?.status==="scanning"||job?.status==="waiting_scan";
    const ready=job&&["ready","paused","finished","error"].includes(job.status);
    const running=job&&["publishing","waiting_send"].includes(job.status);
    byId("bulk-scan").disabled=Boolean(job)||state.jobs.length>=20;
    byId("bulk-source").disabled=Boolean(job);
    byId("bulk-target").disabled=Boolean(job);
    byId("bulk-pause").disabled=!job||!["scanning","publishing","waiting_scan","waiting_send"].includes(job.status);
    byId("bulk-resume").disabled=!job||!["paused","paused_scan","finished","ready"].includes(job.status);
    byId("bulk-disconnect").disabled=!job;
    byId("bulk-ranges").hidden=!ready;
    if(job&&ready){
      if(!Object.keys(state.controls).length||state.lastRun!==job.run_no){
        ranges(job.counts,job.ranges);state.lastRun=job.run_no;
      }
      byId("bulk-start").textContent=job.status==="ready"
        ?"Comenzar transferencia":"Iniciar nueva selección";
      const ct=byId("bulk-continue");
      if(ct)ct.hidden=job.status!=="paused"&&job.status!=="paused_scan";
    }
    if(!job){
      byId("bulk-progress-bar").style.width="0%";
      byId("bulk-progress-text").textContent="Sin transferencia activa.";
      return;
    }
    const total=Number(job.selected||0),done=Number(job.published||0),
          skipped=Number(job.skipped||0);
    const pct=job.status==="finished"?100:(total?Math.min(100,(done+skipped)/total*100):0);
    byId("bulk-progress-bar").style.width=pct.toFixed(1)+"%";
    const stages={
      scanning:"Escaneando el historial",waiting_scan:"Esperando a Telegram para escanear",
      ready:"Escaneo terminado. Selecciona tus rangos.",
      publishing:"Transfiriendo",waiting_send:"Esperando a Telegram para transferir",
      paused:"Transferencia pausada",paused_scan:"Escaneo pausado",
      finished:"Transferencia completada",error:"Requiere revisión",
      review:"Envío pendiente de revisión"
    };
    const name=stages[job.status]||job.status;
    const extra=scanning?" · "+fmt(job.scan_count)+" archivos identificados":
      total?" · "+fmt(done)+" publicados, "+fmt(skipped)+" omitidos de "+fmt(total):"";
    byId("bulk-progress-text").textContent=name+extra+
      (job.error?" · "+job.error:"");
  }
  async function refresh(){
    if(state.selectedId==="new"){render(null);return;}
    const result=await api("bulk-status",state.selectedId?{id:state.selectedId}:{});
    render(result.job);
  }
  async function listJobs(requested){
    const response=await api("bulk-list");
    state.jobs=response.jobs||[];
    const menu=byId("bulk-job-select");
    menu.replaceChildren();
    const create=mk("option","",
      "＋ Nueva transferencia ("+fmt(state.jobs.length)+"/20)");
    create.value="new";menu.append(create);
    for(const job of state.jobs){
      const opt=mk("option","",job.source+" → "+job.destination+" · "+job.status);
      opt.value=job.id;menu.append(opt);
    }
    let selected=requested||state.selectedId;
    if(!selected||(selected!=="new"&&!state.jobs.some(x=>x.id===selected))){
      selected=state.jobs[0]?.id||"new";
    }
    state.selectedId=selected;menu.value=selected;
    state.lastRun=null;state.controls={};
    await refresh();
    if(selected==="new"&&state.jobs.length>=20){
      say("Tienes 20 transferencias configuradas. Desconecta una para agregar otra.");
    }
  }
  async function open(){
    state.open=!state.open;
    byId("bulk-user-panel").hidden=!state.open;
    if(!state.open){clearInterval(state.poll);state.poll=null;return;}
    await loadSelectors();
    try{await listJobs();}catch(error){say(error.message);}
    clearInterval(state.poll);
    state.poll=setInterval(()=>{if(state.open)refresh().catch(()=>{});},4500);
  }
  async function scan(){
    const source=Number(byId("bulk-source").value);
    const destination=Number(byId("bulk-target").value);
    if(!Number.isSafeInteger(source)||!Number.isSafeInteger(destination)||
       source>=0||destination>=0){
      say("Selecciona el origen y el destino.");return;
    }
    const button=byId("bulk-scan");button.disabled=true;
    say("Preparando el escaneo del grupo…");
    try{
      const created=await api("bulk-create",{source,destination});
      await listJobs(created.id);
      say("Escaneo iniciado. Se mostrarán las cantidades antes de transferir.");
    }catch(error){say(error.message);}
    finally{button.disabled=false;}
  }
  async function begin(){
    if(!state.job)return;
    const selected=chosen();
    const count=Object.values(selected).reduce((n,[a,b])=>n+b-a,0);
    if(!count){say("Selecciona por lo menos un archivo.");return;}
    if(state.job.run_no>0&&!window.confirm(
      "Se iniciará una nueva selección de "+fmt(count)+
      " archivos. Puede repetir publicaciones anteriores. ¿Continuar?"))return;
    const button=byId("bulk-start");button.disabled=true;
    try{
      await api("bulk-begin",{id:state.job.id,ranges:selected});
      state.lastRun=null;
      await refresh();
      say("Transferencia solicitada.");
    }catch(error){say(error.message);}
    finally{button.disabled=false;}
  }
  async function pause(){
    if(!state.job)return;
    try{
      await api("bulk-pause",{id:state.job.id});
      await refresh();
      say("Pausa solicitada. El envío en curso terminará de forma segura.");
    }catch(error){say(error.message);}
  }
  async function resume(){
    if(!state.job)return;
    try{
      await api("bulk-resume",{id:state.job.id});
      await refresh();
      say("Reanudando desde el último avance confirmado.");
    }catch(error){say(error.message);}
  }
  async function disconnect(){
    if(!state.job)return;
    if(!window.confirm("¿Desconectar este grupo y finalizar la transferencia? "+
      "Todo lo que ya se publicó permanecerá en Telegram."))return;
    try{
      await api("bulk-disconnect",{id:state.job.id});
      state.lastRun=null;state.controls={};
      await listJobs("new");
      await loadSelectors();
      say("Transferencia desconectada. Puedes configurar otro destino.");
    }catch(error){say(error.message);}
  }
  async function adminJobs(){
    const box=byId("bulk-admin-jobs");box.replaceChildren();
    const response=await api("bulk-admin",{operation:"status"});
    state.admin=true;
    for(const row of response.jobs||[]){
      const card=mk("article","bulk-admin-record");
      card.append(mk("strong","",row.source+" → "+row.destination));
      card.append(mk("p","",
        "Usuario "+row.uid+" · "+row.status+" · "+
        fmt(row.published)+"/"+fmt(row.total)+
        " · archivos registrados "+fmt(row.audited)));
      const details=mk("button","secondary","Ver actividad");
      details.onclick=async()=>{
        details.disabled=true;
        try{
          const result=await api("bulk-admin",{operation:"details",id:row.id});
          card.querySelector(".bulk-admin-list")?.remove();
          const list=mk("div","bulk-admin-list");
          if(!result.audit.length)list.append(mk("p","","Sin archivos registrados."));
          for(const record of result.audit){
            const line=mk("p","",
              "Original "+record.original_id+
              " · destino "+record.destination_id+
              " · autor "+record.author+
              (record.reply_to?" · responde a "+record.reply_to:"")+
              (record.caption?" · "+record.caption:""));
            const privateChat=String(result.archive_chat_id||"");
            if(privateChat.startsWith("-100")&&record.archive_id){
              const link=mk("a",""," · Ver archivo guardado");
              link.href="https://t.me/c/"+privateChat.slice(4)+"/"+record.archive_id;
              link.target="_blank";link.rel="noopener noreferrer";
              line.append(link);
            }else{
              line.append(mk("span",""," · Archivo "+record.archive_id));
            }
            list.append(line);
          }
          card.append(list);
        }catch(error){card.append(mk("p","",error.message));}
        finally{details.disabled=false;}
      };
      const stop=mk("button","secondary","Detener");
      stop.onclick=async()=>{
        if(!window.confirm("¿Detener las transferencias de este usuario?"))return;
        try{
          await api("bulk-admin",{operation:"stop",uid:row.uid});
          await adminJobs();
        }catch(error){card.append(mk("p","",error.message));}
      };
      const block=mk("button","danger","Bloquear acceso");
      block.onclick=async()=>{
        if(!window.confirm("¿Bloquear el acceso del usuario "+row.uid+"?"))return;
        try{
          await api("bulk-admin",{operation:"ban",uid:row.uid});
          await adminJobs();
        }catch(error){card.append(mk("p","",error.message));}
      };
      card.append(details,stop,block);
      box.append(card);
    }
    if(!response.jobs?.length)box.append(mk("p","bulk-help","No hay transferencias registradas."));
  }
  async function openAdmin(){
    try{
      const response=await api("bulk-admin",{operation:"status"});
      if(!response.jobs)throw Error("No autorizado.");
      for(const id of ["steps","phone-step","code-step","password-step","done-step",
                       "creator-review","admin-tools","membership"])
        if(byId(id))byId(id).hidden=true;
      byId("bulk-admin-panel").hidden=false;
      await adminJobs();
    }catch(error){
      if(adminMode)byId("notice").textContent="Administración no disponible.";
    }
  }
  byId("bulk-open").addEventListener("click",()=>open());
  byId("bulk-job-select").addEventListener("change",()=>{
    state.selectedId=byId("bulk-job-select").value;
    state.lastRun=null;state.controls={};
    refresh().catch(error=>say(error.message));
  });
  byId("bulk-scan").addEventListener("click",()=>scan());
  byId("bulk-start").addEventListener("click",()=>begin());
  byId("bulk-pause").addEventListener("click",()=>pause());
  byId("bulk-resume").addEventListener("click",()=>resume());
  byId("bulk-disconnect").addEventListener("click",()=>disconnect());
  const continueButton=mk("button","secondary","Continuar desde el último avance");
  continueButton.id="bulk-continue";continueButton.type="button";continueButton.hidden=true;
  continueButton.onclick=()=>resume();
  byId("bulk-ranges").append(continueButton);
  byId("bulk-admin-refresh").onclick=()=>adminJobs().catch(()=>{});
  byId("bulk-admin-back").onclick=()=>{window.location.search="";};
  byId("bulk-admin-open").onclick=()=>{window.location.search="?mode=bulk-admin";};
  if(creatorMode){
    byId("bulk-open").hidden=true;
  }else if(adminMode){
    openAdmin();
  }else if(tg?.initData){
    api("admin-status").then(x=>{
      if(x.authorized){
        api("bulk-admin",{operation:"status"}).then(y=>{
          if(y.jobs)byId("bulk-admin-open").hidden=false;
        }).catch(()=>{});
      }
    }).catch(()=>{});
  }
})();
