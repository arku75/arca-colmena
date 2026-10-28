# Procedencia y edición pública

## Fuente

La selección procede de `ARCA-COLMENA-20260909-0131.zip`, aportado por la persona titular
de la cuenta que publica este repositorio. La fecha del nombre es **2026-09-09**.
No se publica el historial Git privado. Este archivo no reemplaza EIDOS actual.

Los textos reflejan desarrollo experimental con asistencia de agentes. Los archivos
revisados no establecen una atribución exhaustiva de autoría ni una licencia explícita.
Procedencia del material y derechos sobre él no son afirmaciones equivalentes.

## Selección

Se conservan módulos Python de cola, cliente, barreras, verificación, orquestación,
descargas y memoria; plantillas/esquemas; y pruebas que pueden ejecutarse aisladamente.

Quedan fuera:

- Historial privado, memoria personal, bases de estado, logs, backups, binarios,
  pesos de modelos, entornos virtuales y configuración operativa.
- Bitácora, ideas, tareas privadas y auditoría original; los hallazgos relevantes
  se resumen en `KNOWN_ISSUES.md`.
- Scripts de backup y transformación de modelos vinculados al equipo privado,
  benchmarks que consultaban/interrumpían servicios reales y pruebas con modelos reales.

Los lanzadores `bin/colmena*` y la configuración original no formaban parte de la
selección revisada. La publicación es una selección saneada, no una copia bit a bit.

## Cambios de publicación

1. Documentación nueva; retirada de rutas personales, detalles de la máquina,
   cifras de rendimiento y afirmaciones de investigación no reproducidas.
2. Orquestador y verificador bloqueados, conservando su código como referencia.
3. Identificadores de modelos vacíos, allowlist y manifiesto vacíos; embeddings y
   registro de solicitudes apagados por defecto.
4. Indexación léxica limitada a la raíz, resolviendo symlinks, y exclusión de archivos
   de entorno/directorios privados habituales.
5. Demo temporal, pruebas aisladas de memoria y barreras y selección del cliente
   simulado. Cola y cliente conservan sus pruebas históricas pertinentes.
6. Búsqueda de notas mediante ejecutables ordinarios `rg`/`grep`, eliminando la
   detección de una instalación privada de Claude. Separación de la consulta con
   `--` para tratarla como texto, y cierre explícito del archivo al verificar citas.

Las fuentes originales del equipo permanecen independientes de esta edición.

## Licencia

No se inventa autoría ni se añade una licencia sin constancia en el material o
autorización de sus titulares. La visibilidad pública no debe interpretarse por sí
sola como permiso para redistribuir o crear derivados. Consulte al titular del
repositorio para aclarar los derechos del código propio.

Los programas y modelos externos conservan sus derechos. No se distribuyen sus
fuentes ni binarios. Véase `THIRD_PARTY_NOTICES.md`.
