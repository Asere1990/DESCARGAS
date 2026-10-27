const copy=JSON.parse(document.getElementById('miniapp-copy').textContent);
function t(key,vars={}){const value=copy[key]??'';return value.replace(/\{([A-Za-z_]+)\}/g,(all,k)=>Object.hasOwn(vars,k)?String(vars[k]):all);}
function confirmAction(message,run){
 if(tg?.showPopup)tg.showPopup({title:t('popup.title'),message,buttons:[{id:'yes',type:'default',text:t('popup.accept')},{id:'no',type:'default',text:t('popup.cancel')}]},id=>{if(id==='yes')run();});
 else if(confirm(message))run();
}
const tg=window.Telegram?.WebApp;
const $=id=>document.getElementById(id);
let busy=false, countries=[];
const creatorMode=new URLSearchParams(location.search).get('mode')==='creator';
if(creatorMode){
 $('phone-step').querySelector('h1').textContent=t("creator.title");
 $('phone-step').querySelector('.intro').textContent=t("creator.intro");
 const done=$('done-step');
 done.prepend($('creator-review'));
 for(const selector of ['.success','.eyebrow','h1','.intro'])done.querySelector(selector).hidden=true;
 $('creator-review').hidden=true;
 $('creator-authorize').hidden=true;
}

function show(step){if(creatorMode){$('creator-authorize').hidden=step!=='done';$('creator-review').hidden=step!=='done';if(step==='done')window.scrollTo(0,0);}['phone','code','password','done'].forEach(s=>$(s+'-step').hidden=s!==step);document.querySelectorAll('.steps span').forEach((el,i)=>el.classList.toggle('active',i===(step==='phone'?0:step==='done'?2:1)));$('notice').textContent='';}
async function api(op,data={}){
 let response;try{response=await fetch('api/'+op,{method:'POST',headers:{'Content-Type':'application/json','X-Telegram-Init-Data':tg?.initData||'','X-Miniapp-Mode':creatorMode?'creadores':'cuenta'},body:JSON.stringify(data),cache:'no-store'});}catch(e){throw Error(t('error.network'));}
 let result;try{result=await response.json();}catch(e){throw Error(t('error.request'));}if(!response.ok)throw Error(result.error||t("error.request"));return result;
}
async function perform(fn){if(busy)return;busy=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);$('notice').textContent=t("status.wait");try{await fn();}catch(e){$('notice').textContent=e.message;}finally{busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);}}
function outcome(result){if(result.connected){$('code').value='';$('password').value='';show('done');}else if(result.step){show(result.step);if(result.step==='code')$('notice').textContent=t("status.code_help");}if(result.message)$('notice').textContent=result.message;}
$('country').onchange=()=>{$('prefix').textContent=$('country').value?'+'+$('country').selectedOptions[0].dataset.code:'+';};
$('send').onclick=()=>perform(async()=>{if(!$('consent').checked)throw Error(t("error.consent"));if(!$('country').value)throw Error(t("error.country"));const phone=$('prefix').textContent+$('phone').value.replace(/\D/g,'');outcome(await api('send-code',{phone,consent:true,creator:creatorMode}));});
$('verify').onclick=()=>perform(async()=>{const code=$('code').value.trim();outcome(await api('verify',{code}));});
$('password-send').onclick=()=>perform(async()=>{const password=$('password').value;try{outcome(await api('password',{password}));}finally{$('password').value='';}});
$('back').onclick=()=>show('phone');
document.querySelectorAll('[data-toggle]').forEach(button=>button.onclick=()=>{const input=$(button.dataset.toggle);input.type=input.type==='password'?'text':'password';button.textContent=input.type==='password'?t("button.show"):t("button.hide");});
$('return').onclick=()=>{if(tg)tg.close();};
$('contact').onclick=()=>perform(async()=>{
 if(!tg?.requestContact)throw Error(t("error.manual_phone"));
 const shared=await new Promise(resolve=>tg.requestContact(resolve));
 if(!shared)throw Error(t("error.contact_denied"));
 for(let i=0;i<8;i++){
  const result=await api('contact');if(result.phone){const digits=result.phone.replace(/\D/g,'');const options=countries.filter(c=>digits.startsWith(String(c.code))).sort((a,b)=>String(b.code).length-String(a.code).length);if(options.length){const country=options.find(c=>c.region===result.region)||options[0];$('country').value=country.region;$('country').onchange();$('phone').value=result.national||digits.slice(String(country.code).length);$('notice').textContent=t("status.contact");return;}}
  await new Promise(r=>setTimeout(r,700));
 }
 throw Error(t("error.contact_wait"));
});
(async()=>{tg?.ready();tg?.expand();try{const response=await fetch('countries').catch(()=>{throw Error(t('error.network'));});countries=await response.json().catch(()=>{throw Error(t('error.request'));});const names=new Intl.DisplayNames(['es'],{type:'region'});countries.sort((a,b)=>t('country.'+a.region).localeCompare(t('country.'+b.region),'es')).forEach(c=>{const option=document.createElement('option');option.value=c.region;option.dataset.code=c.code;option.textContent=t('country.option',{name:t('country.'+c.region),code:c.code});$('country').append(option);});if(!tg?.initData)throw Error(t("error.open_bot"));const status=await api('status');outcome(status);}catch(e){$('notice').textContent=e.message;}})();

async function loadMembership(){try{const info=await api('membership');$('membership').hidden=!info.active;if(info.active)$('membership-until').textContent=t('status.membership',{until:info.until});}catch(e){}}
$('cancel-membership').onclick=()=>{const run=()=>perform(async()=>{const result=await api('cancel-membership');$('notice').textContent=result.message;await loadMembership();});confirmAction(t('popup.cancel_membership'),run);};
if(tg?.initData)loadMembership();

$('admin-datos').onclick=()=>perform(async()=>{await api('admin-datos');if(tg)tg.close();});
if(tg?.initData)api('admin-status').then(result=>{$('admin-tools').hidden=!result.authorized;}).catch(()=>{});

async function creatorStatus(){if(!creatorMode)return;const state=await api('creator-status');$('creator-state').textContent=state.message;}
$('creator-authorize').onclick=()=>perform(async()=>{if(!$('creator-consent').checked)throw Error(t("error.creator_consent"));const state=await api('creator-authorize',{consent:true,version:'creator-review-v1'});$('creator-state').textContent=state.message;await creatorStatus();});
if(creatorMode&&tg?.initData)creatorStatus().catch(e=>{$('creator-state').textContent=e.message;});
