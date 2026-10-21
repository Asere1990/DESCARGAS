# Worker exclusivo de ALMACEN

Servicio a crear: **DESCARGAS-ALMACEN**, tipo Background Worker, Python, región Virginia, repositorio https://github.com/Asere1990/DESCARGAS, rama main.

- Build: `pip install -r requirements.txt`
- Start: `python scale_export.py`
- Una instancia, mismo tamaño que el worker actual del repositorio: `1c-2g`.
- `DATABASE_URL`: conexión existente de DESCARGAS-DB.
- `DESCARGAS_SESSION_KEY`: la misma clave existente de DESCARGAS, sin regenerarla.
- `DB_POOL_SIZE=2`; el exportador mantiene otro pool limitado a cuatro conexiones.
- Sin disco persistente ni HTTP; el avance y las exclusiones están en PostgreSQL.

No duplicar el servicio web ni ampliar los workers de usuarios para alojar esta tarea. Confirmar primero el workspace de Render, comprobar los servicios y sus tamaños reales y copiar las variables sin exponer sus valores. Al quedar listo, verificar en logs el rol export y que los workers de usuarios cedan las exportaciones al nuevo rol. La marca dedicated_export se activa solo al arrancar y autenticar este worker; mientras no exista, el trabajo administrativo conserva el modo de fondo con prioridad para usuarios.
