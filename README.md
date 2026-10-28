# ARCA / COLMENA

**Archivo histórico experimental · snapshot de origen: 9 de septiembre de 2026.**

COLMENA explora cómo coordinar tareas con modelos locales: una cola durable,
memoria con procedencia, un cliente HTTP para inferencia y roles de planificación,
trabajo y revisión. Esta edición publica componentes inspeccionados y una demo
local reproducible. Es independiente del proyecto EIDOS actual y no lo reemplaza.

**El ciclo autónomo está desactivado.** La auditoría del snapshot encontró que la
verificación no estaba vinculada de forma fiable al texto generado y que algunas
rutas podían ejecutar archivos o comandos fuera de las comprobaciones previstas.
`Colmena()` se detiene antes de abrir estado o contactar servicios; `verificar()`
devuelve un resultado bloqueado. Véase [Problemas conocidos](docs/KNOWN_ISSUES.md).
Quitar el archivo `PARAR` no activa el bucle.

## Qué contiene

| Componente | Función y estado de esta edición |
| --- | --- |
| `nucleo/cola.py` | Cola SQLite con dependencias, prioridades, afinidad de modelo, eventos, huellas y recuperación de tareas interrumpidas. Se comprueba con datos temporales. |
| `nucleo/motor.py` | Cliente HTTP/SSE para servicios locales como llama-swap: chat, reintentos, diagnóstico de respuestas truncadas y solicitudes de salida estructurada. Pruebas con servidor simulado. |
| `memoria/` | Notas Markdown, propuestas, curación, índice SQLite FTS5 y búsqueda con archivo/línea de procedencia. Embeddings opcionales, apagados por defecto. |
| `agentes/` | Seis plantillas de roles y cuatro esquemas JSON para estudiar el diseño. No ejecutan acciones por sí mismas. |
| `nucleo/barreras.py` | Políticas experimentales de comandos y presupuestos. Allowlist vacía; no constituye un sandbox. |
| `colmena/descargador.py` | Gestor experimental con aria2c y verificaciones de archivos. Manifiesto vacío; descargas reales no validadas en esta publicación. |
| `colmena/bucle.py`, `nucleo/verificar.py` | Referencia del diseño histórico, con las entradas de ejecución automática bloqueadas. |

No se incluyen pesos de modelos, ejecutables de terceros, memoria personal,
credenciales, logs del equipo, configuración de servicios ni historial Git privado.
La selección tampoco incluye los antiguos lanzadores `bin/colmena*`; las
instrucciones siguientes usan directamente Python.

## Probar la demo sin modelos

Requisitos: **Python 3.11 o posterior en Linux**, SQLite con FTS5 y una copia del
repositorio. Las notas usan `fcntl`; Windows nativo no está validado. La demo y
pruebas básicas no necesitan paquetes de pip, GPU ni Internet. Git es opcional
para el historial de notas; la demo no crea commits.

```bash
git clone https://github.com/arku75/arca-colmena.git
cd arca-colmena
python3 examples/offline_demo.py
```

La demo crea un directorio temporal, encola dos tareas ficticias con una dependencia,
marca su resultado y busca una nota sintética sobre un inventario de libros. Imprime
un resumen JSON y elimina el directorio al terminar. No carga modelos, descarga
archivos ni modifica otros proyectos.

## Pruebas públicas

```bash
python3 -m unittest discover -s pruebas -p 'test_memoria.py' -v
python3 -m unittest discover -s pruebas -p 'test_barreras.py' -v
python3 pruebas/test_cola.py
python3 pruebas/test_motor.py
```

El motor inicia un servidor de prueba en `127.0.0.1` y un puerto efímero; necesita
permiso para sockets locales. No utiliza un servidor de modelos preexistente.
Las pruebas de cola interrumpen procesos hijos creados por la propia prueba para
comprobar recuperación y atomicidad. También puede usar `bash pruebas/humo.sh`.

[Validación](docs/VALIDATION.md) registra los resultados y el alcance comprobado.
Las cifras del diario original no se presentan como resultados reproducidos.

## Usar un componente por separado

```python
from nucleo.cola import Cola

with Cola("/tmp/colmena-ejemplo.sqlite") as cola:
    objetivo = cola.nuevo_objetivo("Preparar un catálogo de ejemplo")
    tarea = cola.añadir_tarea(objetivo, "Revisar el índice", modelo="demo")
    trabajo = cola.siguiente("demo", worker="ejemplo")
    if trabajo:
        cola.marcar(trabajo["id"], "hecha", {"resumen": "Ejemplo local"})
```

Para notas e índices, pase una raíz propia y revise qué documentos indexa. Los
índices y notas son archivos locales sin cifrado. El filtro de orígenes es una
política de aplicación: quien controla el proceso puede elegir una etiqueta o
editar los archivos. No es autenticación.

El cliente de modelos necesita un servicio compatible instalado y configurado por
usted. `config/colmena.example.toml` explica las opciones: los identificadores están
vacíos y el registro de solicitudes está apagado por defecto. Las llamadas explícitas
a `Motor` transmiten al servicio elegido los mensajes que les entregue. Configurar
un modelo no habilita el orquestador histórico.

La descarga de modelos es una herramienta opcional separada. Requiere PyYAML,
requests y aria2c; puede leer un manifiesto propio mediante `--config`.
`config/modelos.yaml` no tiene entradas. **`--dry-run` puede consultar tamaños
remotos** y no garantiza una ejecución offline. No se distribuyen modelos ni se
conceden derechos sobre ellos.

## Diseño, procedencia y límites

- [Arquitectura](docs/arquitectura.md): componentes y estado real.
- [Contratos](docs/contratos.md): API y efectos.
- [Problemas conocidos](docs/KNOWN_ISSUES.md): bloqueos y trabajo pendiente.
- [Procedencia](docs/PROVENANCE.md): snapshot, selección y cambios.
- [Terceros](THIRD_PARTY_NOTICES.md): proyectos externos mencionados.

Esta publicación permite estudiar trabajo histórico con ejemplos aislados. No
acredita una solución autónoma terminada, una auditoría de seguridad completa ni
compatibilidad con cualquier servidor de inferencia.

## Licencia

El snapshot revisado no contenía una licencia explícita ni atribución completa de
autoría. Esta edición no atribuye a una persona trabajo de terceros ni añade una
licencia no documentada. Consulte [Procedencia](docs/PROVENANCE.md) antes de
redistribuir o incorporar el código a otro proyecto.

### English summary

Historical experimental source from the 2026-09-09 ARCA/COLMENA snapshot. Includes
a durable SQLite task queue, a local-model HTTP/SSE client, Markdown/FTS5 memory
and role templates. Public demos and tests use synthetic temporary data.
Autonomous orchestration and automatic verification are disabled. This archive
is separate from current EIDOS and is not a production agent platform.
