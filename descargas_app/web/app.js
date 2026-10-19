const tg=window.Telegram?.WebApp;
const $=id=>document.getElementById(id);
let busy=false, countries=[];
function show(step){['phone','code','password','done'].forEach(s=>$(s+'-step').hidden=s!==step);document.querySelectorAll('.steps span').forEach((el,i)=>el.classList.toggle('active',i===(step==='phone'?0:step==='done'?2:1)));$('notice').textContent='';}
async function api(op,data={}){
 const response=await fetch('api/'+op,{method:'POST',headers:{'Content-Type':'application/json','X-Telegram-Init-Data':tg?.initData||''},body:JSON.stringify(data),cache:'no-store'});
 const result=await response.json();if(!response.ok)throw Error(result.error||'No se pudo completar.');return result;
}
async function perform(fn){if(busy)return;busy=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);$('notice').textContent='Un momento…';try{await fn();}catch(e){$('notice').textContent=e.message;}finally{busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);}}
function outcome(result){if(result.connected){$('code').value='';$('password').value='';show('done');}else if(result.step)show(result.step);if(result.message)$('notice').textContent=result.message;}
$('country').onchange=()=>{$('prefix').textContent=$('country').value?'+'+$('country').selectedOptions[0].dataset.code:'+';};
$('send').onclick=()=>perform(async()=>{if(!$('consent').checked)throw Error('Confirma que autorizas la conexión.');if(!$('country').value)throw Error('Selecciona tu país.');const phone=$('prefix').textContent+$('phone').value.replace(/\D/g,'');outcome(await api('send-code',{phone,consent:true}));});
$('verify').onclick=()=>perform(async()=>{const code=$('code').value.trim();outcome(await api('verify',{code}));});
$('password-send').onclick=()=>perform(async()=>{const password=$('password').value;try{outcome(await api('password',{password}));}finally{$('password').value='';}});
$('back').onclick=()=>show('phone');
document.querySelectorAll('[data-toggle]').forEach(button=>button.onclick=()=>{const input=$(button.dataset.toggle);input.type=input.type==='password'?'text':'password';button.textContent=input.type==='password'?'Ver':'Ocultar';});
$('return').onclick=()=>{if(tg)tg.close();};
$('disconnect').onclick=()=>{const run=()=>perform(async()=>{await api('disconnect');$('consent').checked=false;show('phone');$('notice').textContent='Sesión cerrada y borrada.';});if(tg?.showConfirm)tg.showConfirm('¿Cerrar y borrar tu sesión de DESCARGAS?',yes=>{if(yes)run();});else if(confirm('¿Cerrar y borrar tu sesión?'))run();};
$('contact').onclick=()=>perform(async()=>{
 if(!tg?.requestContact)throw Error('Selecciona tu país e introduce el número manualmente.');
 const shared=await new Promise(resolve=>tg.requestContact(resolve));
 if(!shared)throw Error('Puedes introducir el número manualmente.');
 for(let i=0;i<8;i++){
  const result=await api('contact');if(result.phone){const digits=result.phone.replace(/\D/g,'');const options=countries.filter(c=>digits.startsWith(String(c.code))).sort((a,b)=>String(b.code).length-String(a.code).length);if(options.length){const country=options.find(c=>c.region===result.region)||options[0];$('country').value=country.region;$('country').onchange();$('phone').value=result.national||digits.slice(String(country.code).length);$('notice').textContent='Número completado. Revisa el país y continúa.';return;}}
  await new Promise(r=>setTimeout(r,700));
 }
 throw Error('El número aún no llegó. Introdúcelo manualmente o vuelve a intentarlo.');
});
(async()=>{tg?.ready();tg?.expand();try{const response=await fetch('countries');countries=await response.json();const names=new Intl.DisplayNames(['es'],{type:'region'});countries.sort((a,b)=>names.of(a.region).localeCompare(names.of(b.region),'es')).forEach(c=>{const option=document.createElement('option');option.value=c.region;option.dataset.code=c.code;option.textContent=names.of(c.region)+' (+'+c.code+')';$('country').append(option);});if(!tg?.initData)throw Error('Abre esta página desde el botón CONECTAR MI CUENTA del bot.');const status=await api('status');show(status.connected?'done':'phone');}catch(e){$('notice').textContent=e.message;}})();
