# Web Galerías: fuentes aisladas DESCARGAS + DATOS

**Alcance:** únicamente el proceso independiente `python galerias_standalone.py` del
servicio Render **CLOUDFLARE**. El bot, los workers, las colas, las membresías
y las sesiones de DESCARGAS y DATOS no cambian.

## Conexión privada (no subir credenciales a GitHub ni enviarlas por chat)

Conservar todas las variables actuales del servicio CLOUDFLARE y **añadir
únicamente en Render > CLOUDFLARE > Environment**:

- `DATOS_DB`: URL **interna** del PostgreSQL
  `DESCARGAS-DB-datos` (no `DESCARGAS-DB`).
- `DATOS_KEY`: misma clave Fernet
  `DESCARGAS_SESSION_KEY` configurada **en el servicio DATOS**,
  nunca la clave del servicio DESCARGAS.

No hay que cambiar variables en DATOS ni en DESCARGAS ni reiniciar sus workers.
No introducir `DATOS_DB` ni `DATOS_KEY` con valores privados en el repositorio.
Los antiguos nombres `GALERIAS_DATOS_DATABASE_URL` y `GALERIAS_DATOS_SESSION_KEY`
seguirán admitiéndose durante la transición, pero no hace falta utilizarlos.

Si faltan los datos o fallan las credenciales, el lector DATOS queda deshabilitado
y CLOUDFLARE **sigue utilizando DESCARGAS como antes**, sin cambiar el flujo.

## Cómo se aísla el sistema

- DATOS tiene un pool PostgreSQL independiente (máx. tres conexiones).
- El lector externo solo consulta cuentas/sesiones y configuración cifrada.
  No ejecuta migraciones, no modifica contraseñas, sesiones ni el esquema DATOS.
- Cada fuente consulta sus propias colas y toma los locks de sesión
  de su propia base. Los clientes Telethon son solo de lectura y
  `receive_updates=False`; no hacen polling del bot.
- En el registro interno de galerías, `uid > 0` pertenece a DESCARGAS,
  `uid < 0` a DATOS y su Telegram ID real es `abs(uid)`.
  Esta codificación solo existe dentro de las tablas `wg_*`.
- Si un mismo Telegram ID existe conectado en las dos bases, se elige
  DESCARGAS para ese ID. Así no se usa esa sesión simultáneamente en
  bases con locks diferentes.
- Carpetas y mensajes siguen únicos por grupo/canal y message ID.
  No se duplican los originales ni miniaturas R2.
- Las galerías se distribuyen por cuenta en carriles paralelos, limitados
  globalmente por `GALERIAS_SYNC_PARALLEL_ACCOUNTS` (1–4, 3 de inicio).
  Se preserva la prioridad de catálogo/miniaturas visibles antes del
  respaldo original, con turnos cortos y justos.
- Si una cuenta tiene trabajos de descarga pendientes en su sistema,
  el lector de galerías cede el turno a ese sistema.

## Panel administrador

Para fijar una cuenta concreta del historial:

- DESCARGAS: `123456789` o `DESCARGAS:123456789`.
- DATOS: `DATOS:123456789`.

El servidor comprueba que la sesión esté vinculada en la base seleccionada
antes de cambiar la carpeta. El cambio conserva los respaldos R2 ya completos,
revisa el historial y reintenta los archivos antes inaccesibles.

## Comprobación y recuperación

1. Verificar en logs de CLOUDFLARE: `DATOS reader ready`.
2. Confirmar que DATOS y DESCARGAS no registran cambios ni reinicios.
3. Fijar una carpeta de prueba a `DATOS:<telegram_id>` y comprobar
   contador, indexación, miniatura y respaldo tras la vista previa.
4. Probar dos carpetas con cuentas distintas, una en cada fuente, y
   confirmar que los carriles avanzan simultáneamente y sin floodwaits.
5. Deshacer la integración **solo en CLOUDFLARE** retirando las dos variables
   `DATOS_DB` y `DATOS_KEY` o restaurando la versión de CLOUDFLARE anterior.
   Las sesiones originales y los archivos R2 permanecen intactos.

**Importante:** Una cuenta conectada en ambos sistemas no duplica la velocidad.
El paralelismo adicional procede de cuentas Telegram *distintas* de DATOS.
Una sola carpeta tiene un lector a la vez para proteger cursores y copias.
