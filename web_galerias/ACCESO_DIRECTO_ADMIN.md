# Acceso directo de administrador — Web Galerías

## Requisito (una sola vez en Render)
En el servicio **CLOUDFLARE** → **Environment**, agrega:

- `GALERIAS_ADMIN_PASSWORD`: contraseña privada que el administrador elige y escribe personalmente en Render. Nunca enviarla al chat ni ponerla en el repositorio.

**No hay contraseña predeterminada.** Si la variable no existe, el login de navegador falla de forma cerrada. Se recomienda una contraseña larga y exclusiva (Render no impone su longitud). Para cambiarla, cambia solo esta variable; las sesiones de navegador antiguas quedan invalidadas automáticamente.

## Enlace
`https://cloudflare-t2gf.onrender.com/web-galerias/admin`

La página de navegador solicita solo:
- Telegram ID registrado en `admins.txt`.
- La contraseña privada de CLOUDFLARE.

Se genera una cookie `HttpOnly`, `Secure`, `SameSite=Strict`, válida por 180 días desde el último uso y renovada al reabrir la app. No se guarda la contraseña en localStorage ni se entrega el token a JavaScript. Si el navegador borra las cookies, si pasan más de 180 días sin entrar, o si el administrador cambia la contraseña, habrá que iniciar sesión nuevamente. Hay botón **Salir** para revocar el dispositivo.

Los usuarios normales y los de `acceso.txt` NO tienen acceso por contraseña. Su Mini App de Telegram sigue igual. Solo el administrador tiene acceso al panel.

## iPhone
1. Abre el enlace en **Safari**, no dentro de Telegram.
2. Pulsa **Compartir → Añadir a pantalla de inicio**.
3. Abre el icono de Web Galerías; si se solicita, inicia sesión una vez en esa app instalada. En algunos iOS la app instalada usa un almacenamiento de cookies distinto al de Safari.
4. Después abre el icono directamente, sin entrar al bot.

## Android
1. Abre el enlace en **Chrome**.
2. Menú **⋮ → Añadir a pantalla de inicio / Instalar aplicación**.
3. Inicia sesión en la app instalada si se solicita. El icono abrirá directamente el panel.

Se requiere conexión a internet: no se guardan fotos/vídeos privados sin conexión.
Para seguridad, todas las peticiones de administrador exigen cookie válida y cabecera same-origin; solo se aceptan IDs actuales de `admins.txt`. Los intentos fallidos son limitados por ventana de tiempo.

## Despliegue
La funcionalidad se despliega **solo en CLOUDFLARE** (`python galerias_standalone.py`). No modifica sesiones Telegram, los workers ni el servicio web original DESCARGAS.
