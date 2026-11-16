# Web Galerías: vía adicional TOKEN_WEB

Este módulo **no sustituye ni modifica** el abastecimiento tradicional con
cuentas de DATOS/DESCARGAS. No se ejecuta si no existe TOKEN_WEB en Render.

## Activación

1. Crear un bot nuevo en @BotFather (no reutilizar PUBLICIDAD).
2. En el servicio CLOUDFLARE de Render, guardar el token como \`TOKEN_WEB\`.
   No publicar el token ni colocarlo en archivos del repositorio.
3. Añadir el nuevo bot a cada grupo de Telegram y darle el permiso de
   administrador para que pueda recibir fotos, videos y álbumes sin depender
   del modo de privacidad.
4. Si las publicaciones van a llegar **desde otros bots**, activar también
   «Bot-to-Bot Communication Mode» en @BotFather. Telegram puede no entregar
   esas publicaciones si ese modo está desactivado.
5. En el panel administrativo Web Galerías, abrir una carpeta y pulsar
   **«Agregar nuevo Group ID»**. Escribir el ID negativo completo.
6. Repetir para los demás grupos. Cada Group ID pertenece a una sola carpeta.

## Comportamiento

- Se reciben nuevos mensajes de grupo o canal a través del Bot API oficial.
- Fotos, videos, álbumes, GIF/animaciones y documentos de imagen/video
  compatibles se copian de Telegram directamente al servidor y Cloudflare R2;
  nada pasa por el iPhone.
- **Menos de 20 MB por archivo.** Los archivos superiores quedan omitidos
  con estado registrado.
- Los captions originales se conservan. Cuando solo una foto del álbum tiene
  caption, todas las fotos del álbum se indexan para buscarlo, sin modificar el
  caption propio de cada foto.
- El archivo solo aparece para los usuarios cuando R2 confirma el original y
  la miniatura, y después se registra en \`wg_media\` como \`ready\`.
- Quitar o sustituir Group ID corta futuras entradas y deja intactos todos
  los archivos ya guardados.
- Cada archivo se identifica por \`(group_id,message_id)\` para no repetirlo
  tras reinicios. Los números de mensajes internos no se cruzan con el
  sincronizador tradicional ni con las cargas manuales.
- Mensajes solo de texto no se incorporan como archivos a la galería.

## Aislamiento

La nueva vía usa solo \`wg_bot_feed_*\` (nuevas tablas), una tarea independiente
de baja concurrencia y un único token opcional. No cambia las tablas de
sesiones de cuentas, Stripe, los bots existentes ni los cursores de la
sincronización tradicional. Los despliegues deben limitarse a CLOUDFLARE.

Si hay errores de conexión o el bot pierde acceso al grupo, la fila queda
pendiente o fallida en la cola del bot, sin afectar las otras vías.
