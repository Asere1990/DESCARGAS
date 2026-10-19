# @Descargas_Robot
Bot de descargas bajo petición con Mini App para conectar voluntariamente la cuenta del propio usuario. Reemplaza el antiguo script de descarga masiva.

## Flujo
1. `/start` → **CONECTAR MI CUENTA**.
2. Mini App: aviso claro del acceso amplio que concede la sesión y consentimiento obligatorio.
3. **Usar mi número de Telegram** solicita permiso mediante `requestContact`, o país y teléfono manuales. El país no se deduce por idioma ni ubicación.
4. Código oculto (opción Ver) y contraseña de dos pasos si la cuenta la exige. Se envían por HTTPS al servidor, nunca como mensajes al bot ni como `sendData`.
5. Se valida que `get_me().id` de la cuenta coincida con el usuario autenticado mediante initData firmado.
6. Enviar hasta 20 enlaces al DM. El servidor descarga usando exclusivamente la sesión de ese usuario y el bot entrega fotos, videos y álbumes nativos al mismo usuario. Se preservan metadatos y miniaturas.
7. `/desconectar` o la Mini App cierran la sesión en Telegram y borran el registro cifrado. El botón del DM pide una confirmación antes de desconectar. Telegram Ajustes → Dispositivos también permite revocarla.

No requiere api_id/hash de cada usuario; usa las credenciales de la aplicación. No desbloquea contenido con Stars. No garantiza acceso a todos los mensajes, ni convierte formatos incompatibles. Límite: 100 MB por elemento y 500 MB por álbum. La exploración del álbum busca el mismo grouped_id entre los 100 ID cercanos por lado.

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
