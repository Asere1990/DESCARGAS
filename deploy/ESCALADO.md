# DESCARGAS: activación de la versión escalable

## Estado
Código preparado en `scale-ready`. No se ha cambiado el servicio activo ni contratado recursos nuevos.

## Arquitectura y capacidad configurada
- Un frontend `python scale_web.py`: Mini App existente y recepción de enlaces. Mantener una sola instancia: las autorizaciones pendientes viven en memoria hasta 10 minutos.
- PostgreSQL compartido: sesiones/perfiles cifrados, cola durable, deduplicación y cursores por bot. Ninguna dependencia del disco local para conservar estos datos.
- Dos instancias `python scale_worker.py`, 3 transferencias cada una: hasta 6 usuarios procesados en paralelo. Se puede aumentar el número de trabajadores después de medir memoria y tráfico.
- Hasta 10.000 solicitudes pendientes/activas, máximo 20 enlaces por solicitud, una solicitud activa por usuario. El límite de cola es explícito; al llenarse se pide reintentar.
- El frontend admite 50 conexiones de cuenta pendientes. Las conexiones de prueba ya guardadas pueden importarse si su SQLite original sigue disponible; las perdidas en almacenamiento efímero requieren reconexión.
- Las copias al ALMACEN tienen una cola independiente y ritmo compartido. El usuario recibe su DM sin esperar a que se vacíe el almacén. La copia al grupo puede llegar mucho después si hay demanda elevada.
- No hay promesa de miles de transferencias físicas simultáneas. La velocidad depende de tamaño, red, Telegram, ALMACEN y recursos contratados. La prueba de carga mide aceptación de solicitudes en PostgreSQL, no archivos descargados de Telegram.

## Recuperación
Cada enlace tiene progreso durable. Si un trabajador muere durante preparación, el trabajo vuelve a cola al vencer su latido (180 segundos). Antes de enviar se registra `sending`: tras un fallo ambiguo queda en `review` para no repetir automáticamente algo que Telegram pudo haber entregado. Los envíos no son exactamente una vez frente a fallos de red; los casos inciertos requieren revisión. Esta misma protección se aplica al almacén.

El ID de solicitud y el ID del bot evitan encolar dos veces la misma actualización. Un índice impide trabajos concurrentes del mismo usuario. Bloqueos de PostgreSQL coordinan descargas, autorización y desconexión entre procesos. Al desconectar se cancelan pendientes y se revoca la sesión conservando el perfil.

## Variables
En el frontend se mantienen TOKEN, ID, HASH, ALMACEN, DESCARGAS_SESSION_KEY y DESCARGAS_PUBLIC_URL. Se añade DATABASE_URL interno de PostgreSQL.

Los trabajadores necesitan DATABASE_URL y la MISMA DESCARGAS_SESSION_KEY. El frontend publica su configuración de aplicación cifrada en PostgreSQL; los trabajadores la cargan y reinician de forma controlada cuando cambia. Así TOKEN/ID/HASH se editan solo en el frontend. Nunca cambiar la clave de cifrado.

## Costo propuesto (tarifas consultadas el 19 de octubre de 2026)
- Frontend actual 0.5 CPU / 512 MB: $7/mes.
- Dos trabajadores de 1 CPU / 2 GB: $25/mes cada uno, $50/mes.
- PostgreSQL 0.5 CPU / 1 GB: $19/mes.
- Almacenamiento de PostgreSQL: 5 GB a $0.30/GB/mes, presupuestado en $1.50/mes; confirmar el total del panel antes de activar.
- Base presupuestada: aproximadamente $77.50/mes ($76 de cómputo más $1.50 de almacenamiento), más tráfico excedente, impuestos y otros servicios existentes. El consumo de video puede dominar la factura.
- Una fase inicial con un trabajador en vez de dos reduce el base a aproximadamente $52.50/mes y procesa hasta 3 usuarios a la vez. No activar servicios pagos hasta que el propietario elija/apruebe el gasto.

## Activación
1. Revisar CI en GitHub y fusionar la versión aprobada a main. La entrada antigua `python bot.py` sigue disponible y no cambia automáticamente al modo escalable.
2. Aprovisionar PostgreSQL en Virginia y compartir su URL interna. Antes de aplicarlo revisar `deploy/render-scale.yaml`: reutiliza DESCARGAS y crea DESCARGAS-DB y DESCARGAS-WORKER (2 instancias). No modifica TATICA.
3. Detener la recepción antigua. Si hay SQLite persistente accesible y se quieren conservar cuentas, ejecutar `python migrate_to_postgres.py` con DATABASE_URL, la clave existente y SQLITE_MIGRATION_PATH. No ejecutar desde un pre-deploy de Render: esos procesos no pueden acceder al disco adjunto. El importador no reemplaza cuentas ya existentes en PostgreSQL.
4. Cambiar inicio del frontend a `python scale_web.py`, establecer /readyz como health check y desplegar. Mantener la misma URL pública de la Mini App.
5. Iniciar trabajadores con `python scale_worker.py`. Conservar una sola instancia web; aumentar solo trabajadores.
6. Probar autorización, foto, video, álbum, ALMACEN, desconexión/reconexión y un reinicio controlado con las cuentas del propietario. No anunciar capacidad pública antes de esta verificación.
7. Vigilar colas, errores, trabajos review, disco PostgreSQL y tráfico. Guardar una copia segura de la clave y usar las copias de seguridad de la base de datos.

## Pruebas
`python -m unittest discover -s tests -v`

CI usa PostgreSQL 16 desechable y datos sintéticos. TEST_DATABASE_URL solo acepta localhost y base descargas_test; nunca ejecutar el test contra producción. Incluye 5.000 usuarios y 100.000 enlaces, carreras de duplicados, límites, reclamación paralela, recuperación por fases, sesiones cifradas y cola independiente de ALMACEN. No envía mensajes ni descarga contenido real de Telegram.
