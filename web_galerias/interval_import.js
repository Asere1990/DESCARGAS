window.GalleryIntervals = (() => {
 const root = () => document.getElementById("admin-interval-import");
 const labels = {
  queued:"En espera", running:"Importando", counted:"Listo para iniciar",
  paused:"En pausa", stopped:"Detenido", failed:"Requiere atención", done:"Completado"
 };
 let context, polling, mounted, updateNumber = 0, rendered = "";
 let jobList, accountSelect, unlink, count;

 function element(tag, text) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  return node;
 }

 function button(text, action) {
  const control = element("button", text);
  control.type = "button";
  control.className = "small-btn admin-feed-action";
  if (action) control.onclick = action;
  return control;
 }

 function field(label, input) {
  const node = element("label", label);
  node.append(input);
  return node;
 }

 async function act(control, operation, data, refreshFolders = false) {
  control.disabled = true;
  try {
   await context.api("admin-interval-" + operation, data);
   if (refreshFolders) await context.loadAdmin();
   await update(true);
   return true;
  } catch (error) {
   context.notice(error.message);
  } finally {
   control.disabled = false;
  }
 }

 function destination(job) {
  const form = element("form");
  form.className = "admin-history-form";
  const select = element("select");
  select.append(new Option("Crear carpeta nueva", ""));
  for (const folder of context.overview.folders) {
   select.append(new Option(folder.public_name || folder.name, String(folder.id)));
  }
  if (context.overview.folders.length) select.selectedIndex = 1;
  const name = element("input");
  name.maxLength = 100;
  const nameField = field("Nombre de la carpeta", name);
  select.onchange = () => {
   name.required = !select.value;
   nameField.classList.toggle("hidden", Boolean(select.value));
  };
  select.onchange();
  const start = button("Iniciar");
  start.type = "submit";
  start.disabled = !job.photos && !job.videos;
  form.append(field("Destino", select), nameField, start);
  form.onsubmit = event => {
   event.preventDefault();
   act(start, "start", {id:job.id, ...(select.value ? {folder:select.value} : {name:name.value})}, true);
  };
  return form;
 }

 function actions(line, job, accounts) {
  function action(label, operation) {
   const control = button(label, () => act(control, operation, {id:job.id}));
   line.append(control);
  }
  if (["running", "queued", "failed"].includes(job.state)) action("Pausar intervalo", "pause");
  if (["running", "queued", "paused", "failed"].includes(job.state)) action("Detener intervalo", "stop");
  if (["paused", "stopped", "failed"].includes(job.state) && job.uid) action("Reanudar intervalo", "resume");
  if (["paused", "stopped", "failed", "counted", "done"].includes(job.state) && accounts.length) {
   const select = element("select");
   select.append(new Option("Seleccionar cuenta", ""));
   for (const uid of accounts) {
    if (uid !== job.uid) select.append(new Option(uid, uid));
   }
   if (select.options.length > 1) {
    const change = button("Asignar cuenta", () => {
     if (select.value) act(change, "account", {id:job.id, account:select.value});
    });
    line.append(field("Cambiar cuenta del intervalo", select), change);
   }
  }
  const remove = button("Eliminar intervalo", () => {
   if (confirm("¿Eliminar el intervalo y su avance? Los archivos ya publicados se conservan.")) {
    act(remove, "delete", {id:job.id});
   }
  });
  line.append(remove);
 }

 function updateProgress(container, job, number) {
  container.replaceChildren();
  if (job.phase === "count") {
   if (job.state === "running") {
    const pending = element("progress");
    pending.setAttribute("aria-label", "Contando archivos del intervalo " + number);
    container.append(pending);
   }
   return;
  }
  const total = Number(job.photos) + Number(job.videos);
  const copied = Number(job.copied);
  const percent = job.state === "done" ? 100 :
   (total > 0 ? Math.min(99, Math.floor(copied * 100 / total)) : 0);
  const progress = element("progress");
  progress.max = 100;
  progress.value = percent;
  progress.setAttribute("aria-label", "Progreso del intervalo " + number);
  container.append(element("p", percent + "% · " + copied + " de " + total + " archivos copiados"), progress);
 }

 function render(result) {
  const selected = accountSelect.value;
  accountSelect.replaceChildren(new Option("Seleccionar cuenta", ""));
  for (const uid of result.accounts) accountSelect.append(new Option(uid, uid));
  accountSelect.value = result.accounts.includes(selected) ? selected : (result.accounts[0] || "");
  accountSelect.onchange();
  jobList.replaceChildren();
  for (const [index, job] of [...result.jobs].reverse().entries()) {
   const line = element("section");
   line.className = "panel-card";
   line.dataset.intervalJob = job.id;
   const state = job.phase === "count" && job.state === "running" ? "Contando archivos" : labels[job.state];
   const links = element("p", job.first_link + " → " + job.last_link);
   links.className = "interval-links";
   line.append(
    element("h4", "Intervalo " + (index + 1) + " · " + state),
    element("p", (job.uid ? "Cuenta " + job.uid : "Sin cuenta") +
     (job.folder_name ? " · " + job.folder_name : "")),
    links
   );
   if (job.peer) {
    line.append(element("p", job.photos + " fotos · " + job.videos + " videos · " +
      job.albums + " álbumes"));
   }
   const progress = element("div");
   progress.className = "interval-progress";
   updateProgress(progress, job, index + 1);
   line.append(progress);
   if (job.error) line.append(element("p", job.error));
   if (job.state === "counted") line.append(destination(job));
   actions(line, job, result.accounts);
   jobList.append(line);
  }
 }

 async function update(force = false) {
  if (!root().open) return;
  const request = ++updateNumber;
  try {
   const result = await context.api("admin-interval-status");
   if (request !== updateNumber || !root().open) return;
   if (!force && jobList.contains(document.activeElement) &&
       document.activeElement.matches("input,select")) {
    const jobs = new Map(result.jobs.map(job => [job.id, job]));
    for (const [index, line] of Array.from(jobList.children).entries()) {
     const job = jobs.get(line.dataset.intervalJob);
     if (job) updateProgress(line.querySelector(".interval-progress"), job, index + 1);
    }
    return;
   }
   const snapshot = JSON.stringify(result);
   if (force || snapshot !== rendered) {
    render(result);
    rendered = snapshot;
   }
  } catch (error) {
   if (force) context.notice(error.message);
  }
 }

 function mount() {
  const body = root().lastElementChild;
  const form = element("form");
  form.className = "admin-history-form";
  accountSelect = element("select");
  accountSelect.required = true;
  function linkInput() {
   const input = element("input");
   input.type = "url";
   input.maxLength = 500;
   input.required = true;
   input.placeholder = "https://t.me/…";
   return input;
  }
  const first = linkInput();
  const last = linkInput();
  count = button("Calcular intervalo");
  count.type = "submit";
  count.disabled = true;
  form.append(field("Cuenta vinculada a Web Galerías", accountSelect),
   field("Enlace de inicio", first), field("Enlace final", last), count);
  form.onsubmit = async event => {
   event.preventDefault();
   const links = {account:accountSelect.value, first:first.value, last:last.value};
   if (await act(count, "count", links) && first.value === links.first && last.value === links.last) {
    first.value = "";
    last.value = "";
   }
  };
  unlink = button("Desvincular cuenta", () => {
   if (accountSelect.value &&
       confirm("¿Desvincular esta cuenta de Web Galerías? Se conservarán los archivos y el avance.")) {
    act(unlink, "unlink", {account:accountSelect.value});
   }
  });
  accountSelect.onchange = () => {
   count.disabled = !accountSelect.value;
   unlink.disabled = !accountSelect.value;
  };
  jobList = element("div");
  const authorization = context.login(context.overview.history, null, async () => update(true));
  body.append(form, ...authorization, unlink, button("Actualizar", () => update(true)), jobList);
  root().ontoggle = () => {
   clearInterval(polling);
   if (!root().open) return;
   update(true);
   polling = setInterval(() => {
    if (!document.hidden && root().getClientRects().length) update();
   }, 4000);
  };
  if (context.overview.history.pending?.folder === null) root().open = true;
  mounted = true;
 }

 return {
  refreshed(overview, api, notice, login, loadAdmin) {
   context = {overview, api, notice, login, loadAdmin};
   if (!root()) return;
   root().classList.toggle("hidden", !overview.intervals_ready);
   if (overview.intervals_ready && !mounted) mount();
  }
 };
})();
