# Caption + replies + OCR de Web Galerías

El índice de búsqueda **existente** \`wg_media.search_text\` sigue siendo la única
fuente consultada por el buscador. No consulta Telegram, Cloudflare R2 ni motores
OCR durante una petición del usuario.

## Origen de las palabras

- **Caption:** \`wg_media.label\`; se asocian todos los captions del álbum.
- **Replies:** \`wg_replies\` guarda exclusivamente mensajes que responden a otro
  mensaje del mismo chat (también hasta tres niveles de respuestas encadenadas).
  Se propagan a todas las fotos/vídeos/GIF con el mismo \`grouped_id\`.
- **OCR:** \`wg_media.ocr_text\` guarda el texto reconocido de una miniatura.
  El índice materializado propaga el texto OCR a todo el álbum.

Cada archivo mantiene separadamente las tres fuentes en
\`search_caption\`, \`search_replies\`, \`search_ocr\`. Su unión normalizada se
materializa en \`search_text\` y mantiene el índice GIN. Cada revisión se
respalda de forma idempotente mediante \`wg_media_text_outbox\` en un JSON
privado Cloudflare R2: \`folders/<folder>/<message>/search-metadata-v1.json\`.
Es posible restaurar las tres fuentes con \`restore_from_r2\`.

## Prioridad / retrocompatibilidad

1. Las consultas y galerías del cliente conservan su código anterior.
2. El catálogo hace una lectura normal de mensajes y guarda los replies.
   Una cola diferida materializa captions, replies y OCR sin retrasar previews.
3. Otra cola escribe metadatos JSON en R2 con reintentos y comprobación de
   revisión; nunca toca las copias originales ni los tokens del bot.
4. Un recorrido histórico separado, pausado por los locks existentes de
   Telethon, recupera replies antiguos sin reiniciar el catálogo.
5. El OCR **no se ejecuta al buscar** y no es necesario para que captions o
   replies funcionen.

## OCR opcional, CPU local, sin API ni cargo por imagen

El módulo \`web_galerias.ocr_low_priority\` usa el motor libre RapidOCR si
está instalado. Ejecuta **un solo proceso de prioridad reducida**, toma una
miniatura de R2 y guarda únicamente su texto reconocido. No descarga un vídeo
completo para OCR.

Para habilitarlo solo en CLOUDFLARE, cambia el Build Command de ese servicio
de Render a:

\`\`\`bash
pip install -r requirements_gallery_ocr.txt
\`\`\`

Configura \`GALERIAS_OCR_ENABLED=1\` **solo en CLOUDFLARE** y comprueba su
uso de RAM/CPU antes de ampliar el ritmo. La instalación no se realiza por
defecto, para evitar que DESCARGAS y DESCARGAS-WORKER reciban dependencias de
reconocimiento pesado en futuras compilaciones. Si no se activa, el campo OCR
permanece vacío hasta que un motor OCR lo procese; no se inventan resultados.

Limitación de Telegram: replies de **otros chats** o del grupo de discusión
asociado a un canal no aparecen por consultar solo el canal original.
Se enlazan replies que se obtienen del mismo grupo/canal de origen. El álbum
se reconoce por su \`grouped_id\` real de Telegram, no por su texto.
