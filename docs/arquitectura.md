# Arquitectura del snapshot histórico

## Propósito

El diseño divide el trabajo en objetivos y tareas, busca contexto en memoria y llama
a modelos mediante HTTP. Cada tarea conserva estado e historial. La edición pública
permite ejecutar piezas aisladas; la integración autónoma está desactivada.

## Cola durable

`Cola` almacena objetivos, tareas, dependencias, intentos, eventos y huellas en SQLite.
Usa WAL, sincronización FULL y claves foráneas. `siguiente()` reclama una tarea dentro
de una transacción y considera dependencias, prioridad, afinidad de modelo y
envejecimiento de la espera. `reanudar()` recupera intentos interrumpidos. El archivado
de eventos usa archivos comprimidos.

Los triggers restringen cambios ordinarios del journal; quien controla la base puede
modificarlo. La configuración de durabilidad no garantiza resistencia a cualquier fallo
de hardware o del sistema de archivos.

## Cliente de inferencia

`Motor` emplea la biblioteca estándar para HTTP y streaming SSE. Reúne texto, lee uso
de tokens y detecta ciertos finales incompletos. Reintenta respuestas vacías, algunos
errores del servidor y streams truncados con un máximo configurable; errores 4xx se
devuelven sin repetir la petición.

Contiene métodos para salida estructurada, embeddings, reranking y consulta de
modelos cargados. Dependen del contrato del servicio conectado. Las pruebas públicas
comprueban chat/reintentos con respuestas simuladas, no todos los endpoints ni las
capacidades de un modelo concreto.

La restricción histórica de parámetros como `max_tokens` se conserva como parte
de la API del snapshot. No es una recomendación general para otras aplicaciones.

## Memoria

`Notas` guarda apuntes con fecha, origen y cita, permite propuestas y mantiene memoria
curada. `Lexica` indexa texto por líneas con FTS5 y detecta algunas diferencias entre el
índice y el archivo actual. `Semantica` almacena vectores en SQLite y usa embeddings de
un servicio configurado explícitamente. `Memoria.recordar()` combina resultados con
procedencia, dando prioridad a coincidencias léxicas.

La edición pública deja vacío el modelo semántico. Limita la indexación a la raíz
elegida, resolviendo symlinks, y omite archivos de entorno y directorios privados
habituales. No es una detección completa de datos sensibles. Las etiquetas de origen
no autentican a quien escribe.

## Orquestación propuesta, detenida

El ciclo original proponía planificación, trabajo, señales de verificación, reparación,
crítica y juicio final. Se incluyen seis plantillas y sus esquemas. Los roles no
otorgan por sí mismos permisos ni herramientas.

El texto generado y los archivos comprobados no estaban ligados suficientemente.
Algunas señales podían importar código o ejecutar shell fuera de la allowlist.
La entrada de `Colmena` está bloqueada antes de cualquier efecto; `verificar` devuelve
fallo sin ejecutar la cadena histórica. Véase [Problemas conocidos](KNOWN_ISSUES.md).

## Descargas

El gestor opcional lee YAML, lanza aria2c para las entradas de su manifiesto y registra
progreso local. Puede comprobar un hash esperado cuando se proporciona. El manifiesto
público está vacío y no se han validado descargas reales en esta edición.

Se excluyeron scripts de backup, intervención de servicios y benchmarks del equipo
privado. No hay instalador de llama.cpp, llama-swap ni modelos, ni los antiguos
lanzadores de consola.
