# Web Galerías — instalación, recuperación y protección del sistema original

## Prioridad de seguridad
- NUNCA borrar/modificar las sesiones de Telegram en descarga_accounts.
- NUNCA cambiar DESCARGAS_SESSION_KEY ni DATABASE_URL sin un proceso de migración y restauración.
- NUNCA cambiar el TOKEN principal de DESCARGAS para añadir nuevos bots.
- Web Galerías vive en rutas /web-galerias separadas, no modifica las rutas actuales.
- El botón del DM se agrega después de los dos botones actuales y solo para admins.txt/acceso.txt.
- GALERIAS_PUBLIC=0 bloquea todo acceso general durante las pruebas.
- GALERIAS_SYNC_ENABLED=0 evita cualquier consulta nueva a cuentas conectadas.
- Solo habilitar la sincronización después de comprobar disponibilidad R2, carga Telegram y copias de seguridad.

## Variables de Render (servicio web, sin tocar los workers existentes)
- GALERIAS_PUBLIC=0 (piloto cerrado); 1 solo cuando se publique.
- GALERIAS_SYNC_ENABLED=0 (lectura extra de Telegram desactivada durante piloto).
- GALERIAS_R2_BUCKET: bucket privado.
- GALERIAS_R2_ENDPOINT: URL S3 compatible (HTTPS).
- GALERIAS_R2_ACCESS_KEY_ID y GALERIAS_R2_SECRET_ACCESS_KEY: claves del bucket, no del token de Telegram.
- GALERIAS_SYNC_DELAY_SECONDS=35: pausa entre operaciones de respaldo.
- GALERIAS_MAX_BACKUP_MB=128: límite por elemento al inicio.
- GALERIAS_BOT_TOKENS: tokens adicionales solo para verificar initData de Telegram. NO abre otro proceso de polling.
- GALERIAS_STRIPE_KEY: clave secreta con permisos para Checkout y subscriptions.
- GALERIAS_STRIPE_PRICE_ID: precio recurrente exclusivo de Web Galerías.
- GALERIAS_STRIPE_WEBHOOK_SECRET: firma del webhook exclusivo /web-galerias/stripe.

## Pasos de recuperación
1. Comprobar si la base Postgres de DESCARGAS y el almacenamiento externo de medios están disponibles.
2. Si falla la Mini App, redesplegar el commit estable de GitHub en Render, conservando variables y claves. Las carpetas, permisos, pagos y configuración del editor viven en wg_* de PostgreSQL, no en el despliegue.
3. Si falla un bot adicional, cambiar su token únicamente en GALERIAS_BOT_TOKENS y actualizar el botón o comando en el programa propietario de ese bot. Agregar solo un token no cambia por sí solo sus mensajes /start. No ejecutar un segundo getUpdates con el mismo token.
4. Si se reemplaza el bot DESCARGAS, NO cambiar su TOKEN de producción sin preparar antes la migración de los jobs del bot. En cambio, añadir un bot independiente conectado a Web Galerías.
5. Si se pierde acceso a un grupo Telegram, los archivos con estado ready seguirán disponibles desde R2. La sincronización puede encontrar otra cuenta vinculada, si la hay, sin alterar las sesiones.
6. Si se pierde la base Postgres, restaurar una copia de la base de datos antes de abrir acceso público. El almacenamiento externo conserva bytes, pero sin el índice de metadatos no puede reconstruirse automáticamente la misma experiencia.

## Respaldos
- Configurar backups/snapshots periódicos de PostgreSQL (incluyendo tablas wg_* y assets).
- Guardar y probar restauraciones de claves Render, bucket privado R2 y snapshots.
- Verificar la replicación externa del catálogo antes de prometer recuperación total.
- Ocultar una carpeta solo cambia wg_folders.active (visibilidad para clientes). No borra objetos R2 ni detiene la catalogación y el respaldo mientras GALERIAS_SYNC_ENABLED=1 y la cuenta de Telegram esté disponible.
- En una cuenta con muchos usuarios no prometer cero impacto de Telegram: mantener ritmo bajo, locks y suspensión de copia si la cuenta está ocupada.

## Fase privada
admins.txt de DESCARGAS tiene autoridad administrativa; acceso.txt autoriza usuarios normales de prueba.
Faltan los dos Telegram ID de prueba. El administrador puede empezar a configurar nombres, carpetas, video e interfaz.
No se procesan pagos reales sin las tres variables de Stripe. No hay respaldo externo real sin configurar R2.
