# CLOUDFLARE — conexión segura con DESCARGAS (piloto)

## Servicio de Render
- Nombre: CLOUDFLARE.
- Código: repositorio DESCARGAS, arranque python galerias_standalone.py.
- Modo: GALERIAS_PUBLIC=0, GALERIAS_SYNC_ENABLED=0, auto-deploy desactivado.
- Almacenamiento: Cloudflare R2, bucket privado galeria, clase Standard.

## Variables de entorno en CLOUDFLARE
Las credenciales secretas se introducen SOLO en Render > CLOUDFLARE > Environment, nunca en GitHub ni en el chat.

### R2 — ya configurado
- CLOUD_ID: Access Key ID del token R2
- CLOUD_SECRET: Secret Access Key del token R2
- CLOUD_R2: endpoint S3 completo https://<ACCOUNT>.r2.cloudflarestorage.com
- GALERIAS_R2_BUCKET=galeria

### Conexión privada con DESCARGAS — copiar sin cambiar el original
- DATABASE_URL: copiar el valor original de DESCARGAS (Postgres DESCARGAS-DB, no DATOS)
- DESCARGAS_SESSION_KEY: copiar exactamente la clave Fernet de DESCARGAS. NO regenerarla
**No hace falta copiar el token, el API ID ni el API Hash del bot de DESCARGAS.**
Su servicio web guarda la configuración de Telegram cifrada bajo la clave runtime_config de su base de datos. CLOUDFLARE la lee al arrancar y verifica las firmas con el mismo bot, sin iniciar otro polling ni exponer los secretos.

La variable GALERIAS_AUTH_BOT_TOKEN sigue disponible como alternativa opcional para un bot separado o recuperación manual, pero no es obligatoria al compartir DATABASE_URL y DESCARGAS_SESSION_KEY.

CLOUDFLARE conecta a sesiones existentes exclusivamente bajo el account_lock compartido y nunca reescribe, desconecta ni revoca ninguna sesión. GalleryRepository.start NO ejecuta migraciones de DESCARGAS; solo comprueba lectura y crea tablas wg_* en el arranque de la galería.

### Precio de membresía — privado inicialmente
- GALERIAS_STRIPE_PRICE_ID=price_1UaHxOKjV2vzEefKTAdGJZej — 𝐓𝐞𝐥𝐞𝐠𝐫𝐚𝐦® $6.99/mes USD en Stripe
- GALERIAS_STRIPE_KEY: clave privada Stripe, pendiente
- GALERIAS_STRIPE_WEBHOOK_SECRET: secreto del webhook separado /web-galerias/stripe, pendiente

Nunca habilitar GALERIAS_PUBLIC=1 hasta que renovaciones, fallos de pago y permisos estén probados.

## Estado y activación
1. Con variables privadas configuradas, Render reinicia el servicio.
2. La ruta /readyz debe responder {"ready":true} y HTTP 200.
3. La sincronización está apagada hasta agregar carpetas desde el panel admin y comprobar acceso.
4. Activar GALERIAS_SYNC_ENABLED=1 solamente después de probar una carpeta, estimar el consumo y comprobar el efecto en las sesiones.
5. Si una sesión pierde acceso al grupo, conservar archivos ya respaldados y probar otras cuentas autorizadas.
6. Para detener toda carga adicional en Telegram, establecer GALERIAS_SYNC_ENABLED=0. No requiere cerrar sesiones.

## Mini App y nuevos bots
- URL separada: https://cloudflare-t2gf.onrender.com/web-galerias
- El botón existente en DESCARGAS todavía apunta a la Web Galerías del servicio original. Cambiarlo requiere una actualización deliberada del menú, sin tocar los botones existentes y sin segundo polling.
- Los otros bots requieren un botón agregado a sus propios menús; añadir un token en Render no cambia automáticamente /start de cada bot.
