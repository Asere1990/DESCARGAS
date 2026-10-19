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

## Alojamiento compartido en TATICA
Se instala como paquete con una versión fijada por commit. TATICA monta la Mini App en `/descargas/` y comparte su bloqueo de trabajos. No se crea otro servicio ni disco. Una sola transferencia pesada a la vez; el tráfico total puede aumentar los cargos por consumo.

Variables en el **servicio TATICA**:
- `TOKEN`: token de @Descargas_Robot, introducido por el propietario en Render.
- `DESCARGAS_SESSION_KEY`: clave Fernet generada una sola vez (el despliegue la provisiona sin mostrarla). No cambiarla: permite descifrar las sesiones existentes.
- `DESCARGAS_PUBLIC_URL`: `https://tatica.onrender.com/descargas/`.
- Se reutilizan `TELEGRAM_API_ID` y `HASH`.
- Disco: sesiones y estado en `/var/data/descargas`, separado de `/var/data/tatica`.

El arranque configura automáticamente comandos, botón de menú y botones web_app mediante Bot API, una vez añadido el token. Verifica que el token corresponda exactamente a @Descargas_Robot. `/descargas/healthz` indica `ready`; no muestra usuarios ni secretos.

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
