# Validación de la edición pública

Fecha: **2026-10-28**. Entorno utilizado: Linux, Python **3.12.14**, SQLite **3.53.1**
con FTS5. Esta es la validación de la selección pública, no de todos los archivos
del snapshot privado ni de una instalación de modelos real.

| Comprobación | Resultado |
| --- | --- |
| Demo con dos tareas dependientes y memoria sintética | Correcta; integridad SQLite `ok`, cita de nota presente, embeddings apagados. |
| Pruebas de cola SQLite | **52/52 comprobaciones**. |
| Pruebas del cliente contra HTTP/SSE simulado | **13/13 comprobaciones**. |
| Pruebas aisladas de memoria | **10/10 tests**. |
| Pruebas aisladas de políticas y entradas bloqueadas | **7/7 tests**. |
| Compilación sintáctica de fuentes Python | **18 archivos**. |
| Lectura de JSON y TOML públicos | **5 JSON y 1 TOML** válidos. |
| Sintaxis de `pruebas/humo.sh` | Correcta. |

Las pruebas de cola comprueban configuración SQLite, dependencia, prioridad/afinidad,
huellas, restricciones del journal, archivado y recuperación. Las interrupciones
afectan únicamente procesos hijos lanzados por la propia prueba, con bases
temporales. No simulan todos los fallos posibles de almacenamiento.

El cliente se prueba con un servidor efímero en `127.0.0.1`, con respuestas SSE
sintéticas: éxito, respuesta vacía, stream incompleto, HTTP 503, HTTP 400 y límite
de reintentos. No se cargaron, descargaron ni contactaron modelos reales.

La memoria se prueba en directorios temporales: procedencia, propuestas, rechazo
de escritura directa con origen de modelo, indexación incremental, índice obsoleto,
exclusión de archivos de entorno, límites de raíz y symlinks, consulta sin red y
desactivación explícita de embeddings. La prueba bloquea llamadas HTTP durante
su ejecución. Las etiquetas de origen siguen siendo una política, no autenticación.

Las pruebas de publicación verifican que la allowlist vacía o corrupta no autoriza
comandos, que los presupuestos limitan pasos/repeticiones y que la construcción del
orquestador y la entrada del verificador no alcanzan acciones reales. También cargan
las seis plantillas y cuatro esquemas sin llamar a modelos.

## Revisión del contenido publicado

Se aplicó una selección explícita de archivos y revisión de referencias privadas.
Un escaneo por patrones de tokens habituales, claves privadas, JWT, URLs con
credenciales y rutas del equipo original no encontró coincidencias en el paquete
publicable. Esa revisión no equivale a demostrar ausencia universal de secretos ni
a una auditoría completa de seguridad.

No se probaron descargas de modelos, integración autónoma, GPUs, endpoints de
embeddings/reranking reales ni versiones distintas de las indicadas. Los guardianes
de recursos no se certificaron para hardware ajeno al entorno de pruebas.
