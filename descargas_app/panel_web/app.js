(()=>{
const $=id=>document.getElementById(id),el=(tag,text)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;return e;},base=document.body.dataset.base;
let capabilities=null,operationBusy=false,replyDraft=null,attachedFile=null,sendRequest=null;
let panelReviewer=false,panelAdmin=false;
let favoriteItems=[],accountFavoriteItems=[];
let csrf='',account=null,chat=null,accountsNext=null,chatsNext=null,filter='all',mode='messages',before=null,query='',epoch=0,polling=false,rows=[],messages=new Map(),thumbQueue=[],thumbBusy=false,modal=false,retryAt=0,pendingEpoch=-1,requestId=0,listPolling=false,networkTail=Promise.resolve();
const imageCache=new Map();let imageBytes=0;let topic=0,topicNext=null,folder=null,folders=[],scheduleDraft=null,contextMode=false,contextFocus=null,autoHistoryArmed=false,olderLoading=false;
function clearImages(){for(const c of imageCache.values())URL.revokeObjectURL(c.url);imageCache.clear();imageBytes=0;thumbQueue=[];}
function network(url,options){const work=networkTail.then(()=>fetch(url,options));networkTail=work.then(()=>{},()=>{});return work;}
const labels={photo:'Foto',video:'Video',voice:'Mensaje de voz',audio:'Audio',document:'Archivo',sticker:'Sticker'};
const time=stamp=>new Date(stamp*1000).toLocaleTimeString('es',{hour:'2-digit',minute:'2-digit'}),date=stamp=>new Date(stamp*1000).toLocaleDateString('es',{day:'numeric',month:'short'});
function notice(text){$('global-notice').textContent=text||'';$('global-notice').hidden=!text;for(const dialog of document.querySelectorAll('dialog[open]')){let line=dialog.querySelector('.dialog-status');if(!line){line=el('p');line.className='dialog-status';dialog.append(line);}line.textContent=text||'';line.hidden=!text;}}
// Copy the exact Telegram ID without sending it anywhere or changing account state.
async function copyTelegramId(value){
 const id=String(value??'').trim();
 if(!/^-?\d+$/.test(id))return notice('ID de Telegram no disponible.');
 try{
  if(!navigator.clipboard?.writeText)throw Error('Clipboard no disponible');
  await navigator.clipboard.writeText(id);
 }catch{
  // Clipboard permissions differ across Android Chrome and Telegram/iOS webviews.
  const field=el('textarea');field.value=id;field.readOnly=true;
  field.style.cssText='position:fixed;left:-9999px;top:-9999px;opacity:0';
  document.body.append(field);field.select();
  let success=false;
  try{success=!!document.execCommand('copy');}catch{}
  field.remove();
  if(!success)return notice('No se pudo copiar. Mantén pulsado el número para seleccionarlo.');
 }
 notice('Telegram ID '+id+' copiado al portapapeles.');
}
function copyIdButton(value,description){
 const b=el('button','⧉');b.type='button';b.className='copy-id';
 b.title=description;b.setAttribute('aria-label',description+': '+value);
 b.onclick=e=>{e.preventDefault();e.stopPropagation();copyTelegramId(value).catch(error=>notice(error.message));};
 return b;
}
function locked(text){forgetAccounts();forgetResume();cancelOverviewSearch();cancelBatch();stopLive();disposePremium();epoch++;clearImages();favoriteItems=[];accountFavoriteItems=[];$('favorites-menu').hidden=true;$('workspace').hidden=true;$('locked').hidden=false;$('login-note').textContent=text;}
async function api(op,data,raw=false){const r=await network(base+'/api/'+op,{cache:'no-store',method:data?'POST':'GET',headers:data?{...(!raw?{'Content-Type':'application/json'}:{}),'X-Panel-CSRF':csrf}:{},body:data?(raw?data:JSON.stringify(data)):undefined});const result=await r.json();if(!r.ok){if(r.status===401||r.status===403)locked(result.error);if(result.retry)retryAt=Date.now()+Math.max(15000,result.retry*1000);throw Error(result.error||'No se pudo completar.');}return result;}
const view=v=>{$('workspace').dataset.view=v;if(v==='accounts')setTimeout(pumpStatuses,0);};
function avatar(title){const a=el('div',title.split(/\s+/).slice(0,2).map(v=>v[0]).join('').toUpperCase());a.className='avatar';return a;}
function fileUrl(op,m){return base+'/api/'+op+'?account='+account.id+'&peer='+encodeURIComponent(chat.handle)+(m?'&message='+m.id:'');}
function currentFavorite(){return favoriteItems.some(f=>String(f.account_id)===String(account?.id)&&String(f.id)===String(chat?.id));}
function updateFavoriteStar(){
 const button=$('favorite-toggle');button.hidden=!account||!chat;if(button.hidden)return;
 const active=currentFavorite();button.textContent=active?'★':'☆';
 button.setAttribute('aria-pressed',String(active));
 button.setAttribute('aria-label',active?'Quitar de favoritos':'Marcar como favorito');
 button.title=active?'Quitar de favoritos':'Marcar como favorito';
}
async function refreshAccountFavorites(){
 const result=await api('favorite-accounts');
 accountFavoriteItems=result.accounts||[];
 updateAccountFavoriteStars();
 if(!$('favorites-menu').hidden)renderFavoritesMenu();
 return accountFavoriteItems;
}
async function refreshFavorites(){
 const result=await api('favorites');
 favoriteItems=result.items||[];
 accountFavoriteItems=result.accounts||[];
 updateFavoriteStar();updateAccountFavoriteStars();
 if(!$('favorites-menu').hidden)renderFavoritesMenu();
 return favoriteItems;
}
function isAccountFavorite(uid){
 return accountFavoriteItems.some(item=>String(item.id)===String(uid));
}
function updateAccountFavoriteStar(a){
 const card=accountCards.get(String(a.id))||accountCards.get(a.id);
 if(!card?.favorite)return;
 const active=isAccountFavorite(a.id);
 card.favorite.textContent=active?'★':'☆';
 card.favorite.setAttribute('aria-pressed',String(active));
 card.favorite.setAttribute('aria-label',(active?'Quitar de favoritos: ':'Agregar a favoritos: ')+a.name);
 card.favorite.title=active?'Quitar cuenta de favoritos':'Marcar cuenta como favorita';
}
function updateAccountFavoriteStars(){
 for(const a of overviewAccounts.values())updateAccountFavoriteStar(a);
}
async function toggleAccountFavorite(a,button){
 if(!a)return;
 button.disabled=true;
 try{
  await api('favorite-account-toggle',{
   account:a.id,name:a.name,username:a.username||'',
   favorite:!isAccountFavorite(a.id)
  });
  await refreshAccountFavorites();
 }catch(e){notice(e.message);}
 finally{button.disabled=false;}
}
async function toggleChatFavorite(){
 if(!chat||!account)return;
 const chosen=chat,owner=account,button=$('favorite-toggle');button.disabled=true;
 try{
  await api('favorite-toggle',{account:owner.id,peer:chosen.handle,type:chosen.type,
   name:chosen.name,username:chosen.username||'',account_name:owner.name,
   favorite:!currentFavorite()});
  await refreshFavorites();
 }catch(e){notice(e.message);}
 finally{button.disabled=false;}
}
function hideFavoritesMenu(){
 $('favorites-menu').hidden=true;
 $('favorites-open').setAttribute('aria-expanded','false');
}
function renderFavoritesMenu(){
 const list=$('favorites-items');
 if(!list||$('favorites-menu').hidden)return;
 const selected=$('favorites-kind').value;
 const matching=selected==='account'?accountFavoriteItems:
   favoriteItems.filter(item=>item.type===selected);
 list.replaceChildren();
 if(!matching.length){
  list.append(el('p','No tienes favoritos en esta categoría.'));
  return;
 }
 for(const item of matching){
  const entry=el('div');entry.className='favorite-entry';
  const open=el('button');open.type='button';open.className='favorite-open';
  const isAccount=selected==='account';
  if(isAccount){
   const saved=overviewAccounts.get(String(item.id))||overviewAccounts.get(item.id);
   const title=saved?.name||item.name;
   open.append(el('strong',title),
     el('small','Cuenta · Telegram ID: '+item.id),
     el('small',(saved?.username||item.username?'@'+(saved?.username||item.username)+' · ':'')+
        (item.available?'Cuenta vinculada':'Cuenta sin sesión activa')));
   open.onclick=async()=>{
    if(!item.available)return notice('Esta cuenta necesita una sesión activa para abrir sus chats.');
    hideFavoritesMenu();
    try{
     await selectAccount(saved||{id:item.id,name:item.name,
       username:item.username||'',generation:item.generation});
    }catch(e){notice(e.message);}
   };
  }else{
   open.append(el('strong',item.name),
     el('small',(item.username?'@'+item.username+' · ':'')+
       ({group:'Grupo',channel:'Canal',private:'Privado'}[item.type]||'Chat')),
     el('small','ID: '+item.id+' · Telegram ID: '+item.account_id),
     el('small','Cuenta: '+item.account+(item.available?'':' · Reconecta la cuenta para abrir')));
   open.onclick=async()=>{
    if(!item.available||!item.handle)
     return notice('Actualiza la lista de chats de la cuenta vinculada para abrir este favorito.');
    hideFavoritesMenu();
    try{
     if(!account||String(account.id)!==String(item.account_id)){
      const a=overviewAccounts.get(String(item.account_id))||
       {id:item.account_id,name:item.account,generation:item.generation};
      await selectAccount(a);
     }
     await openChat({id:item.id,handle:item.handle,name:item.name,
      username:item.username,type:item.type,avatar:false,forum:false,members:null});
    }catch(e){notice(e.message);}
   };
  }
  const star=el('button','★');star.type='button';star.className='favorite-remove';
  star.title='Quitar de favoritos';
  star.setAttribute('aria-label','Quitar '+item.name+' de favoritos');
  star.onclick=async()=>{
   star.disabled=true;
   try{
    if(isAccount){
     await api('favorite-account-toggle',{account:item.id,favorite:false});
     await refreshAccountFavorites();
    }else{
     await api('favorite-remove',{account:item.account_id,source:item.id});
     await refreshFavorites();
    }
   }catch(e){star.disabled=false;notice(e.message);}
  };
  entry.append(open,star);
  list.append(entry);
 }
}
function favoriteFolder(){
 const menu=$('favorites-menu');
 if(!menu.hidden){hideFavoritesMenu();return;}
 menu.hidden=false;
 $('favorites-open').setAttribute('aria-expanded','true');
 $('favorites-items').replaceChildren(el('p','Cargando favoritos…'));
 refreshFavorites().catch(e=>{
  if(!menu.hidden){$('favorites-items').replaceChildren(el('p','No se pudieron consultar los favoritos.'));notice(e.message);}
 });
}
$('favorites-kind').onchange=renderFavoritesMenu;
$('favorite-toggle').onclick=toggleChatFavorite;
$('copy-account-id').onclick=()=>{if(account)copyTelegramId(account.id);};
$('copy-chat-id').onclick=()=>{if(chat&&(chat.type==='group'||chat.type==='channel'))copyTelegramId(chat.id);};
$('favorites-open').onclick=favoriteFolder;
$('favorites-folder').onclick=()=>{
 view('accounts');
 if($('favorites-menu').hidden)favoriteFolder();
 else renderFavoritesMenu();
};
function scheduleImage(img,url){thumbQueue.push({img,url,epoch,protected:img.dataset.protected==='1'});pumpThumbs();}
async function pumpThumbs(){if(thumbBusy||modal||operationBusy||!thumbQueue.length)return;thumbBusy=true;const task=thumbQueue.shift();if(task.epoch===epoch&&task.img.isConnected){try{const r=await network(task.url,{cache:'no-store'});if(r.ok&&r.status!==204){const blob=await r.blob();if(task.epoch===epoch&&task.img.isConnected){const url=URL.createObjectURL(blob);if(task.protected){task.img.onload=()=>URL.revokeObjectURL(url);task.img.onerror=()=>URL.revokeObjectURL(url);}else{imageCache.set(task.url,{url,size:blob.size});imageBytes+=blob.size;}while((imageBytes>8*1024*1024||imageCache.size>120)&&imageCache.size>1){const [key,old]=imageCache.entries().next().value;imageCache.delete(key);imageBytes-=old.size;URL.revokeObjectURL(old.url);}task.img.src=url;task.img.style.opacity='1';}}}catch{}}thumbBusy=false;pumpThumbs();}
let profileQueue=[],profileBusy=false;
async function pumpProfiles(){if(profileBusy||operationBusy||!profileQueue.length)return;profileBusy=true;const task=profileQueue.shift();try{if(task.epoch===epoch&&task.row.isConnected){const data=await api('account-profile?account='+task.account.id);if(task.epoch===epoch&&task.row.isConnected){task.account.name=data.name;task.account.username=data.username;task.title.textContent=data.name;updateAccountCard(task.account);queueAccountCache();queueStatus(task.account);task.avatar.textContent=avatar(data.name).textContent;if(data.avatar)lazyImage(task.avatar,base+'/api/account-avatar?account='+task.account.id,data.name);}}}catch{}finally{profileBusy=false;pumpProfiles();}}
const profileObserver=new IntersectionObserver(entries=>{for(const item of entries)if(item.isIntersecting){profileObserver.unobserve(item.target);const task=item.target.profileTask;if(task){profileQueue.push(task);pumpProfiles();}}},{rootMargin:'80px'});
const imageObserver=new IntersectionObserver(entries=>{for(const item of entries)if(item.isIntersecting){const img=item.target;imageObserver.unobserve(img);if(img.dataset.url)scheduleImage(img,img.dataset.url);}}, {rootMargin:'100px'});
function lazyImage(parent,url,alt,protectedMedia=false){const img=el('img');img.alt=alt;img.style.opacity='0';img.dataset.url=url;img.dataset.protected=protectedMedia?'1':'0';img.draggable=false;if(protectedMedia)img.oncontextmenu=e=>e.preventDefault();parent.append(img);const cached=protectedMedia?null:imageCache.get(url);if(cached){img.src=cached.url;img.style.opacity='1';}else imageObserver.observe(img);return img;}
async function loadAccounts(append=false){const r=await api('accounts'+(append&&accountsNext?'?after='+accountsNext:''));panelReviewer=!!r.reviewer;if(!append){loadedAccountPages=0;profileObserver.disconnect();profileQueue=[];statusQueue=[];overviewAccounts.clear();accountCards.clear();buildAccountGroups();}for(const a of r.items){overviewAccounts.set(a.id,a);if(!accountCards.has(a.id))createAccountCard(a);else updateAccountCard(a);}accountsNext=r.next;loadedAccountPages++;$('more-accounts').hidden=!r.next;updateAccountGroups();if(!append&&!r.items.length)$('accounts').append(el('p',r.next?'No hay cuentas visibles en esta página. Pulsa Cargar más cuentas.':'No hay cuentas disponibles con tus permisos.'));queueAccountCache();}
async function selectAccount(a){if(operationBusy)return notice('Espera a que termine la operación en curso.');cancelOverviewSearch();stopLive();resetComposer();$('manage-chat').hidden=true;$('favorite-toggle').hidden=true;epoch++;thumbQueue=[];account=a;chat=null;rows=[];messages.clear();mode='messages';$('gallery-count').hidden=true;$('thread').classList.remove('gallery');$('thread').replaceChildren(el('p','Selecciona una conversación de esta cuenta.'));$('chat-title').textContent='Tus conversaciones';$('chat-subtitle').textContent=a.name;$('chat-avatar').textContent='➤';$('message-tabs').hidden=true;$('older').hidden=true;filter='all';document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b.dataset.filter==='all'));query='';$('chat-search').value='';$('account-name').textContent=a.name;$('account-telegram-id').textContent='Telegram ID: '+a.id;$('account-telegram-row').hidden=false;$('account-chip').textContent=a.name;view('chats');$('chats').replaceChildren(el('p','Cargando chats…'));notice('');await pausePersistentSearchForChats();await loadChats();await loadFolders();startLive();refreshFavorites().catch(()=>{});}
async function loadChats(append=false,quiet=false){if(!account)return;const requestEpoch=epoch;const r=await api('chats?account='+account.id+(append&&chatsNext?'&cursor='+encodeURIComponent(chatsNext):''));if(requestEpoch!==epoch)return;if(account.generation!==undefined&&r.generation!==undefined&&account.generation!==r.generation)rows=[];account.generation=r.generation;const oldRows=JSON.stringify(rows);const incoming=r.items.map(item=>{const old=rows.find(r=>r.id===item.id);return old?{...item,handle:old.handle}:item;});rows=append?[...rows,...incoming.filter(item=>!rows.some(old=>old.id===item.id))]:quiet?[...incoming,...rows.filter(old=>!incoming.some(item=>item.id===old.id))]:incoming;if(!quiet)chatsNext=r.next;account.name=r.account||account.name;$('account-name').textContent=account.name;$('account-chip').textContent=account.name;notice('');$('more-chats').hidden=!chatsNext;if(!quiet||JSON.stringify(rows)!==oldRows)renderChats();}
function matchesChatFilter(c,selected){return selected==='all'||(selected==='owner'?!!c.owner:selected==='admin'?!!c.admin:selected==='saved'?!!c.saved:c.type===selected);}
function renderChats(){for(const img of $('chats').querySelectorAll('img'))imageObserver.unobserve(img);const q=$('chat-search').value.trim().toLowerCase();$('chats').replaceChildren();for(const c of rows.filter(c=>matchesChatFilter(c,filter)&&folderMatches(c)&&(!q||c.name.toLowerCase().includes(q)))){const b=el('button');b.className='chat-row'+(c.id===chat?.id?' selected':'');const av=avatar(c.name);if(c.avatar)lazyImage(av,base+'/api/avatar?account='+account.id+'&peer='+encodeURIComponent(c.handle),c.name);b.append(av);const body=el('div');body.className='chat-body';const line=el('div');line.className='chat-line';line.append(el('strong',c.name),el('time',c.date?time(c.date):''));const preview=el('div');preview.className='chat-preview';preview.append(el('p',labels[c.preview]||c.preview||'Sin mensajes'));if(c.unread){const n=el('span',c.unread>999?'999+':c.unread);n.className='badge';preview.append(n);}body.append(line);const showPeerId=panelAdmin&&(c.type==='group'||c.type==='channel');if(showPeerId)body.append(el('small','ID: '+c.id));body.append(preview);if(c.members!=null&&c.type!=='private')body.append(el('small',Number(c.members).toLocaleString('es')+(c.type==='channel'?' suscriptores':' miembros')));b.append(body);b.onclick=()=>openChat(c).catch(e=>notice(e.message));if(showPeerId){const shell=el('div');shell.className='chat-row-shell';shell.append(b,copyIdButton(c.id,'Copiar ID del '+(c.type==='channel'?'canal':'grupo')));$('chats').append(shell);}else $('chats').append(b);}if(!$('chats').children.length){const p=el('p',chatsNext?'Sin resultados en esta página. Carga más chats para continuar.':'No hay conversaciones disponibles.');p.className='empty';$('chats').append(p);}}
async function openChat(c,resume=null){if(operationBusy)return notice('Espera a que termine la operación en curso.');stopLive();resetComposer();topic=0;topicNext=null;contextMode=false;autoHistoryArmed=false;$('return-latest').hidden=true;$('topic-bar').hidden=true;capabilities=null;epoch++;thumbQueue=[];chat=c;mode='messages';query='';messages.clear();before=null;$('chat-title').textContent=c.name;const showHeaderPeerId=panelAdmin&&(c.type==='group'||c.type==='channel');$('chat-id-row').hidden=!showHeaderPeerId;$('chat-source-id').hidden=!showHeaderPeerId;$('chat-source-id').textContent=showHeaderPeerId?'ID: '+c.id:'';$('chat-subtitle').textContent={private:'Chat privado',group:'Grupo',channel:'Canal'}[c.type]+' · '+account.name;$('chat-avatar').replaceChildren(...avatar(c.name).childNodes);updateFavoriteStar();if(c.avatar)lazyImage($('chat-avatar'),fileUrl('avatar'),c.name);$('chat-counts').hidden=true;loadChatCounts();$('message-tabs').hidden=false;$('message-search').hidden=true;$('message-query').value='';tabs();view('conversation');notice('');$('thread').replaceChildren(el('p','Cargando conversación…'));renderChats();if(resume)await restoreConversation(resume);else await loadMessages();await loadCapabilities();if(c.forum)await loadTopics();const opened=epoch;requestAnimationFrame(()=>{if(!resume&&opened===epoch&&mode==='messages')$('thread').scrollTop=$('thread').scrollHeight;});startLive();}
async function loadChatCounts(){if(!chat||chat.type==='private')return;const chosen=epoch,selected=chat;const line=$('chat-counts');line.hidden=false;line.textContent=chat.members!=null?Number(chat.members).toLocaleString('es')+' miembros':'Consultando miembros…';try{const data=await api('chat-counts?account='+account.id+'&peer='+encodeURIComponent(selected.handle));if(chosen!==epoch||chat!==selected)return;line.textContent=(data.members==null?'Miembros: no disponible':Number(data.members).toLocaleString('es')+(chat.type==='channel'?' suscriptores':' miembros'))+(chat.type==='group'?' · '+(data.online==null?'Conectados: no disponible':Number(data.online).toLocaleString('es')+' conectados'):'');}catch{if(chosen===epoch&&chat===selected&&selected.members==null)line.textContent='Miembros: no disponible';}}
function tabs(){$('gallery-count').hidden=mode!=='gallery';$('messages-tab').classList.toggle('active',mode==='messages');$('gallery-tab').classList.toggle('active',mode==='gallery');$('thread').classList.toggle('gallery',mode==='gallery');$('older').textContent=mode==='gallery'?'Cargar más fotos y videos':'Cargar mensajes anteriores';}
async function loadGalleryCount(){const chosen=epoch;try{const r=await api('gallery-count?account='+account.id+'&peer='+encodeURIComponent(chat.handle)+(topic?'&topic='+topic:''));if(chosen!==epoch||mode!=='gallery')return;const number=n=>Number(n).toLocaleString('es');$('gallery-count').textContent=(topic?'Total del tema: ':'Total del chat: ')+(r.approximate?'≈ ':'')+number(r.photos)+' fotos · '+number(r.videos)+' videos';}catch(e){if(chosen===epoch&&mode==='gallery')$('gallery-count').textContent='No se pudo consultar el total. Vuelve a abrir la galería para reintentar.';}}
async function loadMessages(older=false,quiet=false){if(!chat||operationBusy||(quiet&&contextMode)||(polling&&pendingEpoch===epoch))return;polling=true;pendingEpoch=epoch;const runId=++requestId,chosen=epoch;
try{const r=await api((mode==='gallery'?'gallery':'messages')+'?account='+account.id+'&peer='+encodeURIComponent(chat.handle)+(older&&before?'&before='+before:'')+(query?'&q='+encodeURIComponent(query):'')+(topic?'&topic='+topic:''));if(chosen!==epoch)return;const anchor=captureAnchor(),oldHeight=$('thread').scrollHeight,oldTop=$('thread').scrollTop,nearBottom=oldHeight-oldTop-$('thread').clientHeight<100;if(older||!messages.size)before=r.next;let changed=false;for(const m of r.items){const signature=JSON.stringify(m);if(messages.get(m.id)?.signature!==signature){messages.set(m.id,{...m,signature});changed=true;}}if(!older&&r.items.length){const floor=Math.min(...r.items.map(m=>m.id)),incoming=new Set(r.items.map(m=>m.id));for(const id of messages.keys())if(id>=floor&&!incoming.has(id)){messages.delete(id);changed=true;}}if(changed||!quiet)renderMessages();$('older').hidden=!before;if(mode==='gallery'&&!older)loadGalleryCount();if(quiet&&!changed)return;if(older){if(!restoreAnchor(anchor))$('thread').scrollTop=oldTop+$('thread').scrollHeight-oldHeight;}else if(!quiet||(mode==='messages'&&nearBottom))$('thread').scrollTop=mode==='messages'?$('thread').scrollHeight:0;else if(!restoreAnchor(anchor))$('thread').scrollTop=oldTop;if(r.status)$('chat-subtitle').textContent=r.status+' · '+account.name;notice('');}catch(e){if(chosen===epoch)notice(e.message);}finally{if(runId===requestId)polling=false;}}
let batchLimit=500;let batchSelecting=false;const batchSelected=new Set();
function batchEligible(m){return !!m&&['photo','video'].includes(m.media?.type);}
function updateBatch(){const ready=!!chat&&!!$('message-tabs')&&!$('message-tabs').hidden;$('batch-toolbar').hidden=!ready;$('batch-select').hidden=batchSelecting;for(const id of ['batch-count','batch-all','batch-download','batch-cancel'])$(id).hidden=!batchSelecting;$('batch-count').textContent=batchSelected.size+' seleccionados';$('batch-download').disabled=!batchSelected.size||operationBusy;for(const node of $('thread').querySelectorAll('[data-media-id]')){const id=Number(node.dataset.mediaId),m=messages.get(id),eligible=batchEligible(m);node.classList.toggle('selectable-media',batchSelecting&&eligible);node.classList.toggle('selected-media',batchSelecting&&batchSelected.has(id));if(batchSelecting&&eligible)node.setAttribute('aria-pressed',String(batchSelected.has(id)));else node.removeAttribute('aria-pressed');}}
function cancelBatch(){batchSelecting=false;batchSelected.clear();updateBatch();}
function toggleBatch(m){if(!batchEligible(m))return notice('Este archivo no está disponible para descarga.');if(batchSelected.has(m.id))batchSelected.delete(m.id);else{if(batchSelected.size>=batchLimit)return notice('Puedes marcar hasta '+batchLimit+' archivos por lote.');batchSelected.add(m.id);}updateBatch();}
$('batch-select').onclick=()=>{batchSelecting=true;updateBatch();};$('batch-cancel').onclick=cancelBatch;
$('batch-all').onclick=()=>{for(const m of messages.values())if(batchEligible(m)&&batchSelected.size<batchLimit)batchSelected.add(m.id);updateBatch();};
let zipController=null,zipObjectUrl=null;
function zipStatus(text,percent=null){$('zip-progress').hidden=false;$('zip-progress').textContent=text;$('zip-meter').hidden=percent===null;if(percent!==null)$('zip-meter').value=percent;}
$('batch-download').onclick=async()=>{if(!batchSelected.size||operationBusy||!chat||zipController)return;zipController=true;$('batch-download').disabled=true;zipStatus('Registrando entrega…');try{const r=await api('deliver-selection',{account:account.id,peer:chat.handle,messages:[...batchSelected]});zipStatus(r.message);notice(r.message);}catch(e){zipStatus(e.message);}finally{zipController=null;$('batch-download').disabled=!batchSelected.size||operationBusy;}};
const cancelSelection=$('batch-cancel').onclick;
$('batch-cancel').onclick=()=>{if(zipController)return;cancelSelection();};
function installMediaSelection(node,m){let timer=null,start=null,suppressClick=false;const clear=()=>{clearTimeout(timer);timer=null;start=null;};node.addEventListener('pointerdown',event=>{if(event.button!==0||!batchEligible(m))return;clear();suppressClick=false;start={x:event.clientX,y:event.clientY};timer=setTimeout(()=>{timer=null;start=null;suppressClick=true;batchSelecting=true;toggleBatch(m);},500);});node.addEventListener('pointermove',event=>{if(start&&Math.hypot(event.clientX-start.x,event.clientY-start.y)>10)clear();});for(const type of ['pointerup','pointercancel','pointerleave'])node.addEventListener(type,clear);node.addEventListener('contextmenu',event=>{if(batchEligible(m))event.preventDefault();});node.onclick=event=>{if(suppressClick){suppressClick=false;event.preventDefault();return;}if(batchSelecting)toggleBatch(m);else openMedia(m);};}
function attachment(m){const info=m.media,b=el('button');b.className='attachment';b.dataset.mediaId=String(m.id);b.setAttribute('aria-label','Abrir '+(labels[info.type]||info.name));if(['photo','video','sticker'].includes(info.type)){const img=lazyImage(b,fileUrl('thumb',m),labels[info.type],m.protected);img.style.aspectRatio=String(info.width||4)+' / '+String(info.height||3);const glyph=el('span',info.type==='video'?'▶':'▧');glyph.className='placeholder';b.append(glyph);img.addEventListener('load',()=>{if(info.type!=='video')glyph.hidden=true;});if(info.duration){const d=el('span',Math.floor(info.duration/60)+':'+String(Math.floor(info.duration%60)).padStart(2,'0'));d.className='duration';b.append(d);}}else{b.classList.add(info.type==='document'?'document':'audio');const play=el('span',info.type==='document'?'↓':'▶');play.className='play';b.append(play);if(info.type!=='document'){const wave=el('span','┃▏┃▎┃▏┃▎┃▏┃');wave.className='wave';b.append(wave);}b.append(el('span',info.type==='document'?info.name:(info.duration?Math.floor(info.duration/60)+':'+String(Math.floor(info.duration%60)).padStart(2,'0'):labels[info.type])));}if(mode==='gallery'){b.dataset.message=String(m.id);const type=el('span',date(m.date));type.className='type';b.append(type);}if(m.protected){b.oncontextmenu=e=>e.preventDefault();b.classList.add('protected-media');}installMediaSelection(b,m);return b;}
function richText(parent,text,entities=[],messageId){
 const allowed={Bold:'strong',Italic:'em',Underline:'u',Strike:'s',Code:'code',Pre:'code',Blockquote:'blockquote',Spoiler:'span',TextUrl:'a',Url:'a',Email:'a',Mention:'a'};
 const valid=entities.filter(e=>(allowed[e.type]||e.type==='CustomEmoji')&&Number.isInteger(e.offset)&&Number.isInteger(e.length)&&e.offset>=0&&e.length>0&&e.offset+e.length<=text.length);
 const bounds=[...new Set([0,text.length,...valid.flatMap(e=>[e.offset,e.offset+e.length])])].sort((a,b)=>a-b);
 for(let i=0;i<bounds.length-1;i++){const a=bounds[i],b=bounds[i+1],custom=valid.find(e=>e.type==='CustomEmoji'&&e.offset<=a&&e.offset+e.length>=b);if(custom&&a!==custom.offset)continue;let outer=parent;for(const e of valid.filter(e=>e.type!=='CustomEmoji'&&e.offset<=a&&e.offset+e.length>=b).sort((a,b)=>b.length-a.length)){const node=el(allowed[e.type]);if(e.type==='Spoiler'){node.className='spoiler';node.tabIndex=0;node.onclick=()=>node.classList.toggle('revealed');node.onkeydown=ev=>{if(ev.key==='Enter'||ev.key===' ')node.classList.toggle('revealed');};}if(node.tagName==='A'){let url=e.url||text.slice(e.offset,e.offset+e.length);if(e.type==='Email')url='mailto:'+url;if(e.type==='Mention')url='https://t.me/'+url.slice(1);try{const target=new URL(url);if(!['http:','https:','mailto:','tg:'].includes(target.protocol))continue;node.href=target.href;node.target='_blank';node.rel='noopener noreferrer';}catch{continue;}}outer.append(node);outer=node;}outer.append(custom?premiumSpan(custom.document,text.slice(custom.offset,custom.offset+custom.length),messageId):document.createTextNode(text.slice(a,b)));}
}
function renderPoll(parent,m){const p=m.poll,box=el('div');box.className='poll';box.append(el('strong',p.question),el('small',p.closed?'Encuesta cerrada':p.quiz?'Cuestionario':p.multiple?'Varias respuestas':'Encuesta'));const selected=new Set();for(const a of p.answers){const button=el('button',(a.chosen?'✓ ':'')+a.text+(p.total?' · '+Math.round(a.votes/p.total*100)+'%':''));button.disabled=p.closed;button.onclick=async()=>{try{if(p.multiple){selected.has(a.option)?selected.delete(a.option):selected.add(a.option);button.classList.toggle('selected',selected.has(a.option));}else{await change('vote',{message:m.id,options:[a.option]});await refreshAfterChange();}}catch(e){notice(e.message);}};box.append(button);}if(p.multiple&&!p.closed)option(box,'Enviar voto',async()=>{await change('vote',{message:m.id,options:[...selected]});await refreshAfterChange();});box.append(el('small',p.total+' votos'));parent.append(box);}
function renderMessages(){queueResume();disposePremium();requestAnimationFrame(activatePremium);const thread=$('thread');for(const img of thread.querySelectorAll('img'))imageObserver.unobserve(img);thread.replaceChildren();const gallery=mode==='gallery'?el('div'):null;if(gallery){gallery.className='gallery-grid';thread.append(gallery);}let previous='',album=null,albumId=null;const items=[...messages.values()].sort((a,b)=>mode==='gallery'?b.id-a.id:a.id-b.id);for(const m of items){if(mode==='gallery'){if(m.media)gallery.append(attachment(m));continue;}const day=date(m.date);if(day!==previous){const label=el('div',day);label.className='day';thread.append(label);previous=day;album=null;albumId=null;}const b=el('article');b.dataset.message=String(m.id);const tools=el('button','⋯');tools.className='bubble-tools';tools.setAttribute('aria-label','Opciones del mensaje');tools.onclick=()=>messageOptions(m);b.append(tools);b.className='bubble'+(m.out?' out':'')+(m.protected?' protected-media':'');if(m.sender&&!m.out&&chat.type!=='private'){const sender=el('div',m.sender);sender.className='sender';b.append(sender);}if(m.reply){const preview=m.reply_preview||messages.get(m.reply),quote=el('button');quote.className='quote';quote.append(el('strong',preview?.sender||'Respuesta'),el('span',preview?.text?.slice(0,180)||labels[preview?.media?.type]||'Ver mensaje original'));quote.disabled=!!m.reply_external;quote.onclick=()=>jumpToMessage(m.reply).catch(e=>notice(e.message));b.append(quote);}if(m.media)b.append(attachment(m));if(m.poll)renderPoll(b,m);if(m.text){const text=el('div');text.className='message-text';richText(text,m.text,m.entities,m.id);b.append(text);}else if(m.protected&&!m.media&&!m.poll)b.append(el('p','Contenido protegido'));else if(!m.media&&!m.poll)b.append(el('p','Mensaje de servicio'));if(m.reactions?.length){const row=el('div');row.className='reactions';for(const r of m.reactions){const btn=el('button');if(r.document)btn.append(premiumSpan(r.document,'✦',m.id),document.createTextNode(' '+r.count));else btn.textContent=(r.emoji||'✦')+' '+r.count;btn.className=r.chosen?'selected':'';btn.onclick=async()=>{try{if(!r.emoji)return reactionDialog(m);await change('reaction',{message:m.id,emoji:r.chosen?'':r.emoji});await refreshAfterChange();}catch(e){notice(e.message);}};row.append(btn);}b.append(row);}if(panelAdmin&&chat.type!=='private'&&!m.out){const report=el('button','🚩 Reportar publicación');report.type='button';report.className='bubble-report';report.setAttribute('aria-label','Reportar esta publicación a Telegram');report.onclick=event=>{event.stopPropagation();reportMessageDialog(m).catch(e=>notice(e.message));};b.append(report);}b.append(el('time',(m.edited?'editado · ':'')+time(m.date)+(m.out&&m.delivery?(m.delivery==='read'?' ✓✓':' ✓'):'')));if(m.grouped&&m.media){if(albumId!==m.grouped){album=el('div');album.className='album'+(m.out?' out':'');albumId=m.grouped;thread.append(album);}album.append(b);}else{album=null;albumId=null;thread.append(b);}}if(!items.length){const p=el('p',query?'Sin resultados.':'Todavía no hay contenido disponible en esta vista.');p.className='empty';thread.append(p);}updateBatch();}
let mediaCurrent=null,mediaItems=[],mediaGallery=false,mediaPaging=false,mediaGesture=null,mediaRender=0;
const visualMedia=m=>['photo','video'].includes(m.media?.type);
function mediaList(){return [...messages.values()].filter(visualMedia).sort((a,b)=>mediaGallery?b.id-a.id:a.id-b.id);}
function stopMediaPlayback(){for(const player of $('media-content').querySelectorAll('video,audio')){player.pause();player.removeAttribute('src');player.load();}}
function updateMediaNavigation(){const index=mediaItems.findIndex(m=>m.id===mediaCurrent?.id),more=mediaGallery&&!!before;$('media-previous').disabled=mediaPaging||index<=0;$('media-next').disabled=mediaPaging||index<0||(index>=mediaItems.length-1&&!more);$('media-position').textContent=mediaPaging?'Cargando más…':(index+1)+' de '+mediaItems.length+(more?' cargados':'');$('media-navigation').hidden=!visualMedia(mediaCurrent||{});$('media-to-chat').disabled=mediaPaging||operationBusy;}
function openMedia(m){mediaGallery=mode==='gallery';mediaItems=visualMedia(m)?mediaList():[m];if(!mediaItems.some(item=>item.id===m.id))mediaItems.push(m);mediaPaging=false;modal=true;showMedia(m);if(!$('media-dialog').open)$('media-dialog').showModal();}
function showMedia(m){if(typeof panelAdmin!=='undefined'&&typeof chat!=='undefined')reportMediaButton.hidden=!panelAdmin||!chat||chat.type==='private'||!!m.out;const render=++mediaRender;mediaCurrent=m;mediaGesture=null;stopMediaPlayback();const info=m.media,box=$('media-content');box.replaceChildren();box.classList.toggle('protected-media',!!m.protected);$('media-name').textContent=info.name||labels[info.type];$('media-note').textContent='';const url=fileUrl('media',m),current=()=>modal&&mediaRender===render&&mediaCurrent?.id===m.id;$('download').hidden=false;$('download').setAttribute('aria-disabled','false');$('download').removeAttribute('href');$('download').removeAttribute('download');$('download').textContent='Enviar descarga';box.oncontextmenu=null;if(info.type==='photo'||(info.mime||'').startsWith('image/')){const img=el('img');img.alt=m.text||'Foto';img.draggable=false;networkTail.then(()=>{if(current())img.src=url;});img.onerror=()=>{if(current())$('media-note').textContent='No se pudo abrir la imagen. Puedes pasar al siguiente archivo o intentarlo de nuevo.';};box.append(img);}else if(['video','voice','audio'].includes(info.type)){const player=el(info.type==='video'?'video':'audio');player.controls=true;player.preload='metadata';player.playsInline=true;player.src=url;player.onerror=()=>{if(!current())return;const code=player.error?.code;$('media-note').textContent=code===2?'Se interrumpió la transferencia del video. Puedes reintentarlo.':code===3?'Este navegador no pudo decodificar el video.':(m.protected?'No se pudo reproducir este archivo en el navegador.':'No se pudo reproducir: la transferencia falló o el navegador no admite el formato. Puedes descargarlo.');const retry=el('button','Reintentar');retry.type='button';retry.className='media-to-chat';retry.onclick=()=>{if(current())showMedia(m);};$('media-note').append(retry);};box.append(player);}else box.append(el('p','Usa Descargar archivo para abrir este documento.'));updateMediaNavigation();}
async function navigateMedia(direction){if(!modal||!mediaCurrent||mediaPaging||!visualMedia(mediaCurrent))return;const id=mediaCurrent.id,chosen=mediaRender,viewerEpoch=epoch;let index=mediaItems.findIndex(m=>m.id===id),target=index+direction;if(target>=mediaItems.length&&direction>0&&mediaGallery&&before){mediaPaging=true;updateMediaNavigation();try{await loadMessages(true);if(!modal||mediaRender!==chosen||epoch!==viewerEpoch)return;mediaItems=mediaList();index=mediaItems.findIndex(m=>m.id===id);target=index+direction;}finally{if(mediaRender===chosen){mediaPaging=false;updateMediaNavigation();}}}if(!modal||mediaRender!==chosen||epoch!==viewerEpoch||index<0||target<0||target>=mediaItems.length)return;showMedia(mediaItems[target]);}
function closeMedia(){mediaRender++;stopMediaPlayback();$('media-content').replaceChildren();mediaCurrent=null;mediaItems=[];mediaPaging=false;mediaGesture=null;modal=false;pumpThumbs();}
$('close-media').onclick=()=>$('media-dialog').close();$('media-dialog').addEventListener('close',closeMedia);
$('media-previous').onclick=()=>navigateMedia(-1).catch(e=>notice(e.message));$('media-next').onclick=()=>navigateMedia(1).catch(e=>notice(e.message));
$('download').onclick=async event=>{event.preventDefault();if(!mediaCurrent||operationBusy)return;const button=$('download');if(button.dataset.sending)return;button.dataset.sending='1';$('media-note').textContent='Registrando entrega…';try{const r=await api('deliver-selection',{account:account.id,peer:chat.handle,messages:[mediaCurrent.id]});$('media-note').textContent=r.message;}catch(e){$('media-note').textContent=e.message;}finally{delete button.dataset.sending;}};
const reportMediaButton=$('report-media');reportMediaButton.onclick=()=>{const chosen=mediaCurrent;$('media-dialog').close();if(chosen)reportMessageDialog(chosen).catch(e=>notice(e.message));};
$('media-to-chat').onclick=async()=>{if(!mediaCurrent||mediaPaging||operationBusy)return;const id=mediaCurrent.id;$('media-dialog').close();closeMedia();$('message-query').value='';$('message-search').hidden=true;try{await jumpToMessage(id,true);}catch(e){notice(e.message);}};
$('media-dialog').addEventListener('keydown',event=>{if(['INPUT','TEXTAREA','SELECT','VIDEO','AUDIO'].includes(event.target.tagName)||event.altKey||event.ctrlKey||event.metaKey)return;if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();navigateMedia(event.key==='ArrowLeft'?-1:1).catch(e=>notice(e.message));}});
$('media-content').addEventListener('touchstart',event=>{mediaGesture=event.touches.length===1?{x:event.touches[0].clientX,y:event.touches[0].clientY,at:Date.now(),render:mediaRender}:null;},{passive:true});
$('media-content').addEventListener('touchmove',event=>{if(event.touches.length!==1)mediaGesture=null;},{passive:true});
$('media-content').addEventListener('touchcancel',()=>{mediaGesture=null;},{passive:true});
$('media-content').addEventListener('touchend',event=>{const start=mediaGesture;mediaGesture=null;if(!start||event.touches.length||!event.changedTouches.length||start.render!==mediaRender||Date.now()-start.at>1000)return;const dx=event.changedTouches[0].clientX-start.x,dy=event.changedTouches[0].clientY-start.y;if(Math.abs(dx)>=55&&Math.abs(dx)>Math.abs(dy)*1.5)navigateMedia(dx<0?1:-1).catch(e=>notice(e.message));},{passive:true});
$('more-accounts').onclick=()=>loadAccounts(true).catch(e=>notice(e.message));$('more-chats').onclick=()=>loadChats(true).catch(e=>notice(e.message));$('reload').onclick=()=>loadChats().catch(e=>notice(e.message));$('older').onclick=()=>loadMessages(true);
$('back-accounts').onclick=()=>{if(operationBusy)return;stopLive();resetComposer();epoch++;chat=null;view('accounts');for(const row of $('accounts').querySelectorAll('.account')){if(row.profileTask){row.profileTask.epoch=epoch;profileObserver.observe(row);}}};$('tab-accounts').onclick=$('back-accounts').onclick;$('back-chats').onclick=()=>{if(operationBusy)return;stopLive();resetComposer();epoch++;chat=null;contextMode=false;view('chats');startLive();};$('chat-search').oninput=renderChats;
for(const b of document.querySelectorAll('[data-filter]'))b.onclick=async()=>{filter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(n=>n.classList.toggle('active',n===b));renderChats();const chosen=epoch;try{if(filter==='saved'){const saved=await api('saved-chat?account='+account.id);if(chosen===epoch&&filter==='saved')await openChat(saved);return;}if(['owner','admin'].includes(filter)){const selected=filter;let pages=0;while(chatsNext&&chosen===epoch&&filter===selected&&pages++<20){notice('Consultando más chats para este filtro…');await loadChats(true);}if(chosen===epoch)notice(chatsNext?'El filtro es parcial. Carga más chats para continuar.':'');}}catch(e){notice(e.message);}};
async function setMode(next){if(mode===next)return;previousScrollTop=0;epoch++;contextMode=false;autoHistoryArmed=false;$('return-latest').hidden=true;mode=next;if(next==='gallery')$('gallery-count').textContent='Consultando total…';messages.clear();before=null;thumbQueue=[];tabs();await loadMessages();}
$('messages-tab').onclick=()=>setMode('messages');$('gallery-tab').onclick=()=>setMode('gallery');$('search-toggle').onclick=()=>{$('message-search').hidden=!$('message-search').hidden;if(!$('message-search').hidden)$('message-query').focus();};$('message-search').onsubmit=e=>{e.preventDefault();epoch++;contextMode=false;autoHistoryArmed=false;$('return-latest').hidden=true;query=$('message-query').value.trim();messages.clear();before=null;loadMessages();};$('clear-search').onclick=()=>{epoch++;contextMode=false;autoHistoryArmed=false;$('return-latest').hidden=true;query='';$('message-query').value='';messages.clear();before=null;loadMessages();};
$('logout').onclick=async()=>{try{await api('logout',{});forgetAccounts();forgetResume();location.reload();}catch(e){notice(e.message);}};$('revoke').onclick=async()=>{if(!confirm('¿Revocar tu enlace y cerrar todas tus sesiones del panel? Las cuentas vinculadas no se desconectarán.'))return;try{await api('revoke',{});locked('Acceso revocado. Solicita otro enlace desde /menu en Telegram.');}catch(e){notice(e.message);}};
function resetComposer(){cancelBatch();$('batch-toolbar').hidden=true;scheduleDraft=null;$('schedule-compose').textContent='◷ Programar';$('compose-tools').hidden=true;replyDraft=null;attachedFile=null;sendRequest=null;capabilities=null;$('compose-text').value='';$('attachment').value='';$('attached').hidden=true;$('reply-draft').hidden=true;$('composer').hidden=true;}
function updateSendButton(){const key=attachedFile?({photo:'photos',video:'videos',audio:'audio',voice:'voices',document:'documents'}[$('attachment-kind').value]):'text';$('send-message').disabled=operationBusy||!capabilities?.send[key];$('compose-text').disabled=!capabilities?.send.text&&!attachedFile;}
function autoSize(){$('compose-text').style.height='auto';$('compose-text').style.height=Math.min(140,$('compose-text').scrollHeight)+'px';}
async function loadCapabilities(){if(!chat)return;const chosen=epoch;try{const data=await api('permissions?account='+account.id+'&peer='+encodeURIComponent(chat.handle));if(chosen!==epoch)return;capabilities=data;$('composer').hidden=false;$('compose-tools').hidden=false;$('manage-chat').hidden=false;updateSendButton();$('permission-note').textContent=data.role+(data.send.text?' · Cuenta: '+account.name:' · Telegram no permite publicar con esta cuenta');}catch(e){if(chosen===epoch)notice(e.message);}}
function actionId(){const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);return Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');}
async function change(action,extra={},request=actionId()){if(operationBusy)throw Error('Hay una operación en curso.');if(!chat)throw Error('Selecciona una conversación.');operationBusy=true;$('send-message').disabled=true;const chosen=epoch,body={account:account.id,peer:chat.handle,action,request,topic:topic||undefined,...extra};try{const result=await api('action',body);if(chosen===epoch){if(action==='delete')messages.delete(extra.message);notice('✓ Operación completada en Telegram.');}return result;}finally{operationBusy=false;updateSendButton();pumpThumbs();pumpProfiles();}}
async function refreshAfterChange(){if(!chat)return;if(contextMode&&contextFocus)return jumpToMessage(contextFocus,true);await loadMessages(false,true);}
let lastTyping=0;$('compose-text').oninput=()=>{sendRequest=null;autoSize();if(chat&&!operationBusy&&Date.now()-lastTyping>8000){lastTyping=Date.now();api('action',{action:'typing',account:account.id,peer:chat.handle,topic:topic||undefined}).catch(()=>{});}};$('cancel-reply').onclick=()=>{replyDraft=null;sendRequest=null;$('reply-draft').hidden=true;};
$('attachment').onchange=()=>{const file=$('attachment').files[0];if(!file)return;attachedFile=file;sendRequest=null;$('attached-name').textContent=file.name;$('attachment-kind').value=file.type.startsWith('image/')?'photo':file.type.startsWith('video/')?'video':file.type.startsWith('audio/')?'audio':'document';$('attached').hidden=false;updateSendButton();};$('remove-attachment').onclick=()=>{attachedFile=null;sendRequest=null;$('attachment').value='';$('attached').hidden=true;updateSendButton();};$('attachment-kind').onchange=()=>{sendRequest=null;updateSendButton();};
$('composer').onsubmit=async event=>{event.preventDefault();if(operationBusy||!chat)return;const text=$('compose-text').value;if(!text.trim()&&!attachedFile)return;sendRequest=sendRequest||actionId();const chosen=epoch;try{if(attachedFile){operationBusy=true;$('send-message').disabled=true;notice('Enviando '+attachedFile.name+'…');const form=new FormData();form.set('data',JSON.stringify({account:account.id,peer:chat.handle,action:'upload',request:sendRequest,text,reply:replyDraft?.id||(topic>1?topic:undefined),kind:$('attachment-kind').value,schedule:scheduleDraft,format:$('compose-format').value}));form.set('file',attachedFile,attachedFile.name);await api('upload',form,true);operationBusy=false;}else await change('send',{text,reply:replyDraft?.id||(topic>1?topic:undefined),schedule:scheduleDraft,format:$('compose-format').value},sendRequest);if(chosen===epoch){const scheduled=!!scheduleDraft;scheduleDraft=null;$('schedule-compose').textContent='◷ Programar';$('compose-text').value='';attachedFile=null;replyDraft=null;sendRequest=null;$('attachment').value='';$('attached').hidden=true;$('reply-draft').hidden=true;autoSize();if(contextMode){epoch++;contextMode=false;contextFocus=null;messages.clear();before=null;$('return-latest').hidden=true;}await refreshAfterChange();$('thread').scrollTop=$('thread').scrollHeight;notice(scheduled?'✓ Mensaje programado en Telegram.':'✓ Enviado con '+account.name+'.');}}catch(e){notice(e.message);}finally{operationBusy=false;updateSendButton();pumpThumbs();pumpProfiles();}};
$('close-message-actions').onclick=()=>$('message-actions').close();$('close-management').onclick=()=>$('management').close();
function option(parent,label,fn,danger=false){const button=el('button',label);if(danger)button.className='danger';button.onclick=async()=>{try{await fn();}catch(e){notice(e.message);}};parent.append(button);return button;}
function messageOptions(message){const box=$('message-options');box.replaceChildren();const close=()=>$('message-actions').close();option(box,'Reaccionar',()=>{close();reactionDialog(message);});option(box,'↩ Responder',()=>{close();replyDraft=message;sendRequest=null;$('reply-draft-text').textContent='Respuesta: '+(message.text||labels[message.media?.type]||'Mensaje').slice(0,120);$('reply-draft').hidden=false;$('compose-text').focus();});if(!message.protected&&message.text)option(box,'Copiar texto',async()=>{await navigator.clipboard.writeText(message.text);close();});if(!message.protected)option(box,'Reenviar',()=>{close();forwardDialog(message);});if(message.out||capabilities?.rights.edit_messages)option(box,'Editar texto / comentario',()=>{close();editDialog(message);});if(message.out||chat.type==='private'||capabilities?.rights.delete_messages)option(box,'Eliminar para todos',async()=>{if(!confirm('¿Eliminar este mensaje para todos en '+chat.name+'?'))return;await change('delete',{message:message.id});close();await refreshAfterChange();},true);if(chat.type==='private'||capabilities?.rights.pin_messages){option(box,'Fijar mensaje',async()=>{if(!confirm('¿Fijar este mensaje en '+chat.name+'?'))return;await change('pin',{message:message.id});close();});option(box,'Desfijar mensaje',async()=>{await change('unpin',{message:message.id});close();});}if(panelAdmin&&chat.type!=='private'&&!message.out)option(box,'🚩 Reportar a Telegram',()=>{close();reportMessageDialog(message);});$('message-actions').showModal();}
async function reportMessageDialog(m){
 if(!panelAdmin||!account||!chat||chat.type==='private'||m.out)return;
 const selectedAccount=account,selectedChat=chat;
 if(!confirm('¿Iniciar un reporte sobre este mensaje desde la cuenta '+selectedAccount.name+'? Solo Telegram decide si infringe sus normas.'))return;
 const box=management('🚩 Reportar a Telegram');
 box.append(el('p','Consultando los motivos oficiales de Telegram…'));
 let busy=false;
 const stillHere=()=>$('management').open&&account===selectedAccount&&chat===selectedChat;
 async function next(token,comment='',skip=false){
  if(busy||!stillHere())return;
  busy=true;
  try{const result=await api('report-step',{account:selectedAccount.id,peer:selectedChat.handle,token,comment,skip});paint(result);}
  catch(e){notice(e.message);}
  finally{busy=false;}
 }
 function paint(result){
  if(!stillHere())return;
  box.replaceChildren();
  if(result.kind==='reported'){
   box.append(el('p','✓ Telegram confirmó que recibió el reporte de este mensaje. Su equipo decidirá qué medidas tomar.'));
   notice('✓ Reporte recibido por Telegram desde '+selectedAccount.name+'.');
   return;
  }
  if(result.kind==='choose'){
   box.append(el('p',result.title||'Selecciona el motivo que corresponda.'));
   box.append(el('small','Cuenta que reporta: '+selectedAccount.name+' · Grupo o canal: '+selectedChat.name));
   for(const choice of result.choices||[]){
    actionButton(box,choice.text,()=>next(choice.token));
   }
   return;
  }
  if(result.kind==='comment'){
   box.append(el('p',result.optional?'Telegram permite añadir un comentario opcional.':'Telegram solicita una explicación antes de enviar el reporte.'));
   const field=input(box,'Comentario para Telegram','','textarea');field.maxLength=1024;
   actionButton(box,'Enviar reporte a Telegram',async()=>{
    if(!result.optional&&!field.value.trim())throw Error('Escribe el comentario solicitado por Telegram.');
    if(!confirm('¿Confirmas enviar este reporte a Telegram con la cuenta '+selectedAccount.name+'?'))return;
    await next(result.token,field.value);
   });
   if(result.optional)actionButton(box,'Continuar sin comentario',async()=>{
    if(!confirm('¿Confirmas enviar este reporte a Telegram con la cuenta '+selectedAccount.name+'?'))return;
    await next(result.token,'',true);
   });
   return;
  }
  box.append(el('p','Telegram no devolvió una respuesta válida. Cancela e inténtalo desde el mensaje.'));
 }
 try{const result=await api('report-begin',{account:selectedAccount.id,peer:selectedChat.handle,message:m.id});paint(result);}
 catch(e){if(stillHere())box.replaceChildren(el('p','No se pudieron consultar los motivos de Telegram.'));notice(e.message);}
}
function management(title){const status=$('management').querySelector('.dialog-status');if(status){status.textContent='';status.hidden=true;}$('management-title').textContent=title;$('management-source-id').hidden=true;$('management-source-id').textContent='';$('management-body').replaceChildren();$('management').showModal();return $('management-body');}
function input(box,label,value='',type='input'){const l=el('label',label),field=el(type);field.value=value;field.setAttribute('aria-label',label);l.append(field);box.append(l);return field;}
function actionButton(box,label,fn){const b=option(box,label,fn);b.className='primary';return b;}
function editDialog(m){const box=management('Editar mensaje'),field=input(box,'Texto o comentario',m.text,'textarea');actionButton(box,'Guardar edición',async()=>{await change('edit',{message:m.id,text:field.value});$('management').close();await refreshAfterChange();});}
function forwardDialog(m){const box=management('Reenviar mensaje'),select=input(box,'Conversación de destino','','select');for(const c of rows){const o=el('option',c.name);o.value=c.handle;select.append(o);}actionButton(box,'Reenviar',async()=>{const title=select.selectedOptions[0]?.textContent;if(!confirm('¿Reenviar este mensaje a '+title+' con '+account.name+'?'))return;await change('forward',{message:m.id,destination:select.value});$('management').close();});}
async function manageChat(){if(!chat)return;const box=management(chat.name);if(panelAdmin&&chat.type!=='private'){$('management-source-id').textContent='ID: '+chat.id;$('management-source-id').hidden=false;}box.append(el('p','Consultando permisos actuales…'));const info=await api('permissions?account='+account.id+'&peer='+encodeURIComponent(chat.handle));capabilities=info;box.replaceChildren(el('p','Cuenta: '+account.name+' · '+info.role));actionButton(box,'Marcar conversación como leída',async()=>{if(!confirm('¿Marcar este chat como leído con '+account.name+'?'))return;await change('read',{message:Math.max(...messages.keys(),0)||undefined});$('management').close();});actionButton(box,'Mensajes programados',()=>scheduledDialog());if(!info.group)return;actionButton(box,'Propietario',()=>groupPeople('owner'));actionButton(box,'Administradores',()=>groupPeople('admins'));actionButton(box,'Miembros',()=>groupPeople('members'));if(info.rights.add_admins)actionButton(box,'➕ Nuevo administrador',()=>newAdminDialog());actionButton(box,'Consultar usuario / administrar miembro',()=>memberDialog());if(info.rights.change_info)actionButton(box,'Editar título y descripción',()=>groupInfoDialog(info));if(info.rights.ban_users&&!info.channel)actionButton(box,'Permisos de los miembros',()=>defaultsDialog(info));}
$('new-chat').onclick=()=>{if(!account)return;const box=management('Nuevo chat'),target=input(box,'Telegram ID o @username');actionButton(box,'Abrir conversación',async()=>{const result=await api('resolve',{account:account.id,target:target.value});if(!rows.some(r=>r.id===result.id))rows.unshift(result);$('management').close();await openChat(result);});};
$('manage-chat').onclick=()=>manageChat().catch(e=>notice(e.message));
function groupPeople(kind){const selected=chat,selectedAccount=account,title={owner:'Propietario',admins:'Administradores',members:'Miembros'}[kind];const box=management(title+' · '+selected.name),list=el('div'),status=el('p','Consultando Telegram…'),more=el('button','Cargar más');more.className='primary';more.hidden=true;box.append(status,list,more);let cursor='',busy=false;const seen=new Set();async function load(){if(busy)return;busy=true;more.disabled=true;try{const result=await api('member-list?account='+selectedAccount.id+'&peer='+encodeURIComponent(selected.handle)+'&kind='+kind+(cursor?'&cursor='+encodeURIComponent(cursor):''));if(chat!==selected||account!==selectedAccount||!list.isConnected)return;for(const person of result.items){if(seen.has(person.id))continue;seen.add(person.id);const row=el('div');row.className='member';row.append(el('strong',person.name),el('small',(person.username?'@'+person.username+' · ':'')+'ID: '+person.id+' · '+person.role+(person.bot?' · Bot':'')));option(row,'Consultar usuario',()=>memberDialog(person.id));list.append(row);}cursor=result.next||'';more.hidden=!cursor;status.textContent=!result.available?'No disponible: Telegram no permite consultar esta lista.':!seen.size&&!cursor?'No disponible.':cursor?'Lista parcial. Puedes cargar más.':'Lista disponible completada.';}catch(e){status.textContent='No disponible: '+e.message;}finally{busy=false;more.disabled=false;}}more.onclick=load;load();}
function newAdminDialog(){if(!capabilities?.rights.add_admins)return;const selected=chat,selectedAccount=account,info=capabilities;const box=management('➕ Nuevo administrador');box.append(el('p','Paso 1 de 2 · Escribe el Telegram ID o @usuario de la persona.'));const field=input(box,'Nuevo administrador');actionButton(box,'Continuar',async()=>{const target=field.value.trim();if(!target)return notice('Escribe un Telegram ID o @usuario.');const result=await api('user-info?account='+selectedAccount.id+'&peer='+encodeURIComponent(selected.handle)+'&target='+encodeURIComponent(target));if(chat!==selected||account!==selectedAccount)return;if(result.user.role==='Propietario')return notice('Esta persona ya es propietaria.');const details=management('Permisos del nuevo administrador');details.append(el('p','Paso 2 de 2 · '+result.user.name+' · '+result.user.id),el('p','Grupo o canal: '+selected.name));const checks=[];if(info.basic)details.append(el('p','Telegram asignará los permisos de administrador del grupo básico.'));else{details.append(el('p','Marca los permisos que quieres conceder:'));const grid=el('div');grid.className='rights-grid';for(const right of info.grant){const label=el('label'),check=el('input');check.type='checkbox';check.value=right.flag;label.append(check,document.createTextNode(right.label));grid.append(label);checks.push(check);}details.append(grid);}actionButton(details,'✅ Confirmar nuevo administrador',async()=>{if(chat!==selected||account!==selectedAccount)return;const rights=info.basic?info.grant.map(r=>r.flag):checks.filter(c=>c.checked).map(c=>c.value);if(!info.basic&&!rights.length)return notice('Marca al menos un permiso.');if(!confirm('¿Nombrar administrador a '+result.user.name+' ('+result.user.id+') en '+selected.name+' con la cuenta '+selectedAccount.name+'?'))return;await change('promote',{target:result.user.id,rights,rank:''});$('management').close();notice('Administrador añadido correctamente.');});actionButton(details,'Atrás',()=>newAdminDialog());});}
function memberDialog(target=''){const box=management('Usuario / administrador'),field=input(box,'Telegram ID o @username',target),note=el('p');box.append(note);actionButton(box,'Consultar usuario',async()=>{const result=await api('user-info?account='+account.id+'&peer='+encodeURIComponent(chat.handle)+'&target='+encodeURIComponent(field.value));note.textContent=result.user.name+' · '+result.user.role+' · '+result.user.id;for(const check of checks)check.checked=result.user.rights.includes(check.value);rank.value=result.user.rank||'';});const rights=el('div');rights.className='rights-grid';const checks=[];if(capabilities?.rights.add_admins){rights.append(el('p',capabilities.basic?'Grupo básico: Telegram concede el cargo de administrador con sus permisos conjuntos.':'Permisos que otorgarás:'));for(const r of (capabilities.basic?[]:capabilities.grant)){const label=el('label'),check=el('input');check.type='checkbox';check.value=r.flag;label.append(check,document.createTextNode(r.label));rights.append(label);checks.push(check);}box.append(rights);}const rank=input(box,'Etiqueta de administrador (opcional)');rank.maxLength=16;const apply=async action=>{if(!confirm('¿Aplicar '+({promote:'los permisos de administrador',demote:'la retirada de administrador',ban:'el baneo',unban:'el desbloqueo',restrict:'la restricción para escribir',unrestrict:'la retirada de restricciones',invite:'la invitación'}[action])+' a '+field.value+' en '+chat.name+' con '+account.name+'?'))return;await change(action,{target:field.value,rights:capabilities.basic?capabilities.grant.map(r=>r.flag):checks.filter(c=>c.checked).map(c=>c.value),rank:rank.value});$('management').close();};if(capabilities?.rights.add_admins){actionButton(box,'Añadir / actualizar administrador',()=>apply('promote'));actionButton(box,'Retirar administrador',()=>apply('demote'));}if(capabilities?.rights.ban_users){for(const [action,label]of [['ban','Expulsar / banear'],['unban','Desbanear'],['restrict','Restringir escritura'],['unrestrict','Quitar restricciones']]){if(capabilities.basic&&action!=='ban')continue;actionButton(box,label,()=>apply(action));}}actionButton(box,'Invitar usuario',()=>apply('invite'));}
async function groupInfoDialog(info){const box=management('Editar grupo / canal');box.append(el('p','Cargando información actual…'));const current=await api('group-details?account='+account.id+'&peer='+encodeURIComponent(chat.handle));box.replaceChildren();const title=input(box,'Título',current.name),about=input(box,'Descripción',current.about||'','textarea');actionButton(box,'Guardar información',async()=>{if(!confirm('¿Actualizar título y descripción de '+chat.name+'?'))return;await change('group-info',{title:title.value,about:about.value});chat.name=title.value;$('chat-title').textContent=title.value;$('management').close();});}
function defaultsDialog(info){const box=management('Permisos de los miembros'),checks=[];for(const r of info.defaults){const label=el('label'),check=el('input');check.type='checkbox';check.value=r.flag;check.checked=r.allowed;label.append(check,document.createTextNode(r.label));box.append(label);checks.push(check);}actionButton(box,'Aplicar permisos',async()=>{if(!confirm('¿Aplicar estos permisos a los miembros de '+chat.name+'?'))return;await change('defaults',{allowed:checks.filter(c=>c.checked).map(c=>c.value)});$('management').close();});}
window.addEventListener('hashchange',()=>{if(location.hash)location.reload();});document.addEventListener('visibilitychange',()=>{if(!document.hidden&&chat&&!modal&&Date.now()>retryAt)loadMessages(false,true);});
async function loadOlder(){if(olderLoading||polling||!before||operationBusy)return;olderLoading=true;autoHistoryArmed=false;$('older').disabled=true;$('older').textContent='Cargando historial…';try{await loadMessages(true);}finally{olderLoading=false;$('older').disabled=false;tabs();}}
let previousScrollTop=0,touchScrolling=false,scrollIdleAt=0;
$('thread').addEventListener('touchstart',()=>{touchScrolling=true;},{passive:true});
for(const event of ['touchend','touchcancel'])$('thread').addEventListener(event,()=>{touchScrolling=false;scrollIdleAt=Date.now()+350;},{passive:true});
$('thread').addEventListener('scroll',()=>{const thread=$('thread');const moved=mode==='gallery'?thread.scrollTop>previousScrollTop:thread.scrollTop<previousScrollTop;if(moved)autoHistoryArmed=true;previousScrollTop=thread.scrollTop;scrollIdleAt=Date.now()+350;if(!autoHistoryArmed||document.hidden||!chat||olderLoading)return;const edge=mode==='gallery'?thread.scrollHeight-thread.scrollTop-thread.clientHeight<220:thread.scrollTop<180;if(edge)loadOlder();},{passive:true});
$('older').onclick=loadOlder;
function captureAnchor(){const root=$('thread'),top=root.getBoundingClientRect().top;const node=[...root.querySelectorAll('[data-message]')].find(n=>n.getBoundingClientRect().bottom>top);return node?{id:node.dataset.message,offset:node.getBoundingClientRect().top-top}:null;}
function restoreAnchor(anchor){if(!anchor)return false;const root=$('thread'),node=root.querySelector('[data-message="'+anchor.id+'"]');if(!node)return false;root.scrollTop+=node.getBoundingClientRect().top-root.getBoundingClientRect().top-anchor.offset;return true;}
async function jumpToMessage(id,force=false){if(operationBusy)return;const known=$('thread').querySelector('[data-message="'+Number(id)+'"]');if(mode==='messages'&&known&&!force){known.scrollIntoView({block:'center'});known.classList.add('highlight');setTimeout(()=>known.classList.remove('highlight'),2200);return;}const chosen=++epoch;mode='messages';query='';contextMode=true;contextFocus=id;autoHistoryArmed=false;tabs();notice('Cargando mensaje original…');const result=await api('context?account='+account.id+'&peer='+encodeURIComponent(chat.handle)+'&message='+id);if(chosen!==epoch)return;messages.clear();for(const m of result.items)messages.set(m.id,{...m,signature:JSON.stringify(m)});before=result.next;renderMessages();$('older').hidden=!before;$('return-latest').hidden=false;const node=$('thread').querySelector('[data-message="'+Number(id)+'"]');if(node){node.scrollIntoView({block:'center'});node.classList.add('highlight');setTimeout(()=>node.classList.remove('highlight'),2200);}notice('');}
$('return-latest').onclick=async()=>{epoch++;contextMode=false;autoHistoryArmed=false;messages.clear();before=null;$('return-latest').hidden=true;await loadMessages();};
async function loadFolders(){const chosen=epoch;folder=null;folders=[];$('folder-select').replaceChildren(new Option('Todas las carpetas',''));try{const result=await api('folders?account='+account.id);if(chosen!==epoch)return;folders=result.items;for(const f of folders)$('folder-select').append(new Option(f.title,String(f.id)));}catch(e){if(chosen===epoch)notice(e.message);}}
function folderMatches(c){if(!folder)return true;if(folder.exclude.includes(c.id))return false;if(folder.include.includes(c.id))return true;if(folder.exclude_read&&!c.unread||folder.exclude_archived&&c.archived||folder.exclude_muted&&c.muted)return false;return c.type==='group'?folder.groups:c.type==='channel'?folder.broadcasts:c.bot?folder.bots:c.contact?folder.contacts:folder.non_contacts;}
$('folder-select').onchange=()=>{folder=folders.find(f=>String(f.id)===$('folder-select').value)||null;renderChats();};
async function loadTopics(append=false){const chosen=epoch;const data=await api('topics?account='+account.id+'&peer='+encodeURIComponent(chat.handle)+(append&&topicNext?'&cursor='+encodeURIComponent(topicNext):''));if(chosen!==epoch)return;if(!append)$('topic-select').replaceChildren(new Option('Todos los temas','0'));for(const t of data.items)$('topic-select').append(new Option(t.title+(t.closed?' · cerrado':''),String(t.id)));topicNext=data.next;$('more-topics').hidden=!topicNext;$('topic-bar').hidden=false;}
$('more-topics').onclick=()=>loadTopics(true).catch(e=>notice(e.message));
$('topic-select').onchange=async()=>{epoch++;topic=Number($('topic-select').value);contextMode=false;autoHistoryArmed=false;messages.clear();before=null;await loadMessages();};
$('global-search').onclick=()=>{const box=management('Buscar en todos los chats'),field=input(box,'Buscar mensajes'),results=el('div');let next=null,needle='',busy=false;const more=el('button','Más resultados');more.hidden=true;const run=async append=>{if(busy)return;busy=true;try{if(!append){needle=field.value.trim();next=null;results.replaceChildren();}const data=await api('global-search?account='+account.id+'&q='+encodeURIComponent(needle)+(append&&next?'&cursor='+encodeURIComponent(next):''));for(const hit of data.items){const button=el('button');button.className='search-hit';button.append(el('strong',hit.chat.name),el('span',hit.message.text||labels[hit.message.media?.type]||'Mensaje'),el('small',date(hit.message.date)+' '+time(hit.message.date)));button.onclick=async()=>{try{$('management').close();await openChat(hit.chat);await jumpToMessage(hit.message.id);}catch(e){notice(e.message);}};results.append(button);}next=data.next;more.hidden=!next;if(!data.items.length&&!append)results.append(el('p','Sin resultados disponibles.'));}catch(e){notice(e.message);}finally{busy=false;}};actionButton(box,'Buscar',()=>run(false));box.append(results,more);more.onclick=()=>run(true);field.onkeydown=e=>{if(e.key==='Enter')run(false);};};
function reactionDialog(m){const box=management('Reaccionar al mensaje'),grid=el('div');grid.className='emoji-grid';for(const emoji of ['👍','❤️','🔥','🥰','👏','😁','🤔','🤯','😢','🎉','🤩','💯','👎','😱','🙏'])option(grid,emoji,async()=>{await change('reaction',{message:m.id,emoji});$('management').close();await refreshAfterChange();});box.append(grid);actionButton(box,'Quitar mi reacción',async()=>{await change('reaction',{message:m.id,emoji:''});$('management').close();await refreshAfterChange();});}
$('emoji-picker').onclick=()=>{const box=management('Emojis'),grid=el('div');grid.className='emoji-grid';for(const emoji of ['😀','😃','😄','😁','😊','🥰','😍','😘','😎','🤔','😭','😂','😅','🙌','👍','👏','🙏','❤️','💙','💚','🔥','✨','🎉','💯','✅','👋','🤝','📷','🎬','🇨🇺'])option(grid,emoji,()=>{const field=$('compose-text'),start=field.selectionStart,end=field.selectionEnd;field.value=field.value.slice(0,start)+emoji+field.value.slice(end);sendRequest=null;autoSize();$('management').close();field.focus();});box.append(grid);};
async function mediaLibrary(kind){const box=management(kind==='stickers'?'Stickers recientes':'GIF guardados');box.append(el('p','Cargando biblioteca de esta cuenta…'));const chosen=epoch,data=await api('library?account='+account.id+'&kind='+kind);if(chosen!==epoch||!$('management').open)return;box.replaceChildren();const grid=el('div');grid.className='library-grid';for(const doc of data.items){const button=el('button');button.setAttribute('aria-label','Enviar '+(doc.label||'archivo'));lazyImage(button,base+'/api/library-thumb?account='+account.id+'&document='+encodeURIComponent(doc.handle),doc.label||'Archivo');button.append(el('span',doc.label));button.onclick=async()=>{try{if(!confirm('¿Enviar este '+(kind==='stickers'?'sticker':'GIF')+' a '+chat.name+' con '+account.name+'?'))return;await change('saved-media',{document:doc.handle});$('management').close();await refreshAfterChange();}catch(e){notice(e.message);}};grid.append(button);}box.append(grid);if(!data.items.length)box.append(el('p',kind==='stickers'?'No hay stickers recientes en esta cuenta.':'No hay GIF guardados en esta cuenta.'));}
$('sticker-picker').onclick=()=>mediaLibrary('stickers').catch(e=>notice(e.message));$('gif-picker').onclick=()=>mediaLibrary('gifs').catch(e=>notice(e.message));
$('poll-create').onclick=()=>{const box=management('Crear encuesta'),question=input(box,'Pregunta'),answers=input(box,'Respuestas (una por línea)','','textarea');const label=el('label'),multiple=el('input');multiple.type='checkbox';label.append(multiple,document.createTextNode('Permitir varias respuestas'));box.append(label);actionButton(box,'Enviar encuesta',async()=>{if(!confirm('¿Enviar esta encuesta a '+chat.name+' con '+account.name+'?'))return;await change('poll',{question:question.value,answers:answers.value.split('\n').map(x=>x.trim()).filter(Boolean),multiple:multiple.checked});$('management').close();await refreshAfterChange();});};
$('schedule-compose').onclick=()=>{const box=management('Programar envío'),dateField=input(box,'Fecha y hora (hora de tu teléfono)');dateField.type='datetime-local';if(scheduleDraft){const d=new Date(scheduleDraft);dateField.value=new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}actionButton(box,'Usar esta fecha',()=>{const d=new Date(dateField.value);if(!Number.isFinite(d.getTime())||d.getTime()<=Date.now())throw Error('Selecciona una fecha futura.');scheduleDraft=d.toISOString();sendRequest=null;$('schedule-compose').textContent='◷ '+d.toLocaleString('es');$('management').close();});actionButton(box,'Quitar programación',()=>{scheduleDraft=null;sendRequest=null;$('schedule-compose').textContent='◷ Programar';$('management').close();});actionButton(box,'Ver mensajes programados',()=>scheduledDialog());};
async function scheduledDialog(){const box=management('Mensajes programados');box.append(el('p','Consultando Telegram…'));const data=await api('scheduled?account='+account.id+'&peer='+encodeURIComponent(chat.handle));box.replaceChildren();for(const m of data.items){const row=el('div');row.className='scheduled-item';row.append(el('strong',new Date(m.date*1000).toLocaleString('es')),el('p',m.text||labels[m.media?.type]||'Mensaje'));option(row,'Enviar ahora',async()=>{if(!confirm('¿Enviar ahora este mensaje con '+account.name+'?'))return;await change('scheduled-send',{message:m.id});await scheduledDialog();await refreshAfterChange();});option(row,'Cancelar envío',async()=>{if(!confirm('¿Eliminar este mensaje programado?'))return;await change('scheduled-delete',{message:m.id});await scheduledDialog();},true);box.append(row);}if(!data.items.length)box.append(el('p','No hay mensajes programados en este chat.'));}

let liveSource=null,liveSerial=0,liveTimer=null,liveDirty=false,liveIds=new Set(),liveDeletes=new Set(),liveResync=false,typingTimer=null;
function liveState(text){$('live-state').textContent=text;$('conversation-live-state').textContent=text;}
function stopLive(){liveSerial++;if(liveSource)liveSource.close();liveSource=null;clearTimeout(liveTimer);liveTimer=null;liveDirty=false;liveIds.clear();liveDeletes.clear();liveResync=false;}
function visibleMessageIds(){const root=$('thread'),rect=root.getBoundingClientRect();return [...root.querySelectorAll('[data-message]')].filter(n=>{const b=n.getBoundingClientRect();return b.bottom>=rect.top&&b.top<=rect.bottom;}).slice(0,60).map(n=>Number(n.dataset.message));}
function scheduleLiveRefresh(){if(!liveTimer)liveTimer=setTimeout(()=>{liveTimer=null;flushLive().catch(e=>notice(e.message));},70);}
async function applyLiveChanges(ids){if(!chat||!ids.length)return;const chosen=epoch,handle=chat.handle;const r=await api('changes?account='+account.id+'&peer='+encodeURIComponent(handle)+'&ids='+ids.join(','));if(chosen!==epoch||chat?.handle!==handle)return;const anchor=captureAnchor();let changed=false;for(const id of r.deleted)changed=messages.delete(id)||changed;for(const m of r.items){if(!messages.has(m.id))continue;const signature=JSON.stringify(m);if(messages.get(m.id).signature!==signature){messages.set(m.id,{...m,signature});changed=true;}}if(changed){renderMessages();restoreAnchor(anchor);}}
async function flushLive(){if(!account||document.hidden)return;if(operationBusy||polling||listPolling||touchScrolling||Date.now()<scrollIdleAt){liveTimer=setTimeout(()=>{liveTimer=null;flushLive().catch(e=>notice(e.message));},200);return;}const changed=[...liveIds].filter(id=>messages.has(id)).slice(0,100),deleted=[...liveDeletes],refresh=liveDirty,resync=liveResync;liveDirty=false;liveResync=false;for(const id of changed)liveIds.delete(id);liveDeletes.clear();if(chat){if(deleted.length){const anchor=captureAnchor();let modified=false;for(const id of deleted)modified=messages.delete(id)||modified;if(modified){renderMessages();restoreAnchor(anchor);}}if(resync)for(const id of visibleMessageIds())if(!changed.includes(id))changed.push(id);if(changed.length)await applyLiveChanges(changed.slice(0,100));if(refresh&&!contextMode)await loadMessages(false,true);else if(mode==='gallery'&&(deleted.length||changed.length))loadGalleryCount();}else if(refresh){listPolling=true;try{await loadChats(false,true);}finally{listPolling=false;}}if(liveIds.size||liveDirty||liveDeletes.size||liveResync)scheduleLiveRefresh();}
function startLive(){stopLive();if(!account||document.hidden)return;const serial=liveSerial;if(!window.EventSource){liveState('Este navegador no admite actualizaciones en vivo.');return;}const stream=new EventSource(base+'/api/events?account='+account.id+(chat?'&peer='+encodeURIComponent(chat.handle):''));liveSource=stream;liveState('Conectando actualizaciones…');stream.onerror=()=>{if(liveSource!==stream)return;liveState('Reconectando actualizaciones…');if(stream.readyState===EventSource.CLOSED)setTimeout(async()=>{if(liveSource!==stream)return;try{await api('me');if(liveSource===stream)startLive();}catch{}},1500);};stream.onmessage=event=>{if(liveSource!==stream||serial!==liveSerial)return;let data;try{data=JSON.parse(event.data);}catch{return;}if(data.kind==='reconnect'){stopLive();setTimeout(startLive,1200);return;}if(data.kind==='state'){liveState(data.state==='live'?'● En tiempo real':data.state==='busy'?'Actualizaciones en pausa durante la transferencia':'Reconectando actualizaciones…');return;}if(data.kind==='revoked'){stopLive();locked('El acceso a esta cuenta fue revocado.');return;}if(data.kind==='typing'){if(chat){clearTimeout(typingTimer);$('chat-subtitle').textContent=data.active?'Escribiendo…':account.name;if(data.active)typingTimer=setTimeout(()=>{if(chat)$('chat-subtitle').textContent=account.name;},6000);}return;}if(data.kind==='status'){if(chat&&data.status)$('chat-subtitle').textContent=data.status+' · '+account.name;return;}if(data.kind==='read'){const anchor=captureAnchor();let changed=false;for(const m of messages.values())if(m.out&&m.id<=data.max&&m.delivery!=='read'){m.delivery='read';m.signature=JSON.stringify({...m,signature:undefined});changed=true;}if(changed){renderMessages();restoreAnchor(anchor);}return;}if(data.kind==='delete'){for(const id of data.ids||[])liveDeletes.add(id);}else if(data.kind==='edit'||data.kind==='reaction'){for(const id of data.ids||[])liveIds.add(id);}else{liveDirty=true;if(data.kind==='resync')liveResync=true;}scheduleLiveRefresh();};}
document.addEventListener('visibilitychange',()=>{if(document.hidden)stopLive();else startLive();});window.addEventListener('pagehide',stopLive);

const premiumNodes=new Set(),premiumCache=new Map();let premiumBytes=0,lottieReady=null,premiumActive=0,premiumQueue=[];
function loadLottie(){if(window.lottie)return Promise.resolve(window.lottie);if(!lottieReady)lottieReady=new Promise((resolve,reject)=>{const script=el('script');script.src=base+'/assets/lottie-light.js';script.onload=()=>{window.lottie.setQuality('medium');resolve(window.lottie);};script.onerror=()=>{lottieReady=null;script.remove();reject(Error('No se pudo cargar el reproductor de emojis.'));};document.head.append(script);});return lottieReady;}
function disposePremium(){for(const node of premiumNodes){premiumObserver.unobserve(node);if(node.player)node.player.destroy();}premiumNodes.clear();premiumQueue=premiumQueue.filter(n=>n.isConnected);}
function premiumSpan(documentId,alt,messageId){const span=el('span',alt);span.className='premium-emoji';span.setAttribute('role','img');span.setAttribute('aria-label',alt||'Emoji premium');span.dataset.document=String(documentId);span.dataset.url=base+'/api/emoji?account='+account.id+'&peer='+encodeURIComponent(chat.handle)+'&message='+messageId+'&document='+documentId;span.premiumEpoch=epoch;premiumNodes.add(span);premiumObserver.observe(span);return span;}
async function premiumAsset(node){const key=node.dataset.document;if(premiumCache.has(key))return premiumCache.get(key).value;const r=await network(node.dataset.url,{cache:'no-store'});if(!r.ok)throw Error('Emoji no disponible.');const mime=r.headers.get('Content-Type')||'';let value,size;if(mime.includes('application/json')){const data=await r.json();if(data.format!=='lottie')throw Error('Animación no válida.');value={format:'lottie',animation:data.animation};size=JSON.stringify(data).length*2;}else{const blob=await r.blob();value={format:'image',url:URL.createObjectURL(blob)};size=blob.size;}premiumCache.set(key,{value,size});premiumBytes+=size;while((premiumCache.size>128||premiumBytes>12*1024*1024)&&premiumCache.size>1){const [key,old]=premiumCache.entries().next().value;premiumCache.delete(key);premiumBytes-=old.size;if(old.value.url)URL.revokeObjectURL(old.value.url);}return value;}
async function pumpPremium(){if(premiumActive>=2||!premiumQueue.length||document.hidden)return;const node=premiumQueue.shift();if(!node.isConnected){pumpPremium();return;}premiumActive++;try{const asset=await premiumAsset(node);if(!node.isConnected)return;if(asset.format==='lottie'){const lottie=await loadLottie();if(!node.isConnected)return;node.replaceChildren();node.player=lottie.loadAnimation({container:node,renderer:'svg',loop:true,autoplay:node.inViewport&&!window.matchMedia('(prefers-reduced-motion: reduce)').matches,animationData:structuredClone(asset.animation),rendererSettings:{progressiveLoad:true,hideOnTransparent:true}});}else{const img=el('img');img.alt=node.getAttribute('aria-label');img.src=asset.url;img.draggable=false;node.replaceChildren(img);}}catch{node.title='El emoji no está disponible; se muestra su alternativa.';}finally{node.premiumLoading=false;premiumActive--;pumpPremium();}}
const premiumObserver=new IntersectionObserver(entries=>{for(const item of entries){const node=item.target;node.inViewport=item.isIntersecting;if(node.player){if(item.isIntersecting&&!document.hidden&&!window.matchMedia('(prefers-reduced-motion: reduce)').matches)node.player.play();else node.player.pause();}else if(item.isIntersecting&&!node.premiumLoading&&!node.querySelector('img,svg')){node.premiumLoading=true;premiumQueue.push(node);pumpPremium();}}},{rootMargin:'30px'});
function activatePremium(){if(document.hidden)return;const box=$('thread').getBoundingClientRect();for(const node of premiumNodes){const rect=node.getBoundingClientRect();if(rect.bottom<box.top||rect.top>box.bottom||!rect.width)continue;node.inViewport=true;if(!node.player&&!node.premiumLoading&&!node.querySelector('img,svg')){node.premiumLoading=true;premiumQueue.push(node);}}pumpPremium();}
$('thread').addEventListener('scroll',activatePremium,{passive:true});
document.addEventListener('visibilitychange',()=>{for(const node of premiumNodes)if(node.player){if(document.hidden)node.player.pause();else if(node.inViewport&&!window.matchMedia('(prefers-reduced-motion: reduce)').matches)node.player.play();}if(!document.hidden)pumpPremium();});

$('compose-text').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('composer').requestSubmit();}});
const overviewAccounts=new Map(),accountCards=new Map();
let accountGroups=new Map(),accountTab='active',statusQueue=[],statusBusy=false,overviewRun=0,overviewSearching=false,overviewPending=[];
const accountStates={active:'Activa',revoked:'Sesión revocada',expired:'Sesión vencida',invalid:'Sin sesión válida',unknown:'Sin verificar'};
function statusGroup(state){return state==='active'?'active':'inactive';}
function buildAccountGroups(){accountGroups.clear();$('accounts').replaceChildren();for(const [key,title] of [['active','Cuentas activas'],['inactive','Cuentas no activas o sin verificar']]){const section=el('section'),heading=el('h2',title),list=el('div');section.className='account-group';section.append(heading,list);$('accounts').append(section);accountGroups.set(key,{section,heading,list,title});}updateAccountGroups();}
function updateAccountGroups(){
 const selected=panelReviewer?'active':accountTab,partial=accountsNext?'+':'';
 for(const [key,group] of accountGroups){
  const count=group.list.children.length;
  group.section.hidden=key!==selected;
  group.heading.textContent=group.title+(panelReviewer?'':' · '+count);
  const button=$('account-tab-'+key);
  button.dataset.selected=String(key===selected);
  button.setAttribute('aria-pressed',String(key===selected));
  $('account-count-'+key).textContent=String(count)+partial;
 }
 $('account-tab-inactive').hidden=panelReviewer;
 const note=$('account-tab-note'),parts=[];
 if(selected==='inactive')parts.push('Incluye cuentas sin verificar; no significa que su sesión esté revocada.');
 if(accountsNext)parts.push('Contadores parciales (+): puedes cargar más cuentas.');
 note.textContent=parts.join(' ');
 note.hidden=!parts.length;
}
function setAccountTab(value){
 if(value!=='active'&&value!=='inactive')return;
 if(panelReviewer&&value==='inactive')return;
 accountTab=value;updateAccountGroups();queueResume();
}
$('account-tab-active').onclick=()=>setAccountTab('active');
$('account-tab-inactive').onclick=()=>setAccountTab('inactive');
function updateAccountCard(a){const card=accountCards.get(a.id);if(!card)return;card.title.textContent=a.name;card.detail.replaceChildren(el('span','Telegram ID: '+a.id),el('span','Username: '+(a.username?'@'+a.username:'No disponible')),el('span','Teléfono: '+(a.phone||'No disponible')),el('span','País del teléfono: '+(a.country||'No disponible')));const state=a.status?.state||'unknown';card.wrapper.hidden=panelReviewer&&state!=='active';card.status.textContent=accountStates[state]||accountStates.unknown;card.status.dataset.state=state;card.status.title=a.status?.reason||'';card.checked.textContent=a.status?.checked?'Comprobada: '+new Date(a.status.checked*1000).toLocaleString('es'):(a.status?.reason||'Sin verificar');if(typeof updateAccountFavoriteStar==='function')updateAccountFavoriteStar(a);const group=accountGroups.get(statusGroup(state));if(card.wrapper.parentElement!==group.list)group.list.append(card.wrapper);updateAccountGroups();}
async function verifyAccount(a){const card=accountCards.get(a.id);if(!card)return;card.check.disabled=true;card.check.textContent='Comprobando…';try{const status=await api('account-status?account='+a.id);a.status=status;updateAccountCard(a);queueAccountCache();}catch(e){a.status={state:'unknown',reason:e.message,checked:0};updateAccountCard(a);queueAccountCache();}finally{card.check.disabled=false;card.check.textContent='Comprobar sesión';}}
async function pumpStatuses(){if(statusBusy||overviewSearching||operationBusy||!statusQueue.length||$('workspace').dataset.view!=='accounts')return;statusBusy=true;const a=statusQueue.shift();try{await verifyAccount(a);}finally{statusBusy=false;pumpStatuses();}}
function queueStatus(a){if(!a.statusQueued&&(!a.status?.checked||Date.now()/1000-a.status.checked>300)){a.statusQueued=true;statusQueue.push(a);pumpStatuses();}}
function createAccountCard(a,skipProfile=false){const wrapper=el('div'),b=el('button'),av=avatar(a.name),body=el('div'),title=el('strong',a.name),detail=el('small'),status=el('span'),checked=el('small'),check=el('button','Comprobar sesión'),favorite=el('button','☆');wrapper.className='account-card';b.className='account';status.className='account-status';checked.className='account-checked';check.className='account-check';favorite.type='button';favorite.className='account-favorite-star';favorite.onclick=()=>toggleAccountFavorite(a,favorite);body.append(title,detail,status);b.append(av,body);wrapper.append(b,favorite,checked,check);check.onclick=()=>verifyAccount(a);b.onclick=()=>selectAccount(a).catch(e=>notice(e.message));b.profileTask={epoch,account:a,row:b,avatar:av,title,detail};if(!skipProfile)profileObserver.observe(b);accountCards.set(a.id,{wrapper,row:b,title,detail,status,checked,check,avatar:av,favorite});updateAccountCard(a);}

// Short-lived, administrator-only display snapshot. Never store Telegram sessions,
// tokens, access links, media or credentials. Always authenticate before restoring.
const ACCOUNT_LIST_TTL=12*60*60*1000,ACCOUNT_STATUS_TTL=5*60*1000,ACCOUNT_LIST_LIMIT=1200;
let accountListKey='',accountListReady=false,accountListTimer=null,loadedAccountPages=0;
function forgetAccounts(){accountListReady=false;clearTimeout(accountListTimer);try{if(accountListKey)localStorage.removeItem(accountListKey);}catch{}}
function saveAccounts(){
 if(!panelAdmin||!accountListReady||!accountListKey)return;
 try{
  const items=[...overviewAccounts.values()].slice(0,ACCOUNT_LIST_LIMIT).map(a=>({
   id:String(a.id),name:a.name,username:a.username||'',phone:a.phone||'',
   country:a.country||'',country_code:a.country_code||'',generation:a.generation,
   status:a.status?{state:a.status.state,reason:a.status.reason,checked:a.status.checked}:null
  }));
  localStorage.setItem(accountListKey,JSON.stringify({version:1,saved:Date.now(),pages:loadedAccountPages,next:accountsNext,items}));
 }catch{ /* No storage available: fetching from the server still works. */ }
}
function queueAccountCache(){if(!accountListReady||!panelAdmin)return;clearTimeout(accountListTimer);accountListTimer=setTimeout(saveAccounts,350);}
function restoreCachedAccounts(){
 if(!panelAdmin||!accountListKey)return 0;
 try{
  const saved=JSON.parse(localStorage.getItem(accountListKey));
  if(saved?.version!==1||!Array.isArray(saved.items)||saved.items.length>ACCOUNT_LIST_LIMIT||
     !Number.isFinite(saved.saved)||saved.saved>Date.now()||Date.now()-saved.saved>ACCOUNT_LIST_TTL)return 0;
  profileObserver.disconnect();profileQueue=[];statusQueue=[];overviewAccounts.clear();accountCards.clear();buildAccountGroups();
  for(const row of saved.items){
   if(!/^[1-9][0-9]{0,18}$/.test(String(row?.id)))continue;
   const a={...row,id:String(row.id)};
   const checked=Number(a.status?.checked)||0;
   if(!checked||checked>Date.now()/1000||Date.now()-checked*1000>ACCOUNT_STATUS_TTL)
    a.status={state:'unknown',reason:'Estado pendiente de comprobar',checked:0};
   overviewAccounts.set(a.id,a);createAccountCard(a,true);
  }
  accountsNext=typeof saved.next==='string'?saved.next:null;
  $('more-accounts').hidden=!accountsNext;
  updateAccountGroups();
  if(overviewAccounts.size)$('accounts-progress').textContent='Cuentas guardadas; actualizando información…';
  return Math.min(25,Math.max(0,Number(saved.pages)||0));
 }catch{return 0;}
}
function accountMetadataMatches(a,mode,query){
 const folded=value=>String(value??'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLocaleLowerCase('es').trim();
 const term=folded(query);
 if(!term)return false;
 if(mode==='name')return folded(a.name).includes(term);
 if(mode==='id'){const digits=term.replace(/\D/g,'');return !!digits&&String(a.id||'').includes(digits);}
 if(mode==='username'){const username=term.replace(/^@/,'');return !!username&&folded(a.username).replace(/^@/,'').includes(username);}
 if(mode==='phone'){const digits=term.replace(/\D/g,'');return !!digits&&String(a.phone||'').replace(/\D/g,'').includes(digits);}
 if(mode==='country')return folded(a.country).includes(term)||folded(a.country_code).includes(term);
 return false;
}
function cancelOverviewSearch(){overviewRun++;overviewSearching=false;$('cancel-account-search').hidden=true;}
$('check-accounts').onclick=()=>{for(const a of overviewAccounts.values()){a.statusQueued=false;queueStatus(a);}pumpStatuses();};
$('cancel-account-search').onclick=()=>{cancelOverviewSearch();$('accounts-progress').textContent='Búsqueda detenida. Los resultados son parciales.';$('continue-account-search').hidden=!overviewPending.length;pumpStatuses();};
function accountSearchHit(a,hit){const button=el('button');button.className='account-search-hit';const kind=hit.chat.saved?'Mensajes guardados':({private:'Chat privado',group:'Grupo',channel:'Canal'}[hit.chat.type]||'Conversación');button.append(el('strong',a.name+' · '+a.id),el('span',kind+' · '+hit.chat.name));if(hit.message)button.append(el('span',hit.message.text||labels[hit.message.media?.type]||'Mensaje'),el('small',date(hit.message.date)+' '+time(hit.message.date)));else button.append(el('small','Coincidencia en el nombre'));button.onclick=async()=>{cancelOverviewSearch();try{await selectAccount(a);await openChat(hit.chat);if(hit.message)await jumpToMessage(hit.message.id,true);}catch(e){notice(e.message);}};$('accounts-results').append(button);}
let overviewNeedle='',overviewSeen=new Set(),overviewSkipped=0,overviewComplete=0,overviewTotal=0,overviewResults=[],overviewFailures=[],overviewDisplayed=0,overviewMode='phrases';
const OVERVIEW_RESULT_PAGE=80;
const ENTITY_SEARCH_MODES=new Set(['channels','channel_id','groups','group_id','bots']);
function isEntitySearchMode(mode){return ENTITY_SEARCH_MODES.has(mode);}
function entityIdMatches(id,needle){
 const value=String(id||'').trim(),requested=String(needle||'').replace(/\s/g,'');
 if(!/^-?\d+$/.test(requested))return false;
 if(value===requested)return true;
 // Telegram peers can be shown as -100<id> or -<id>. Also allow the
 // unmarked numeric ID, but never a partial number or a different peer type.
 if(!requested.startsWith('-'))return (value.startsWith('-100')&&value.slice(4)===requested)||
  (value.startsWith('-')&&value.slice(1)===requested);
 return false;
}
function entitySearchMatches(chat,mode,needle){
 if(!chat||!isEntitySearchMode(mode))return false;
 if(mode==='bots'){if(chat.type!=='private'||chat.bot!==true)return false;}
 else if(mode.startsWith('channel')){if(chat.type!=='channel')return false;}
 else if(chat.type!=='group')return false;
 if(mode==='channel_id'||mode==='group_id')return entityIdMatches(chat.id,needle);
 const folded=value=>String(value||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLocaleLowerCase('es').trim();
 const term=folded(needle),username=term.replace(/^@/,'');
 // Empty text means list every authorized channel, group or bot in that category.
 return !term||folded(chat.name).includes(term)||
  (!!username&&folded(chat.username).replace(/^@/,'').includes(username));
}
function activeSearchAccounts(accounts){return [...accounts].filter(a=>a.status?.state==='active');}
function searchProgress(current=''){
 const progress=$('accounts-progress');
 progress.textContent=overviewComplete+'/'+overviewTotal+' cuentas activas revisadas · '+overviewResults.length+' resultados'+(overviewSkipped?' · '+overviewSkipped+' con error':'')+(current?' · Revisando '+current:'');
}
function appendOverviewResults(){
 const box=$('accounts-results'),previous=box.querySelector('[data-result-more]');
 if(previous)previous.remove();
 const to=Math.min(overviewResults.length,overviewDisplayed+OVERVIEW_RESULT_PAGE);
 while(overviewDisplayed<to){
  const hit=overviewResults[overviewDisplayed++];
  if(overviewMode==='phrases')accountSearchHit(hit.account,hit.hit);
  else if(isEntitySearchMode(overviewMode))entitySearchHit(hit.account,hit.hit);
  else metadataSearchHit(hit.account);
 }
 if(overviewDisplayed<overviewResults.length){
  const button=el('button','Mostrar más resultados · '+overviewDisplayed+'/'+overviewResults.length);
  button.className='more';button.dataset.resultMore='1';button.onclick=appendOverviewResults;box.append(button);
 }
}
function finishOverviewResults(){
 const box=$('accounts-results');box.replaceChildren();box.hidden=false;overviewDisplayed=0;
 if(!overviewResults.length){box.append(el('p','No se encontraron coincidencias en las cuentas revisadas.'));return;}
 appendOverviewResults();
}
async function loadActiveSearchScope(run){
 $('accounts-progress').textContent='Preparando las cuentas activas para una búsqueda completa…';
 while(accountsNext&&run===overviewRun){
  if(document.hidden)break;
  await loadAccounts(true);
 }
 if(run!==overviewRun)return null;
 if(accountsNext)return null;
 return activeSearchAccounts(overviewAccounts.values());
}
async function runAccountSearch(continuing=false){
 const needle=$('accounts-query').value.trim();
 if(!needle)return;
 if(operationBusy)return notice('Espera a que termine la operación.');
 cancelOverviewSearch();
 const run=overviewRun;
 overviewSearching=true;statusQueue=[];stopLive();
 $('cancel-account-search').hidden=false;$('continue-account-search').hidden=true;
 $('accounts-results').hidden=false;
 try{
  if(!continuing||needle!==overviewNeedle||overviewMode!=='phrases'){
   overviewNeedle=needle;overviewMode='phrases';overviewSeen.clear();
   overviewResults=[];overviewFailures=[];overviewPending=[];overviewSkipped=0;overviewComplete=0;overviewTotal=0;
   $('accounts-results').replaceChildren();
   const active=await loadActiveSearchScope(run);
   if(run!==overviewRun)return;
   if(!active){
    $('accounts-progress').textContent='Búsqueda pausada mientras se completa la lista de cuentas. Vuelve a pulsar Buscar.';
    return;
   }
   overviewTotal=active.length;
   overviewPending=active.map(a=>({account:a,cursor:'',cursors:new Set()}));
  }else if(!overviewPending.length&&overviewFailures.length){
   overviewPending=overviewFailures.splice(0).map(v=>({account:v.account,cursor:v.cursor,cursors:new Set()}));
   overviewSkipped=0;
  }
  searchProgress();
  // Exhaust each active account before advancing the numerator; never silently
  // stop after a global page budget or a maximum number of search hits.
  while(overviewPending.length&&run===overviewRun){
   if(document.hidden)break;
   const work=overviewPending[0],a=work.account;
   searchProgress(a.name);
   try{
    const result=await api('accounts-search?account='+a.id+'&q='+encodeURIComponent(overviewNeedle)+(work.cursor?'&cursor='+encodeURIComponent(work.cursor):''));
    if(run!==overviewRun)return;
    if(!Array.isArray(result.items))throw Error('Respuesta de búsqueda inválida.');
    for(const hit of result.items){
     const key=a.id+':'+hit.chat?.id+':'+(hit.message?.id??'title');
     if(overviewSeen.has(key)||!hit.chat?.id)continue;
     overviewSeen.add(key);overviewResults.push({account:a,hit});
    }
    if(result.next){
     if(typeof result.next!=='string'||work.cursors.has(result.next)||result.next===work.cursor)
      throw Error('Telegram repitió la página de búsqueda; no se puede confirmar el final.');
     work.cursors.add(result.next);work.cursor=result.next;
    }else{
     overviewPending.shift();overviewComplete++;
    }
   }catch(e){
    if(run!==overviewRun)return;
    overviewPending.shift();overviewFailures.push({account:a,cursor:work.cursor,error:e.message});
    overviewSkipped=overviewFailures.length;
   }
   searchProgress();
   // The panel shares a per-actor API quota with normal navigation. Scan at
   // a conservative rate so the search does not flood the service or Telegram.
   if(overviewPending.length)await new Promise(resolve=>setTimeout(resolve,400));
  }
  if(run!==overviewRun)return;
  finishOverviewResults();
  if(overviewPending.length){
   $('accounts-progress').textContent='Búsqueda pausada · '+overviewComplete+'/'+overviewTotal+' cuentas activas terminadas. '+overviewResults.length+' resultados parciales.';
  }else if(overviewFailures.length){
   $('accounts-progress').textContent='Búsqueda incompleta · '+overviewComplete+'/'+overviewTotal+' cuentas activas terminadas · '+overviewFailures.length+' sin revisar por errores · '+overviewResults.length+' resultados comprobados. Pulsa Seguir buscando para reintentar.';
   for(const issue of overviewFailures){
    const warning=el('p',issue.account.name+' · Sin revisar por error: '+issue.error);
    warning.className='account-search-warning';$('accounts-results').append(warning);
   }
  }else{
   $('accounts-progress').textContent='Búsqueda terminada · '+overviewComplete+'/'+overviewTotal+' cuentas activas revisadas · '+overviewResults.length+' resultados encontrados por Telegram.';
  }
 }catch(e){
  if(run===overviewRun)$('accounts-progress').textContent='Búsqueda incompleta · '+overviewComplete+'/'+overviewTotal+' cuentas activas revisadas: '+e.message;
 }finally{
  if(run===overviewRun){
   overviewSearching=false;$('cancel-account-search').hidden=true;
   $('continue-account-search').hidden=!overviewPending.length&&!overviewFailures.length;
   pumpStatuses();
  }
 }
}
function entitySearchHit(a,hit){
 const chat=hit.chat,kind=chat.bot?'Bot':chat.type==='channel'?'Canal':'Grupo';
 const button=el('button');button.className='account-search-hit';
 button.append(el('strong',chat.name||kind));
 button.append(el('span',kind+' · ID: '+chat.id));
 if(chat.username)button.append(el('small','@'+chat.username.replace(/^@/,'')));
 button.append(el('small','Cuenta activa: '+a.name+' · '+a.id));
 button.onclick=async()=>{
  cancelOverviewSearch();
  try{await selectAccount(a);await openChat(chat);}
  catch(e){notice(e.message);}
 };
 $('accounts-results').append(button);
}
async function runAccountEntitySearch(continuing=false){
 const mode=$('accounts-search-mode').value,needle=$('accounts-query').value.trim();
 if(!isEntitySearchMode(mode))return;
 if((mode==='channel_id'||mode==='group_id')&&!/^-?\d+$/.test(needle)){
  $('accounts-progress').textContent='Introduce el ID completo del canal o grupo.';
  return;
 }
 if(operationBusy)return notice('Espera a que termine la operación.');
 cancelOverviewSearch();
 const run=overviewRun;
 overviewSearching=true;statusQueue=[];stopLive();
 $('cancel-account-search').hidden=false;$('continue-account-search').hidden=true;
 $('accounts-results').hidden=false;
 try{
  if(!continuing||mode!==overviewMode||needle!==overviewNeedle){
   overviewNeedle=needle;overviewMode=mode;overviewSeen.clear();
   overviewResults=[];overviewFailures=[];overviewPending=[];overviewSkipped=0;overviewComplete=0;overviewTotal=0;
   $('accounts-results').replaceChildren();
   const active=await loadActiveSearchScope(run);
   if(run!==overviewRun)return;
   if(!active){
    $('accounts-progress').textContent='Búsqueda pausada: aún faltan cuentas por cargar. Pulsa Buscar para reintentar.';
    return;
   }
   overviewTotal=active.length;
   overviewPending=active.map(a=>({account:a,cursor:'',cursors:new Set()}));
  }else if(!overviewPending.length&&overviewFailures.length){
   overviewPending=overviewFailures.splice(0).map(v=>({account:v.account,cursor:v.cursor,cursors:new Set()}));
   overviewSkipped=0;
  }
  searchProgress();
  while(overviewPending.length&&run===overviewRun){
   if(document.hidden)break;
   const work=overviewPending[0],a=work.account;
   searchProgress(a.name);
   try{
    const result=await api('chats?account='+a.id+(work.cursor?'&cursor='+encodeURIComponent(work.cursor):''));
    if(run!==overviewRun)return;
    if(!Array.isArray(result.items))throw Error('Respuesta de conversaciones inválida.');
    if(a.generation!==undefined&&result.generation!==undefined&&a.generation!==result.generation)
     throw Error('La sesión cambió durante la búsqueda.');
    for(const chat of result.items){
     if(!entitySearchMatches(chat,mode,needle))continue;
     const key=a.id+':'+chat.id;
     if(overviewSeen.has(key))continue;
     overviewSeen.add(key);overviewResults.push({account:a,hit:{chat}});
    }
    if(result.next){
     if(typeof result.next!=='string'||result.next===work.cursor||work.cursors.has(result.next))
      throw Error('Telegram repitió una página de chats; no se puede confirmar el final.');
     work.cursors.add(result.next);work.cursor=result.next;
    }else{overviewPending.shift();overviewComplete++;}
   }catch(e){
    if(run!==overviewRun)return;
    overviewPending.shift();overviewFailures.push({account:a,cursor:work.cursor,error:e.message});
    overviewSkipped=overviewFailures.length;
   }
   searchProgress();
   // Use the existing read-only chats endpoint at a conservative rate.
   if(overviewPending.length)await new Promise(resolve=>setTimeout(resolve,400));
  }
  if(run!==overviewRun)return;
  finishOverviewResults();
  if(overviewPending.length)
   $('accounts-progress').textContent='Búsqueda pausada · '+overviewComplete+'/'+overviewTotal+' cuentas activas terminadas · '+overviewResults.length+' resultados parciales.';
  else if(overviewFailures.length){
   $('accounts-progress').textContent='Búsqueda incompleta · '+overviewComplete+'/'+overviewTotal+' cuentas activas terminadas · '+overviewFailures.length+' con error · '+overviewResults.length+' coincidencias. Pulsa Seguir buscando para reintentar.';
   for(const issue of overviewFailures){
    const warning=el('p',issue.account.name+' · No se pudo revisar: '+issue.error);
    warning.className='account-search-warning';$('accounts-results').append(warning);
   }
  }else $('accounts-progress').textContent='Búsqueda terminada · '+overviewComplete+'/'+overviewTotal+' cuentas activas revisadas · '+overviewResults.length+' '+(mode==='bots'?'bots':mode.startsWith('channel')?'canales':'grupos')+' encontrados.';
 }catch(e){
  if(run===overviewRun)$('accounts-progress').textContent='Búsqueda incompleta · '+overviewComplete+'/'+overviewTotal+' cuentas revisadas: '+e.message;
 }finally{
  if(run===overviewRun){
   overviewSearching=false;$('cancel-account-search').hidden=true;
   $('continue-account-search').hidden=!overviewPending.length&&!overviewFailures.length;
   pumpStatuses();
  }
 }
}
function metadataSearchHit(a){
 const button=el('button');button.className='account-search-hit';
 button.append(el('strong',a.name+' · '+a.id));
 button.append(el('span','@username: '+(a.username?'@'+a.username.replace(/^@/,''):'No disponible')));
 button.append(el('span','Teléfono: '+(a.phone||'No disponible')));
 button.append(el('span','País: '+(a.country||'No disponible')));
 button.append(el('small','Estado: '+(accountStates[a.status?.state]||accountStates.unknown)));
 button.onclick=()=>selectAccount(a).catch(e=>notice(e.message));
 $('accounts-results').append(button);
}
async function runAccountMetadataSearch(continuing=false){
 const needle=$('accounts-query').value.trim(),mode=$('accounts-search-mode').value;
 if(!needle)return;
 if(operationBusy)return notice('Espera a que termine la operación.');
 cancelOverviewSearch();
 const run=overviewRun;overviewSearching=true;statusQueue=[];
 $('cancel-account-search').hidden=false;
 $('continue-account-search').hidden=true;
 $('accounts-results').hidden=false;
 try{
  if(!continuing||mode!==overviewMode||needle!==overviewNeedle){
   overviewNeedle=needle;overviewMode=mode;overviewSeen.clear();
   overviewResults=[];overviewPending=[];overviewFailures=[];overviewSkipped=0;overviewComplete=0;overviewTotal=0;
   $('accounts-results').replaceChildren();
   const active=await loadActiveSearchScope(run);
   if(run!==overviewRun)return;
   if(!active){
    $('accounts-progress').textContent='Búsqueda pausada mientras se completa la lista de cuentas. Vuelve a pulsar Buscar.';
    return;
   }
   overviewTotal=active.length;
   overviewPending=active.map(a=>({account:a}));
  }
  searchProgress();
  while(overviewPending.length&&run===overviewRun){
   if(document.hidden)break;
   const a=overviewPending.shift().account;
   if(accountMetadataMatches(a,mode,needle))overviewResults.push({account:a});
   overviewComplete++;
   searchProgress(a.name);
   // Give iOS a chance to paint each increment without opening Telegram sessions.
   await new Promise(resolve=>setTimeout(resolve,0));
  }
  if(run!==overviewRun)return;
  finishOverviewResults();
  if(overviewPending.length)$('accounts-progress').textContent='Búsqueda pausada · '+overviewComplete+'/'+overviewTotal+' cuentas activas revisadas · '+overviewResults.length+' coincidencias parciales.';
  else $('accounts-progress').textContent='Búsqueda terminada · '+overviewComplete+'/'+overviewTotal+' cuentas activas revisadas · '+overviewResults.length+' coincidencias.';
 }catch(e){
  if(run===overviewRun)$('accounts-progress').textContent='Búsqueda incompleta · '+overviewComplete+'/'+overviewTotal+': '+e.message;
 }finally{
  if(run===overviewRun){
   overviewSearching=false;$('cancel-account-search').hidden=true;
   $('continue-account-search').hidden=!overviewPending.length;pumpStatuses();
  }
 }
}
function resetAccountSearch(){
 cancelOverviewSearch();overviewPending=[];
 $('accounts-results').replaceChildren();$('accounts-results').hidden=true;
 $('continue-account-search').hidden=true;
 $('accounts-progress').textContent='Selecciona una cuenta vinculada';
}
$('accounts-search-mode').onchange=()=>{
 const fields={phrases:'Buscar frases',channels:'Nombre o @usuario (opcional)',channel_id:'Canal ID completo',groups:'Nombre o @usuario (opcional)',group_id:'Group ID completo',bots:'Nombre o @usuario (opcional)',name:'Nombre de Telegram',id:'Telegram ID',username:'@username',phone:'Número de teléfono',country:'País'};
 const current=$('accounts-search-mode').value,field=$('accounts-query');
 field.value='';field.placeholder=fields[current]||fields.phrases;
 field.setAttribute('aria-label','Buscar por '+(fields[current]||'frases'));
 field.inputMode=['id','channel_id','group_id'].includes(current)?'numeric':current==='phone'?'tel':'search';
 resetAccountSearch();
};
$('accounts-query').oninput=()=>{if(overviewSearching||!$('accounts-query').value.trim())resetAccountSearch();};
$('accounts-search-form').onsubmit=e=>{
 e.preventDefault();
 const mode=$('accounts-search-mode').value;
 if(mode==='phrases')runAccountSearch(false);
 else if(isEntitySearchMode(mode))runAccountEntitySearch(false);
 else runAccountMetadataSearch();
};
$('continue-account-search').onclick=()=>{if(overviewMode==='phrases')runAccountSearch(true);else if(isEntitySearchMode(overviewMode))runAccountEntitySearch(true);else runAccountMetadataSearch(true);};


// Search execution lives on the authenticated web server, not in this browser.
// iOS may suspend JavaScript in the background; the encrypted database checkpoint
// and server job continue independently. This view only polls and renders results.
let persistentJob=null,persistentCursor=0,persistentFetching=false,persistentSeen=new Set(),
    persistentDisplayed=0,persistentMore=null,persistentPollBusy=false;
let searchEnabled=false,nameSearchGeneration=0,nameSearchTimer=null,nameSearchNext=null,
    nameSearchQuery='',nameSearchLoading=false;

function clearAccountSearchView(clearInput=true){
 nameSearchGeneration++;clearTimeout(nameSearchTimer);nameSearchTimer=null;
 nameSearchQuery='';nameSearchNext=null;nameSearchLoading=false;
 searchEnabled=false;persistentJob=null;persistentCursor=0;persistentDisplayed=0;
 persistentSeen.clear();persistentMore=null;
 if(clearInput)$('accounts-query').value='';
 $('accounts-results').replaceChildren();$('accounts-results').hidden=true;
 $('cancel-account-search').hidden=true;$('continue-account-search').hidden=true;
 $('accounts-progress').textContent='Selecciona una cuenta vinculada';
}
async function clearAccountSearch(){
 clearAccountSearchView();
 try{await api('search-job-clear',{});}
 catch(e){notice('No se pudo limpiar la búsqueda anterior: '+e.message);}
}
function nameSearchHit(a){
 const button=el('button');button.className='account-search-hit';
 button.append(el('strong',a.name),el('small','Telegram ID: '+a.id));
 if(a.username)button.append(el('small','@'+a.username.replace(/^@/,'')));
 button.onclick=()=>selectAccount(a).catch(e=>notice(e.message));
 $('accounts-results').append(button);
}
async function runFastNameSearch(append=false){
 if($('accounts-search-mode').value!=='name')return;
 const needle=$('accounts-query').value.trim();
 if(!needle){clearAccountSearchView(false);return;}
 if(append&&(!nameSearchNext||nameSearchLoading))return;
 clearTimeout(nameSearchTimer);nameSearchTimer=null;
 const generation=++nameSearchGeneration;
 const after=append?nameSearchNext:0;
 nameSearchQuery=needle;
 if(!append){
  $('accounts-results').replaceChildren();$('accounts-results').hidden=false;
  nameSearchNext=null;
 }
 nameSearchLoading=true;
 $('accounts-progress').textContent='Buscando nombres de cuentas vinculadas…';
 $('cancel-account-search').hidden=true;$('continue-account-search').hidden=true;
 try{
  // This endpoint consults only saved encrypted profile metadata. Bypass the
  // shared Telegram-traffic request queue to keep typing responsive on iOS.
  const url=base+'/api/account-name-search?q='+encodeURIComponent(needle)+'&after='+after;
  const response=await fetch(url,{cache:'no-store',credentials:'same-origin'});
  const data=await response.json();
  if(!response.ok)throw Error(data.error||'No se pudo buscar por nombre.');
  if(generation!==nameSearchGeneration||$('accounts-search-mode').value!=='name'
    ||$('accounts-query').value.trim()!==needle)return;
  for(const account of data.items||[])nameSearchHit(account);
  nameSearchNext=data.next;
  const total=Number(data.total||0);
  $('accounts-progress').textContent=total
    ?total+' cuenta'+(total===1?'':'s')+' cuyo nombre comienza con «'+needle+'».'
    :'No se encontraron cuentas que empiecen con «'+needle+'».';
  $('continue-account-search').textContent='Mostrar más nombres';
  $('continue-account-search').hidden=!nameSearchNext;
  if(!total)$('accounts-results').append(el('p','No hay coincidencias de nombres.'));
 }catch(e){
  if(generation===nameSearchGeneration)notice(e.message);
 }finally{
  if(generation===nameSearchGeneration)nameSearchLoading=false;
 }
}
function scheduleFastNameSearch(){
 ++nameSearchGeneration;clearTimeout(nameSearchTimer);
 if(!$('accounts-query').value.trim()){clearAccountSearchView(false);return;}
 nameSearchTimer=setTimeout(()=>runFastNameSearch(false),190);
}

function serverSearchState(state){return ['queued','running','waiting'].includes(state);}
async function pausePersistentSearchForChats(){
 // A background search should not keep borrowing Telegram sessions while
 // the administrator is reading a conversation.
 try{
  const job=(await api('search-job-status')).job;
  if(!job||!serverSearchState(job.state))return;
  const result=await api('search-job-control',{id:job.id,action:'pause'});
  if(persistentJob?.id===job.id)persistentJob=result.job;
 }catch(e){notice('No se pudo pausar la búsqueda: '+e.message);}
}
function serverSearchNote(job){
 const count=job.completed+'/'+job.total+' cuentas activas revisadas · '+job.found+' resultados'+(job.cached?' · '+job.cached+' revisadas desde índice local':'');
 if(job.state==='complete')return 'Búsqueda terminada · '+count;
 if(job.state==='incomplete')return 'Búsqueda incompleta · '+count+' · '+job.failures+' cuentas con error. Puedes reintentar.';
 if(job.state==='paused')return 'Búsqueda pausada · '+count;
 if(job.state==='cancelled')return 'Búsqueda detenida · '+count;
 if(job.state==='expired')return 'Búsqueda detenida automáticamente tras una hora para liberar las cuentas. Inicia otra búsqueda.';
 if(job.state==='waiting')return 'Esperando a Telegram; la búsqueda seguirá automáticamente · '+count;
 if(job.state==='queued')return 'Búsqueda guardada en el servidor · '+count;
 return 'Búsqueda en segundo plano · '+count+(job.current?' · '+job.current:'');
}
function renderServerJob(job){
 if(!job){
  persistentJob=null;return;
 }
 if(persistentJob?.id!==job.id){
  persistentCursor=0;persistentDisplayed=0;persistentSeen.clear();persistentMore=null;
  $('accounts-results').replaceChildren();$('accounts-results').hidden=false;
  // The last saved query is history, not the browser's current search field.
  // Never reinsert a previous name (such as Realme) into the input.
 }
 persistentJob=job;
 $('accounts-progress').textContent=serverSearchNote(job);
 const running=serverSearchState(job.state);
 $('cancel-account-search').hidden=!running;
 $('cancel-account-search').textContent='Detener búsqueda';
 $('continue-account-search').hidden=!(job.state==='paused'||job.state==='incomplete');
 $('continue-account-search').textContent=job.state==='incomplete'?'Reintentar pendientes':'Reanudar búsqueda';
 if(job.state==='complete'&&!job.found&&!persistentDisplayed&&!$('accounts-results').children.length){
  $('accounts-results').append(el('p','Búsqueda terminada sin coincidencias.'));
 }
}
function renderServerHit(job,row){
 const a=row.account,hit=row.hit;
 if(!a?.id||persistentSeen.has(a.id+':'+(hit?.chat?.id||'meta')+':'+(hit?.message?.id||'title')))return;
 persistentSeen.add(a.id+':'+(hit?.chat?.id||'meta')+':'+(hit?.message?.id||'title'));
 if(job.mode==='phrases'&&hit?.chat)accountSearchHit(a,hit);
 else if(isEntitySearchMode(job.mode)&&hit?.chat)entitySearchHit(a,hit);
 else if(!hit)metadataSearchHit(a);
 persistentDisplayed++;
}
async function loadPersistentResults(){
 const job=persistentJob;
 if(!job||persistentFetching)return;
 persistentFetching=true;
 try{
  const r=await api('search-job-results?id='+encodeURIComponent(job.id)+'&after='+persistentCursor);
  if(persistentJob?.id!==job.id)return;
  const box=$('accounts-results'),more=box.querySelector('[data-server-more]');
  if(more)more.remove();
  for(const row of r.items||[])renderServerHit(job,row);
  persistentCursor=r.cursor||persistentCursor;
  persistentMore=r.next;
  if(r.next){
   const button=el('button','Mostrar más resultados');
   button.className='more';button.dataset.serverMore='1';
   button.onclick=()=>loadPersistentResults().catch(e=>notice(e.message));
   box.append(button);
  }
 }finally{persistentFetching=false;}
}
async function refreshPersistentSearch(){
 if(!searchEnabled||persistentPollBusy||document.hidden||$('workspace').hidden
   ||$('accounts-search-mode').value==='name')return;
 persistentPollBusy=true;
 try{
  const response=await api('search-job-status');
  const job=response.job;
  if(!job){persistentJob=null;return;}
  const changed=persistentJob?.id!==job.id;
  renderServerJob(job);
  if(changed||(!persistentMore&&job.found>persistentDisplayed)||
     (['complete','paused','incomplete'].includes(job.state)&&!persistentDisplayed&&job.found))
   await loadPersistentResults();
 }finally{persistentPollBusy=false;}
}
async function startPersistentSearch(){
 const searchMode=$('accounts-search-mode').value,needle=$('accounts-query').value.trim();
 if(!needle&&!isEntitySearchMode(searchMode))return;
 if(searchMode==='name')return runFastNameSearch(false);
 searchEnabled=true;
 try{
  const data=await api('search-job-start',{mode:searchMode,query:needle});
  if(!searchEnabled)return;
  renderServerJob(data.job);
  await refreshPersistentSearch();
 }catch(e){notice(e.message);}
}
async function controlPersistentSearch(action){
 if(!persistentJob)return;
 try{
  const result=await api('search-job-control',{id:persistentJob.id,action});
  renderServerJob(result.job);
  if(result.job?.state==='complete'||result.job?.state==='paused'||result.job?.state==='cancelled')
   await loadPersistentResults();
 }catch(e){notice(e.message);}
}
// Override only the account-overview search controls. Keep the chat's own
// per-conversation search and every other account panel feature unchanged.
$('accounts-search-form').onsubmit=e=>{e.preventDefault();startPersistentSearch();};
$('cancel-account-search').onclick=()=>controlPersistentSearch('cancel');
$('continue-account-search').onclick=()=>$('accounts-search-mode').value==='name'
 ?runFastNameSearch(true):controlPersistentSearch('resume');
$('clear-account-search').onclick=clearAccountSearch;
// Keep ordinary phrase/channel search unchanged; only the name option receives
// instant, prefix-only lookup with no per-account Telegram session checks.
const previousSearchModeChange=$('accounts-search-mode').onchange;
$('accounts-search-mode').onchange=()=>{
 previousSearchModeChange?.();
 const hadJob=searchEnabled||!!persistentJob;
 clearAccountSearchView(false);
 if(hadJob)api('search-job-clear',{}).catch(e=>notice(e.message));
 if($('accounts-search-mode').value==='name')scheduleFastNameSearch();
};
$('accounts-query').oninput=()=>{
 if($('accounts-search-mode').value==='name'){
  if(searchEnabled||persistentJob){
   searchEnabled=false;persistentJob=null;
   api('search-job-clear',{}).catch(e=>notice(e.message));
  }
  scheduleFastNameSearch();
 }else if(overviewSearching||!$('accounts-query').value.trim())resetAccountSearch();
};
document.addEventListener('visibilitychange',()=>{
 if(!document.hidden)refreshPersistentSearch().catch(e=>notice(e.message));
});
window.addEventListener('pageshow',event=>{
 if(event.persisted)clearAccountSearch().catch(e=>notice(e.message));
 else refreshPersistentSearch().catch(e=>notice(e.message));
});
setInterval(()=>{
 if(!document.hidden&&$('workspace').dataset.view==='accounts')
  refreshPersistentSearch().catch(()=>{});
},3000);

// Panel-only continuity. No credentials, drafts, protected content or Telegram sessions are stored.
let resumeKey='',resumeReady=false,resumeRestoring=false,resumeTimer=null;
const RESUME_TTL=7*86400000,RESUME_LIMIT=100;
function forgetResume(){resumeReady=false;clearTimeout(resumeTimer);try{if(resumeKey)localStorage.removeItem(resumeKey);}catch{}}
function readResume(){try{const value=JSON.parse(localStorage.getItem(resumeKey));if(value?.version!==1||!Number.isFinite(value.saved)||Date.now()-value.saved>RESUME_TTL||value.saved>Date.now()+60000)return null;return value;}catch{return null;}}
function snapshotResume(){
 const currentView=$('workspace').dataset.view;
 const anchor=currentView==='conversation'?captureAnchor():null;
 const ordered=[...messages.values()].sort((a,b)=>mode==='gallery'?b.id-a.id:a.id-b.id);
 const at=Math.max(0,ordered.findIndex(m=>String(m.id)===anchor?.id)),start=Math.max(0,at-30),selected=ordered.slice(start,start+RESUME_LIMIT);
 // Never store protected message bodies or media; they must be fetched again.
 const items=selected.filter(m=>!m.protected).map(({signature,...m})=>m);
 return {version:1,saved:Date.now(),view:currentView,accountTab,account:account?{id:account.id,generation:account.generation}:null,
  chat:currentView==='conversation'&&chat?{id:chat.id,handle:chat.handle,name:chat.name,type:chat.type,forum:chat.forum}:null,
  mode,query,filter,chatQuery:$('chat-search').value,topic,contextMode,contextFocus,anchor,
  scroll:$('thread').scrollTop,chatsScroll:document.querySelector('.chat-list').scrollTop,accountsScroll:document.querySelector('.accounts').scrollTop,
  items,before:(mode==='gallery'?start+RESUME_LIMIT<ordered.length:start>0)?Math.min(...selected.map(m=>m.id)):before};
}
function saveResume(){if(!resumeReady||resumeRestoring||!resumeKey||$('workspace').hidden)return;try{const value=JSON.stringify(snapshotResume());if(value.length<=1500000)localStorage.setItem(resumeKey,value);}catch{ /* Storage disabled/full: the panel continues normally. */ }}
function queueResume(){if(!resumeReady||resumeRestoring)return;clearTimeout(resumeTimer);resumeTimer=setTimeout(saveResume,300);}
function restorePosition(saved){const chosen=epoch;requestAnimationFrame(()=>requestAnimationFrame(()=>{if(chosen!==epoch)return;if(!restoreAnchor(saved.anchor))$('thread').scrollTop=Number(saved.scroll)||0;previousScrollTop=$('thread').scrollTop;}));}
async function restoreConversation(saved){
 const chosen=epoch;
 // Validate live account/source authorization before displaying any saved conversation.
 await api('permissions?account='+account.id+'&peer='+encodeURIComponent(chat.handle));
 if(chosen!==epoch||$('workspace').hidden)return;
 mode=saved.mode==='gallery'?'gallery':'messages';query=typeof saved.query==='string'?saved.query:'';topic=Number(saved.topic)||0;
 contextMode=!!saved.contextMode;contextFocus=Number(saved.contextFocus)||null;
 $('message-query').value=query;$('message-search').hidden=!query;$('return-latest').hidden=!contextMode;
 tabs();messages.clear();for(const m of (Array.isArray(saved.items)?saved.items:[]).slice(0,RESUME_LIMIT))if(Number.isSafeInteger(m.id)&&!m.protected)messages.set(m.id,{...m,signature:JSON.stringify(m)});
 before=saved.before||null;renderMessages();$('older').hidden=!before;restorePosition(saved);
 // Revalidate saved messages, including edits/deletions during absence, then fetch new arrivals.
 const ids=[...messages.keys()];if(ids.length)await applyLiveChanges(ids);
 if(chosen!==epoch||$('workspace').hidden)return;
 if(contextMode&&contextFocus&&!messages.size)await jumpToMessage(contextFocus,true);
 else if(!contextMode)await loadMessages(false,true);
 if(mode==='gallery')loadGalleryCount();
 restorePosition(saved);
}
async function resumePanel(){
 const saved=readResume();if(!saved)return;resumeRestoring=true;
 try{
  setAccountTab(saved.accountTab==='inactive'?'inactive':'active');
  if(saved.view==='accounts'||!saved.account){document.querySelector('.accounts').scrollTop=Number(saved.accountsScroll)||0;return;}
  while(!overviewAccounts.has(saved.account.id)&&accountsNext)await loadAccounts(true);
  const a=overviewAccounts.get(saved.account.id);if(!a)return;
  await selectAccount(a);
  filter=['all','private','group','channel','owner','admin','saved'].includes(saved.filter)?saved.filter:'all';
  $('chat-search').value=typeof saved.chatQuery==='string'?saved.chatQuery:'';
  document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b.dataset.filter===filter));renderChats();
  document.querySelector('.chat-list').scrollTop=Number(saved.chatsScroll)||0;
  if(saved.view==='conversation'&&saved.chat){
   // Old handles/content are discarded when the linked account generation changes.
   if(a.generation!==saved.account.generation){forgetResume();return;}
   await openChat(rows.find(c=>c.id===saved.chat.id)||saved.chat,saved);
  }
 }catch(e){if(!$('workspace').hidden){chat=null;messages.clear();$('thread').replaceChildren();view(account?'chats':'accounts');notice('No se pudo recuperar la vista anterior: '+e.message);}}
 finally{resumeRestoring=false;}
}
for(const node of [$('thread'),document.querySelector('.chat-list'),document.querySelector('.accounts')])node.addEventListener('scroll',queueResume,{passive:true});
document.addEventListener('click',queueResume);document.addEventListener('change',queueResume);
document.addEventListener('visibilitychange',()=>{if(document.hidden)saveResume();});
window.addEventListener('pagehide',saveResume);
// Safari may restore the page from its back/forward cache without visibilitychange.
window.addEventListener('pageshow',event=>{if(event.persisted){startLive();if(chat&&!modal&&!contextMode)loadMessages(false,true);}});


// Purchased media stays inside this account's read-only folder.
$('purchases').onclick=()=>openPurchases().catch(e=>notice(e.message));
async function openPurchases(){
 if(!account||operationBusy)return;
 const selected=account,root=el('section'),box=management('⭐ Comprados · '+account.name);
 box.append(root);let next=null,busy=false,selectedSource=null;
 const purchases=new Map(),groups=new Map(),status=el('p','Consultando compras…'),list=el('div'),more=el('button','Cargar más compras');
 more.hidden=true;root.append(status,list,more);
 const current=()=>account===selected&&root.isConnected&&$('management').open;
 const url=(op,item,index)=>base+'/api/'+op+'?account='+selected.id+'&peer='+encodeURIComponent(item.chat.handle)+'&purchase='+encodeURIComponent(item.token)+'&index='+index;
 const stopMedia=()=>{for(const video of list.querySelectorAll('video')){video.pause();video.removeAttribute('src');video.load();}};
 const render=()=>{
  stopMedia();list.replaceChildren();
  if(selectedSource===null){
   for(const [source,group] of groups){const button=el('button');button.className='search-hit';button.append(el('strong',group.chat.name),el('span',(group.chat.bot?'Bot':{private:'Chat privado',group:'Grupo',channel:'Canal'}[group.chat.type])+' · '+group.items.size+' compras cargadas'));button.onclick=()=>{selectedSource=source;render();};list.append(button);}
  }else{
   const group=groups.get(selectedSource),back=el('button','‹ Volver a Comprados');back.onclick=()=>{selectedSource=null;render();};list.append(back,el('h3',group.chat.name));
   for(const id of group.items){const item=purchases.get(id),card=el('article');card.className='purchase-card';card.append(el('strong',item.title),el('p',date(item.date)+' · '+item.media.length+' archivos'));
    if(item.link){const link=el('a','Abrir mensaje original en Telegram');link.href=item.link;link.target='_blank';link.rel='noopener noreferrer';card.append(link);}else card.append(el('small','Este chat no tiene un enlace público al mensaje.'));
    if(item.message){const jump=el('button','Ver mensaje en el panel');jump.onclick=async()=>{stopMedia();$('management').close();try{await openChat(item.chat);await jumpToMessage(item.message);}catch(e){notice(e.message);}};card.append(jump);}
    for(const media of item.media){const row=el('div'),show=el('button',(media.type==='video'?'▶ Ver video':'🖼 Ver foto')+' '+(media.index+1));row.className='purchase-media';show.onclick=()=>{const node=el(media.type==='video'?'video':'img');node.className='purchase-preview';if(media.type==='video'){node.controls=true;node.playsInline=true;node.preload='metadata';}else node.alt='Foto comprada';node.src=url('purchase-media',item,media.index);node.onerror=()=>notice('No se pudo cargar este archivo. Vuelve a abrir Comprados para actualizarlo.');show.replaceWith(node);};row.append(show);card.append(row);}
    list.append(card);
   }
  }
  status.textContent=purchases.size?(next?'Resultados parciales · '+purchases.size+' compras cargadas.':'Consulta completada · '+purchases.size+' compras disponibles.'):(next?'Todavía no hay fotos o videos comprados en esta página. Carga más para continuar.':'No hay fotos o videos comprados disponibles para esta cuenta.');
  more.hidden=!next;
 };
 const load=async()=>{if(busy||!current())return;busy=true;more.disabled=true;status.textContent='Consultando compras…';try{const data=await api('purchases?account='+selected.id+(next?'&cursor='+encodeURIComponent(next):''));if(!current())return;for(const item of data.items){if(purchases.has(item.id))continue;purchases.set(item.id,item);let group=groups.get(item.chat.id);if(!group){group={chat:item.chat,items:new Set()};groups.set(item.chat.id,group);}group.items.add(item.id);}next=data.next;render();}catch(e){if(current()){status.textContent=e.message;more.hidden=false;more.textContent='Reintentar';}}finally{busy=false;more.disabled=false;}};
 more.onclick=load;
 const close=()=>{stopMedia();$('management').removeEventListener('close',close);};$('management').addEventListener('close',close);
 await load();
}

(async()=>{try{const token=location.hash.slice(1);if(token){history.replaceState(null,'',location.pathname);await api('login',{token});}const me=await api('me');csrf=me.csrf;panelReviewer=!!me.reviewer;panelAdmin=!!me.admin;batchLimit=me.zip_limit||500;resumeKey='descargas-panel-resume:'+base+':'+me.actor;accountListKey='descargas-panel-accounts:'+base+':'+me.actor;$('locked').hidden=true;$('workspace').hidden=false;favoriteItems=[];accountFavoriteItems=[];hideFavoritesMenu();const restoredPages=restoreCachedAccounts();await loadAccounts();await resumePanel();while(loadedAccountPages<restoredPages&&accountsNext)await loadAccounts(true);accountListReady=true;queueAccountCache();resumeReady=true;queueResume();
 // Every fresh visit begins with an EMPTY name/query field and NO zombie job.
 // This deletes ONLY old search jobs/hits, never a linked Telegram session.
 clearAccountSearchView();
 await api('search-job-clear',{}).catch(e=>notice('Búsqueda previa no borrada: '+e.message));
 // Load small star bookmarks only after accounts and search are usable.
 refreshAccountFavorites().catch(()=>{});
 }catch(e){locked(e.message);}})();
})();
