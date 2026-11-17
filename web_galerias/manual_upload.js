"use strict";
/* Admin-only uploads to R2. No Telegram sessions, bot queues or Render media proxy. */
(() => {
  const $ = id => document.getElementById(id);
  const picker = $("manual-r2-files");
  const folder = $("manual-r2-folder");
  const go = $("manual-r2-start");
  const status = $("manual-r2-status");
  const progress = $("manual-r2-progress");
  if (!picker || !folder || !go || !status || !progress) return;
  const cachePrefix = "wg_manual_r2_v1:";
  const MIME = {
    jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png",
    webp: "image/webp", heic: "image/heic", heif: "image/heif", avif: "image/avif", mp4: "video/mp4", mov: "video/quicktime",
    webm: "video/webm"
  };
  let busy = false;
  let lastFolders = "";
  const captionPanel = $("manual-r2-caption-panel");
  const captionTitle = $("manual-r2-caption-title");
  const captionCount = $("manual-r2-caption-count");
  const captionText = $("manual-r2-caption-text");
  const captionStatus = $("manual-r2-caption-status");
  const captionSave = $("manual-r2-caption-save");
  const captionSkip = $("manual-r2-caption-skip");
  let pendingUploads = [];
  const setStatus = (message) => { status.textContent = message; };

  function closeCaptionPrompt(message) {
    pendingUploads = [];
    captionPanel.classList.add("hidden");
    captionText.value = "";
    captionStatus.textContent = "";
    go.disabled = busy;
    if (message) setStatus(message);
  }

  function promptCaption(uploads) {
    pendingUploads = [...new Set(uploads)].filter(Boolean);
    if (!pendingUploads.length) return;
    const number = pendingUploads.length;
    captionTitle.textContent = "¿Agregar caption a este lote?";
    captionCount.textContent = number + " archivo" + (number === 1 ? "" : "s") +
      " publicado" + (number === 1 ? "" : "s") +
      ". Puedes asociar un mismo texto para encontrarlos en Buscar.";
    captionText.value = "";
    captionStatus.textContent = "";
    captionPanel.classList.remove("hidden");
    go.disabled = true;
    captionPanel.scrollIntoView({behavior: "smooth", block: "center"});
  }

  captionSave.addEventListener("click", async () => {
    if (!pendingUploads.length) return;
    const caption = captionText.value.trim();
    if (!caption) {
      captionStatus.textContent = "Escribe el caption o elige No agregar caption.";
      return;
    }
    captionSave.disabled = true;
    captionSkip.disabled = true;
    captionStatus.textContent = "Guardando el caption en todos los archivos del lote…";
    try {
      const result = await api("admin-manual-caption", {
        uploads: pendingUploads, caption
      });
      if (result.updated !== pendingUploads.length)
        throw Error("No se confirmó el caption para todos los archivos.");
      closeCaptionPrompt("✓ Caption asociado a " + result.updated +
        " archivos. Ya se pueden encontrar desde Buscar.");
    } catch (error) {
      captionStatus.textContent = "No se pudo guardar: " + error.message +
        " Los archivos siguen publicados; puedes reintentar.";
    } finally {
      captionSave.disabled = false;
      captionSkip.disabled = false;
    }
  });

  captionSkip.addEventListener("click", () => {
    if (!pendingUploads.length || captionSave.disabled) return;
    closeCaptionPrompt("✓ Archivos publicados sin caption. Se mantienen sus nombres buscables.");
  });
  function keyOf(file, folderId) {
    return cachePrefix + [folderId, file.name, file.size, file.lastModified].join(":");
  }
  function saved(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
  }
  function remember(key, value) {
    try {
      if (value) localStorage.setItem(key, value);
      else localStorage.removeItem(key);
    } catch (_) {}
  }
  function kindOf(file) {
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    if (!MIME[ext]) throw Error("Formato no admitido: " + file.name);
    return {kind: ["jpg", "jpeg", "png", "webp", "heic", "heif", "avif"].includes(ext) ? "photo" : "video",
            mime: MIME[ext]};
  }
  function asJpeg(canvas) {
    return new Promise((resolve, reject) => {
      canvas.toBlob(blob => blob && blob.size
        ? resolve(blob)
        : reject(Error("No se pudo generar la miniatura JPEG.")),
      "image/jpeg", 0.82);
    });
  }
  function paintThumbnail(source, width, height) {
    const canvas = document.createElement("canvas");
    canvas.width = 640; canvas.height = 640;
    const ctx = canvas.getContext("2d");
    if (!ctx || !width || !height) throw Error("Vista previa inválida.");
    ctx.fillStyle = "#101828"; ctx.fillRect(0, 0, 640, 640);
    const scale = Math.min(640 / width, 640 / height);
    const w = Math.max(1, Math.round(width * scale));
    const h = Math.max(1, Math.round(height * scale));
    ctx.drawImage(source, (640 - w) / 2, (640 - h) / 2, w, h);
    return canvas;
  }
  async function photoThumbnail(file) {
    const url = URL.createObjectURL(file);
    try {
      const image = document.createElement("img");
      await new Promise((resolve, reject) => {
        image.onload = resolve;
        image.onerror = () => reject(Error("No se pudo abrir la foto."));
        image.src = url;
      });
      const width = image.naturalWidth, height = image.naturalHeight;
      return {blob: await asJpeg(paintThumbnail(image, width, height)), width, height};
    } finally { URL.revokeObjectURL(url); }
  }
  async function videoThumbnail(file) {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    video.muted = true;
    video.preload = "auto";
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    video.setAttribute("webkit-playsinline", "");
    try {
      await new Promise((resolve, reject) => {
        let settled = false;
        const finish = error => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          if (error) reject(error); else resolve();
        };
        const timer = setTimeout(
          () => finish(Error("El teléfono no pudo preparar una miniatura del video.")),
          30000);
        video.onerror = () => finish(Error("El video no se puede leer en este navegador."));
        video.onloadedmetadata = () => {
          const duration = video.duration;
          const target = Number.isFinite(duration) && duration > 0.25
            ? Math.min(1, duration * 0.1) : 0;
          try { video.currentTime = target; } catch (_) { /* loadeddata fallback */ }
        };
        video.onseeked = () => {
          if (video.videoWidth && video.videoHeight) finish();
        };
        video.onloadeddata = () => {
          if (video.videoWidth && video.videoHeight) finish();
        };
        video.src = url;
        video.load();
      });
      const width = video.videoWidth, height = video.videoHeight;
      return {blob: await asJpeg(paintThumbnail(video, width, height)), width, height};
    } finally {
      video.pause();
      video.removeAttribute("src");
      video.load();
      URL.revokeObjectURL(url);
    }
  }
  async function put(url, blob, mime) {
    const response = await fetch(url, {
      method: "PUT", mode: "cors", body: blob,
      headers: {"Content-Type": mime},
      credentials: "omit", cache: "no-store"
    });
    if (!response.ok) throw Error("Cloudflare rechazó una parte (" + response.status + ").");
  }
  async function relayPut(upload, segment, blob) {
    // Same-origin, admin-authenticated fallback. The backend relays only
    // one bounded part to R2; a complete video never enters Render memory.
    const raw = typeof tg === "undefined" ? "" : (tg?.initData || "");
    const headers = {"Content-Type": "application/octet-stream"};
    if (raw) headers["X-Telegram-Init-Data"] = raw;
    else headers["X-Gallery-Web"] = "1";
    const response = await fetch(
      "/web-galerias/admin/manual-r2-bytes/" +
        encodeURIComponent(upload) + "/" + encodeURIComponent(String(segment)), {
      method: "PUT", cache: "no-store", credentials: "same-origin",
      headers, body: blob
    });
    if (!response.ok) {
      const message = (await response.text()).replace(/<[^>]*>/g, " ").trim();
      throw Error(message.slice(0, 180) || "Cloudflare rechazó el fragmento.");
    }
  }

  async function sendFile(file, folderId, label, report, transport = "direct") {
    const {kind, mime} = kindOf(file);
    if (file.size <= 0 || file.size > 2 * 1024 ** 3)
      throw Error("El archivo debe tener entre 1 byte y 2 GiB.");
    report("Preparando miniatura de " + file.name + "…");
    // Preparing thumbnails before the upload avoids publishing empty video cards.
    const thumbnail = kind === "photo"
      ? await photoThumbnail(file)
      : await videoThumbnail(file);
    const cacheKey = keyOf(file, folderId);
    let session = null;
    const earlier = saved(cacheKey);
    if (earlier) {
      try { session = await api("admin-manual-status", {upload: earlier}); }
      catch (_) { remember(cacheKey, null); }
    }
    if (!session) {
      session = await api("admin-manual-start", {
        folder: folderId, filename: file.name, size: file.size,
        width: thumbnail.width, height: thumbnail.height
      });
      remember(cacheKey, session.upload);
    }
    if (session.status === "ready") {
      remember(cacheKey, null);
      report("✓ " + file.name + " ya está publicado.");
      return session.upload;
    }
    if (session.thumb_url) {
      report("Enviando miniatura de " + file.name + "…");
      if (transport === "relay") await relayPut(session.upload, "thumb", thumbnail.blob);
      else await put(session.thumb_url, thumbnail.blob, "image/jpeg");
    }
    if (session.status === "uploading") {
      if (!session.multipart) {
        report("Subiendo " + file.name + "…");
        if (transport === "relay") await relayPut(session.upload, "file", file);
        else await put(session.url, file, mime);
      } else {
        const chunk = session.part_size;
        const partCount = Math.ceil(file.size / chunk);
        const complete = new Map(
          (session.parts || []).map(part => [Number(part.number), Number(part.size)]));
        let sent = [...complete.values()].reduce((a, b) => a + b, 0);
        let next = 1;
        let failure = null;
        const jobs = Array.from({length: Math.min(3, partCount)}, async () => {
          while (next <= partCount && !failure) {
            const number = next++;
            if (complete.has(number)) continue;
            const start = (number - 1) * chunk;
            const end = Math.min(file.size, start + chunk);
            try {
              const part = await api("admin-manual-part-url",
                {upload: session.upload, part: number});
              const slice = file.slice(start, end, "application/octet-stream");
              if (transport === "relay") await relayPut(session.upload, number, slice);
              else await put(part.url, slice, "application/octet-stream");
              sent += end - start;
              report(label + " · " + file.name + ": " +
                Math.min(100, Math.floor(100 * sent / file.size)) + "%");
            } catch (error) {
              failure = error;
              break;
            }
          }
        });
        await Promise.all(jobs);
        if (failure) throw failure;
      }
    }
    report("Verificando archivo y miniatura en Cloudflare…");
    await api("admin-manual-complete", {upload: session.upload});
    remember(cacheKey, null);
    report("✓ Publicado: " + file.name);
    return session.upload;
  }

  function refreshed(admin) {
    if (!admin?.folders) return;
    const folders = admin.folders.filter(f => f.active && !f.parent_folder);
    const signature = folders.map(f => [f.id, f.public_name || f.name]).join("|");
    if (signature === lastFolders) return;
    lastFolders = signature;
    const current = folder.value;
    folder.replaceChildren(new Option("Selecciona una galería…", ""));
    folders.forEach(f => folder.add(new Option(
      (f.public_name || f.name || "Galería") + " · " + f.id, String(f.id))));
    folder.value = folders.some(f => String(f.id) === current) ? current : "";
  }

  go.addEventListener("click", async () => {
    if (pendingUploads.length) {
      setStatus("Primero guarda el caption del lote anterior o pulsa No agregar caption.");
      captionPanel.scrollIntoView({behavior: "smooth", block: "center"});
      return;
    }
    if (busy || !S.me?.admin || !folder.value || !picker.files?.length) {
      setStatus("Selecciona la galería y los archivos que quieres publicar.");
      return;
    }
    const files = Array.from(picker.files);
    if (files.length > 1000) {
      setStatus("Puedes seleccionar hasta 1,000 archivos por tanda.");
      return;
    }
    busy = true; go.disabled = true; progress.value = 0; progress.max = files.length;
    try {
      setStatus("Comprobando conexión a Cloudflare R2…");
      const config = await api("admin-manual-config");
      if (!config.ready) throw Error(config.reason || "Cloudflare no está listo.");
      const transport = config.transport === "relay" ? "relay" : "direct";
      let next = 0, done = 0, failed = 0;
      const notes = [];
      const publishedUploads = [];
      const workers = Array.from({length: Math.min(3, files.length)}, async () => {
        while (next < files.length) {
          const index = next++;
          const file = files[index];
          try {
            const uploaded = await sendFile(file, Number(folder.value),
              (index + 1) + "/" + files.length, text => {
                setStatus(text + " · Finalizados " + done + "/" + files.length);
              }, transport);
            if (uploaded) publishedUploads.push(uploaded);
          } catch (error) {
            failed++;
            notes.push(file.name + ": " + error.message);
          }
          done++; progress.value = done;
          setStatus(done + "/" + files.length + " procesados. " +
            (failed ? failed + " pendientes por reintentar. " : "") +
            (notes.length ? notes[notes.length - 1] : ""));
        }
      });
      await Promise.all(workers);
      if (publishedUploads.length) promptCaption(publishedUploads);
      if (!failed) {
        picker.value = "";
        setStatus("✓ " + done + " archivos publicados. Ya aparecen en Web Galerías.");
        await loadAdmin();
      } else {
        setStatus("✓ " + (done - failed) + " publicados. " + failed +
          " sin terminar: vuelve a elegir los mismos archivos para reanudar. " +
          notes.slice(0, 2).join(" | "));
      }
    } catch (error) {
      setStatus("No se pudo iniciar la carga: " + error.message);
    } finally {
      busy = false; go.disabled = Boolean(pendingUploads.length);
    }
  });

  window.GalleryManualUpload = {refreshed};
})();