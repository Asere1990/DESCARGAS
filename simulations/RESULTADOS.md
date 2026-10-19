# Simulación de espera de 1.000 usuarios

Ejecutar `python simulations/demand.py` reproduce `results.json`. Es un modelo de eventos discretos: no se han utilizado cuentas, enlaces ni archivos reales de Telegram. No se han cambiado servicios ni planes de Render.

## Objetivo
Todos los usuarios mandan al mismo tiempo un enlace a un archivo. Se evalúa la meta estricta de menos de 60 segundos para el último usuario, tanto para empezar a procesar como para recibir el primer archivo. No es una media ni una garantía solo para el 95% de los usuarios.

## Supuestos
- Usuarios ya conectados y con acceso al contenido.
- Preparar, descargar y subir cada archivo requiere 30 segundos por tarea, independientemente del número de tareas. No es un tiempo medido.
- Capacidad de red agregada ilimitada. No se incluyen disputas de CPU o RAM, pérdida de red, FloodWait ni saturación de PostgreSQL. Por tanto, el modelo resulta optimista respecto a una instalación real.
- Se reproducen la recepción serial de actualizaciones con aviso de recibido, las pausas de dos segundos, las confirmaciones al acabar, el limitador compartido de 0.06 segundos y la cola de almacén con dos consumidores.
- Las latencias sintéticas de base de datos y API son 5 ms y 50 ms respectivamente. La entrega de multimedia añade 100 ms después de su turno de envío.
- El ritmo de 0.06 segundos procede del código preparado; no se presenta como el límite oficial de Telegram.

## Resultados con un archivo por usuario (30 segundos supuestos)

| Configuración modelada | Último usuario: comienza | Último primer archivo recibido |
|---|---:|---:|
| Estructura preparada, 6 tareas | 88 min 57 s | 89 min 27 s |
| Estructura preparada, 60 tareas hipotéticas | 8 min 45 s | 9 min 15 s |
| Estructura preparada, 200 tareas hipotéticas | 2 min 42 s | 3 min 12 s |
| Estructura preparada, 1.000 tareas hipotéticas | 2 min 6 s | 2 min 52 s |
| Variante hipotética sin aviso inicial/final y turnos por enlace, 600 tareas | 56 s | 91 s |

El cálculo simplificado anterior de 83 minutos omitía pausas, avisos y competencia por el limitador de envíos. Este modelo incluye esos efectos.

**La configuración preparada de $77.50/mes no satisface esta meta para una ráfaga de 1.000 usuarios.** Aumentar trabajadores por sí solo tampoco basta: llega un punto en que domina la recepción y el limitador compartido. No se recomienda contratarla suponiendo menos de 60 segundos de espera para todos.

La variante de 600 tareas consigue que todos comiencen antes de un minuto bajo los supuestos, pero no que todos reciban el archivo antes de un minuto. Es una simulación de una posible arquitectura modificada, no una implementación activada ni una recomendación de contratar 600 tareas. Ni su capacidad de red ni sus límites de Telegram han sido medidos.

## Lotes y equidad
Se modeló también un caso adverso: 50 usuarios con 20 archivos, seguidos de 150 con tres y 800 con uno, con 60 tareas. Mantener ocupado un trabajador durante toda la orden deja al último usuario empezando alrededor de 19 min 56 s. Dar turnos por enlace y suprimir los avisos modelados reduce ese inicio a unos 8 min 36 s. Sigue sin alcanzar la meta. Ceder el turno por archivo sirve para impedir que un lote grande acapare todo un trabajador.

## Capacidad de red: orden de magnitud
1.000 archivos de 20 MB implican aproximadamente 20 GB descargados y 20 GB subidos. Para completar todos en 60 segundos harían falta como mínimo unos 667 MB/s agregados (5.3 Gb/s), ignorando protocolos y toda otra latencia. Con archivos de 100 MB el mínimo sería aproximadamente 26.7 Gb/s. No se afirma que Render entregue ese ancho de banda en los planes propuestos.

## Qué falta medir antes de prometer menos de 60 segundos
Usar archivos de prueba autorizados de distintos tamaños y medir la conexión, descarga, subida y entrega; repetir con concurrencia creciente para ver cómo se comparte el ancho de banda. Después ajustar el modelo a esos datos, separar recepción y avisos del procesamiento y dar turnos por enlace. Mantener el almacén asíncrono. Validar primero el tiempo al primer archivo para cada usuario y después el de la orden completa. No aumentar límites de envío de Telegram por suposición.

Fuentes internas: `descargas_app/scalable.py`, `descargas_app/pg_store.py`, `descargas_app/service.py`. Los resultados son sintéticos y reproducibles; no sustituyen una prueba de transferencias reales.
