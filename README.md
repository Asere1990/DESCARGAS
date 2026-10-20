# @Descargas_Robot
Bot de descargas bajo petición con Mini App para conectar voluntariamente la cuenta del propio usuario. Reemplaza el antiguo script de descarga masiva.

## Flujo
1. `/start` → **CONECTAR MI CUENTA**.
2. Mini App: aviso claro del acceso amplio que concede la sesión y consentimiento obligatorio.
3. **Usar mi número de Telegram** solicita permiso mediante `requestContact`, o país y teléfono manuales. El país no se deduce por idioma ni ubicación.
4. Código oculto (opción Ver) y contraseña de dos pasos si la cuenta la exige. Se envían por HTTPS al servidor, nunca como mensajes al bot ni como `sendData`.
5. Se valida que `get_me().id` de la cuenta coincida con el usuario autenticado mediante initData firmado.
6. Enviar hasta 5 enlaces al DM. El servidor descarga usando exclusivamente la sesión de ese usuario y el bot entrega fotos, videos y álbumes nativos al mismo usuario. Se preservan metadatos y miniaturas.
7. `/desconectar` o la Mini App cierran la sesión en Telegram y borran el registro cifrado. El botón del DM pide una confirmación antes de desconectar. Telegram Ajustes → Dispositivos también permite revocarla.

No requiere api_id/hash de cada usuario; usa las credenciales de la aplicación. No desbloquea contenido con Stars sin comprar; requiere que la cuenta solicitante tenga acceso completo. No garantiza acceso a todos los mensajes, ni convierte formatos incompatibles. Límite: 500 MiB por elemento y 1 GiB por álbum. La exploración del álbum busca el mismo grouped_id entre los 100 ID cercanos por lado.

## Servicio independiente en Render
Inicio: `python bot.py`. URL pública en `DESCARGAS_PUBLIC_URL`; disco persistente montado en `/var/data`.

Variables del servicio DESCARGAS:
- `TOKEN`: token del bot que opera el servicio.
- `ID`: api_id de la aplicación propia.
- `HASH`: api_hash correspondiente al mismo ID.
- `DESCARGAS_SESSION_KEY`: clave Fernet existente. No cambiarla.
- `DESCARGAS_PUBLIC_URL`: URL raíz del servicio.
- `ALMACEN`: ID del destino de las copias.

Para sustituir las credenciales de aplicación, actualizar ID y HASH juntos y guardar/desplegar en Render. El servidor usa los nuevos valores al arrancar sin borrar las sesiones cifradas. Telegram puede exigir reconectar sesiones o rechazar cuentas/aplicaciones restringidas: esta configuración no garantiza recuperar acceso suspendido. Nunca cambiar la clave de cifrado ni borrar el disco durante la sustitución.

La identidad del bot se obtiene de TOKEN. Un cambio de bot reinicia solo los identificadores de actualizaciones y órdenes del bot, no las sesiones de usuarios. Agregar el bot nuevo al almacén y que cada usuario inicie su DM con /start.

El arranque configura comandos y menú automáticamente. `/healthz` indica `ready` sin mostrar usuarios ni secretos.

## Seguridad y operación
- initData validado con HMAC, sin campos duplicados, fecha de autorización de máximo una hora; no se confía en initDataUnsafe ni un user_id enviado por el frontend.
- Códigos, teléfono de verificación y contraseña no se guardan en disco. Los pendientes se cierran a los 10 minutos; los contactos compartidos se descartan también a los 10 minutos.
- Sesiones cifradas con Fernet antes de SQLite. La clave vive en las variables de Render. Archivos privados y sin acceso por rutas web.
- Identidad, bloqueo y trabajos separados por usuario. No usa la cuenta personal del propietario para descargar solicitudes de terceros.
- Máximo 20 conexiones pendientes, 20 órdenes en cola, una orden pendiente por usuario y límites de solicitudes por usuario. Sin reintentos automáticos de subidas ambiguas.
- Al desconectar se invalida la cola pendiente; si ya hay una subida en curso, la desconexión espera su finalización. No se borran mensajes ya entregados.
- Un reinicio no reanuda órdenes; el usuario debe revisar lo recibido y mandar otra orden si corresponde. No garantiza entrega exactamente una vez ante fallas de red.
- No registrar cuerpos, códigos, claves, cookies, initData ni sesiones. Los errores registran únicamente la clase de excepción.
- Guardar la clave Fernet: perderla impide recuperar las sesiones cifradas. La revocación remota fallida se informa y no se presenta como éxito.

## Desarrollo
`pip install -e .`

`python -m unittest discover -s tests -q`

También funciona como servicio independiente con `python bot.py` y `DESCARGAS_PUBLIC_URL` apuntando a la raíz de ese servicio. Requiere un disco persistente para conservar sesiones.


## ALMACEN
Variable `ALMACEN`: ID negativo completo del grupo o canal donde se guarda una copia de cada descarga. El bot debe pertenecer al destino y tener permiso para publicar. Vacía significa copia desactivada.

Tras entregar al usuario, el bot reutiliza los archivos ya subidos a Telegram para guardarlos en ALMACEN, sin descargarlos ni subir sus bytes otra vez. Conserva el álbum nativo; el primer elemento lleva como caption el nombre del solicitante y su Telegram ID, ambos enlazados mediante `tg://user?id=...`. En las copias del almacén ese caption sustituye al original; el DM conserva sus captions originales. Los enlaces al perfil pueden estar sujetos a las restricciones de privacidad de Telegram.

Si falla el almacén, se registra el tipo de error y se informa de que el DM ya fue entregado, sin repetirlo automáticamente. El aviso de la Mini App y del bot informa al usuario del almacenamiento. No se guardan credenciales en ALMACEN.

## Registro de usuarios
Tabla SQLite `profiles` en el mismo disco persistente: uid (Telegram ID verificado), phone (E.164 cifrado con Fernet), phone_country (ISO del número; ZZ si desconocido), calling_code, created_at/updated_at (UTC Unix), consent_version. No es geolocalización ni residencia. Solo se crea tras conexión exitosa, consentimiento e identidad coincidente. Sin backfill silencioso de sesiones anteriores. /desconectar revoca y borra únicamente la sesión; conserva el perfil (ID, teléfono cifrado y país) en la base. No elimina copias de medios ya publicadas en ALMACEN. No hay endpoint público ni exportación de teléfonos. Sin servicio de base de datos adicional.


### Progreso y respaldo independiente
Las transferencias nuevas muestran un solo aviso editable con puntos, porcentaje de bytes descargados/subidos (no una estimación de tiempo), y una nota para que el usuario pueda salir del chat. Los puntos se actualizan cada 1,5 segundos; el porcentaje depende solo de los bytes reales, y se omiten fotogramas si Telegram tiene envíos pendientes; los callbacks de transferencia solo actualizan contadores en memoria. Las entregas desde caché no esperan este aviso. No se envía un aviso separado de «Recibido/en cola».

Al publicar por primera vez un contenido en ALMACEN, se guarda una tarea cifrada y única en `descarga_backups` en la misma transacción que deja la caché disponible. LADRON consume esa tarea por HTTPS autenticado, registra sus `file_id` y bot propietario en `public.descargas_backups` (Supabase de LADRON), y respalda en `/𝐁𝐎𝐓 𝐃𝐄𝐒𝐂𝐀𝐑𝐆𝐀𝐒/<media_key>/01.ext`. La carpeta es determinista y se comprueba el hash de Dropbox. No se introducen identificadores de otro bot en las colas originales de LADRON.

Variables internas: `DESCARGAS_BACKUP_KEY` compartida exclusivamente entre el web DESCARGAS y LADRON, y `DESCARGAS_BACKUP_URL` en LADRON (URL pública del web DESCARGAS). Nunca usar `DESCARGAS_SESSION_KEY` como clave del puente. La conexión de respaldo del bot se guarda cifrada y reutiliza; no se solicitan credenciales adicionales a los usuarios. Los archivos pasan por streaming y disco temporal, sin cargar videos completos en memoria. Un consumidor independiente respalda dos archivos en paralelo después de la entrega y drena hasta tres álbumes seguidos por turno. No es una exportación retroactiva de cachés antiguas.

La tabla de Supabase debe tener RLS activado y acceso solo `service_role`. La migración está en `migrations/descargas_backups.sql`. El despliegue no crea servicios pagados adicionales. El tráfico y espacio utilizados sí crecen con las descargas y los respaldos.


### Moderación de DESCARGAS

Los usuarios incluidos en `admins.txt` pueden usar `/ban ID` y `/unban ID`, en el DM del bot o en ALMACEN. Un ID positivo identifica a un usuario; uno negativo identifica a un grupo/canal. En ALMACEN, responder con `/ban` a cualquier elemento registrado de un álbum bloquea al solicitante original y al grupo de origen; `/unban` por respuesta revierte ambos. En ALMACEN deben escribir desde su cuenta identificable; el modo anónimo no permite comprobar `admins.txt`.

Los bloqueos se guardan en PostgreSQL por bot y se revisan antes de aceptar solicitudes, al resolver el origen y antes de enviar, incluyendo caché. La respuesta al solicitante es exactamente `Bot fuera de servicio`. No expulsa miembros de Telegram ni borra contenido ya entregado. Cada elemento nuevo del almacén conserva una referencia verificable al solicitante y al origen; no se confía en captions para aplicar bloqueos. Los álbumes antiguos se enriquecen al reutilizarlos si existe su registro de backup; si no hay procedencia verificable, el comando pide los IDs por separado.


### Pago mensual y almacenamiento privado

Solo `admins.txt` puede usar `/pay ID`, `/lock ID` y `/unlock ID` en el DM o en ALMACEN. IDs positivos: usuarios. IDs negativos: grupos **o canales**. `/pay` exige una suscripción personal activa al usuario o al solicitar enlaces de ese origen; pagar no desbloquea a otros miembros. Tarifas iguales a ZEUS: 500 Stars cada 30 días con renovación automática, o $6.99 USD por mes mediante Stripe. Los bloqueos `/ban` prevalecen sobre el pago. `/suscripcion` muestra la vigencia y cómo gestionar/cancelar la renovación.

Stripe necesita `DESCARGAS_PAYMENT_LINK`, `DESCARGAS_STRIPE_LINK_ID`, `DESCARGAS_STRIPE_PRICE_ID`, `DESCARGAS_STRIPE_WEBHOOK_SECRET` y `DESCARGAS_BILLING_PORTAL` en el servicio web. Webhook: `/payments/stripe`. Se validan firmas, propietario, producto, importe, recibos duplicados, renovaciones, fallos de pago y cancelaciones. No se activa acceso desde la página de retorno. La Mini App no cambia.

`/lock` permite entregar al usuario, pero guarda los file_id cifrados en `descarga_private_cache`, tabla separada en el PostgreSQL existente. No crea una copia en ALMACEN ni un trabajo de Dropbox. Las repeticiones comprueban acceso y pago antes de enviar con los file_id del propio bot. `/unlock` permite que las nuevas descargas vuelvan al flujo normal; no borra copias históricas ni revierte una transferencia ya enviada. El origen y el usuario bloquean almacenamiento de forma independiente: desbloquear uno no anula el lock del otro.


### Stripe y grupo de confirmación de DESCARGAS

El flujo usa una referencia individual: botonera → Payment Link de 6,99 USD/mes → grupo intermedio → confirmación del Telegram ID → acceso mensual. Entrar sin un pago verificado no concede acceso. Los recibos y las renovaciones se procesan una sola vez; la salida del grupo se programa a los cinco minutos. El bot debe ser administrador del grupo con permiso para restringir miembros. ZEUS no se modifica.

Variables del servicio web: `DESCARGAS_PAYMENT_LINK`, `DESCARGAS_STRIPE_LINK_ID`, `DESCARGAS_STRIPE_PRICE_ID`, `DESCARGAS_PAYMENT_GROUP`, `DESCARGAS_PAYMENT_GROUP_LINK` y el secreto de firma `DESCARGAS_STRIPE_WEBHOOK_SECRET`. El endpoint es `https://descargas-eu1x.onrender.com/payments/stripe`. Suscribir checkout.session.completed, checkout.session.async_payment_succeeded, invoice.paid, invoice.payment_failed y customer.subscription.created/updated/deleted. Sin firma configurada no se ofrece el pago con tarjeta.

`STRIPE_KEY` permite completar el perfil de compra mediante una clave restringida de lectura de Invoice Payments, PaymentIntents y Charges. Se guardan cifrados el Telegram ID asociado, nombre, email, dirección de facturación, marca/tipo de tarjeta, últimos cuatro dígitos y fingerprint cuando Stripe los proporciona. No se guarda PAN ni CVC. El perfil queda pendiente si falta la clave, sin detener las descargas. Activar la recogida de nombre y dirección de facturación en el Payment Link si se requieren esos datos. `DESCARGAS_BILLING_PORTAL` es opcional para el enlace de gestión de suscripciones. No se habilitan impuestos automáticos sin revisar las obligaciones fiscales y los registros de Stripe Tax.


### Cancelación y disputas

Los miembros activos ven la fecha `DD-Mes-AAAA` en español (zona America/New_York), el botón Cancelar membresía en el chat y la sección de membresía al abrir Mi cuenta. `/cancelar_membresia` abre la misma gestión. La cancelación exige confirmación y conserva el período ya pagado. Stars usa editUserStarSubscription; Stripe usa cancel_at_period_end. Para Stripe, STRIPE_KEY requiere también **Subscriptions: escritura** además de las lecturas de Charges, Invoice Payments e Invoices. Un fallo no se informa como cancelación exitosa.

El webhook recibe charge.dispute.created y guarda la disputa para resolverla contra el cargo, factura y suscripción vinculados al Telegram ID. El bloqueo permanente se almacena aparte y /unban no lo elimina. Se reintenta si falta la clave o aún no existe la vinculación; el webhook nunca asigna una disputa por coincidencia de nombre/email. Solo se afectan las suscripciones de DESCARGAS.

Los reembolsos de Stars confirmados por Telegram mediante refunded_payment también crean un bloqueo permanente. Se comprueba el recibo y su propietario, incluidas renovaciones; pedir información o cancelar la renovación no genera ese bloqueo.


### Panel de administradores y estadísticas

Solo los IDs de `admins.txt` pueden consultar `/datos`, `/usuario[s]`, `/grupo[s]`, `/telefono[s] país` y `/comando[s]`, en privado o en ALMACEN. Cada listado de usuarios queda asociado durante 15 minutos a ese administrador y a ese chat; enviar el número de fila abre la ficha con los datos disponibles. No se exponen sesiones ni credenciales. Los nombres son menciones de Telegram y los países se infieren del teléfono (no geolocalización).

Los contadores permanentes se actualizan de forma idempotente junto al checkpoint de entrega, cuentan archivos entregados y también los aciertos de caché. El histórico recuperable de trabajos terminados se incorpora en lotes de 100; no se inventan orígenes que no se guardaron antes. La información anterior puede ser parcial. Los nombres de grupos/canales se toman de la entidad que devuelve Telegram cuando está disponible. Las ganancias son cobros brutos conocidos del bot, sin comisiones/impuestos/disputas de Stripe y sin convertir Stars a USD.

El panel corre fuera del polling y de las colas de descarga: dos consultas concurrentes, máximo 20 pendientes, pool propio de dos conexiones y timeout SQL de cinco segundos. Las consultas son acotadas y los rankings tienen índices. Los admins tienen prioridad dentro de las colas rápida/lenta; no interrumpen envíos que ya comenzaron. No se aumentaron workers ni costes.

`protegidos` contiene un ID canónico por línea (`-100...`), permite comentarios `#` y se aplica en admisión, resolución del origen y antes de cada entrega, también con caché. El archivo se distribuye con el código. Los usuarios de admins.txt están exentos de estas protecciones.

Los workers dejan de tomar trabajos al recibir SIGTERM y esperan hasta 20 segundos por las descargas activas (`DOWNLOAD_DRAIN_SECONDS`, máximo 240). Se reencolan inmediatamente los trabajos cuya fase todavía permite reintento seguro; no se repiten automáticamente los envíos ambiguos. Para ampliar esa espera, el tiempo de apagado de Render debe ser mayor que DOWNLOAD_DRAIN_SECONDS. Mantener PostgreSQL y despliegues con solapamiento, sin disco adjunto al servicio. No se garantiza ausencia absoluta de interrupciones en subidas largas, fallos de red o cambios incompatibles; los envíos ambiguos siguen en revisión para evitar duplicados.

Compatibilidad actual de enlaces: `https://t.me/usuario_publico/123` y `https://t.me/c/123456/789` para grupos/canales a los que la cuenta conectada tenga acceso. No se aceptan invitaciones ni enlaces a conversaciones individuales, Mensajes guardados o enlaces específicos de temas/comentarios. Se admiten fotos, videos, álbumes y documentos según el tipo y los límites configurados; no es garantía de que toda variante de contenido sea descargable. El contenido de Stars solo se procesa si la cuenta solicitante ya tiene acceso a la compra.


`/search telegram_id` abre la ficha administrativa con todos los campos disponibles de perfil, contacto, membresía, políticas, métricas y resumen de la compra (incluidos metadatos de tarjeta proporcionados por Stripe). Nunca muestra códigos, contraseñas, tokens ni sesiones. Disponible solo para admins en privado y ALMACEN; también aparece en /comandos.

`/datos` muestra usuarios registrados, usuarios con interacción en los últimos cinco minutos y usuarios con trabajos preparando/enviando. Telegram no comunica que un usuario simplemente tenga abierto el chat; no se presenta ese dato como medible. Solo se registra actividad de mensajes/botones en privado y peticiones autenticadas de Mi cuenta, con escrituras limitadas a una por usuario cada 30 segundos. El historial anterior no permite reconstruir esta actividad. Los admins también tienen un botón /datos dentro de Mi cuenta; el servidor verifica admins.txt en cada petición.


### Fuentes importadas de CUSTOMER-SERVICE

La lista protege 53 IDs únicos: 26 históricos declarados como valores de MAIN_TO_DERIVED en CUSTOMER-SERVICE/main/bot.py y 27 adicionales del catálogo habilitado de programados y de las sustituciones guardadas en Supabase. Se conserva la protección histórica. Las consultas fueron de solo lectura; CUSTOMER-SERVICE no se modificó. La lista es una instantánea, no una sincronización automática.

`bots_protegidos` permite un ID positivo o @username por línea, con comentarios #. Es preferible usar el ID estable. La comprobación se realiza al admitir aliases, resolver la entidad y antes de entregar un origen de bot identificado. No habilita soporte nuevo para enlaces de conversaciones individuales: solo rechaza el origen protegido si el flujo consigue resolverlo. El archivo contiene los cuatro IDs de bots indicados por el administrador el 20-10-2026.


Actualización de protegidos (20-10-2026): se consultaron en modo de solo lectura GALLERY.gallery_destinations, GALLERY.gallery_scheduled y Telegram Stripe.membership_group_overrides, y se contrastó program_schedules de Bot Ladron. El catálogo habilitado de programados y las sustituciones de membresías aportaron 27 IDs adicionales a los 26 históricos del código: 53 IDs únicos. Se mantienen los anteriores para proteger contenido antiguo. No se cambiaron las bases de datos ni CUSTOMER-SERVICE. Es una instantánea, no sincronización automática. bots_protegidos incluye los cuatro IDs aportados por el administrador.


Los IDs presentes en admins.txt tienen acceso de administrador: no se aplican /ban, bloqueos por disputas/reembolsos, /pay, /lock, protegidos ni bots_protegidos a sus solicitudes. Las reglas almacenadas se conservan y vuelven a tener efecto si se retira ese ID de admins.txt. Se conserva su prioridad de cola. Esta exención no modifica permisos de Telegram, compras no realizadas, compatibilidad de enlaces, límites de tamaño, capacidad ni intervalos de Telegram. Los administradores también están exentos del límite de cinco enlaces por lote.
