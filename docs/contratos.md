# Contratos de los componentes públicos

## Cola

```python
Cola(ruta_db)
cola.nuevo_objetivo(texto, prioridad=5, meta=None)
cola.añadir_tarea(objetivo_id, descripcion, modelo="demo", depende_de=())
cola.siguiente(modelo_cargado, worker="demo")
cola.marcar(tarea_id, "hecha", resultado={})
cola.reanudar()
cola.eventos(tarea_id=None, limite=50)
cola.integridad()
cola.cerrar()
```

El constructor crea la base si falta. La clase admite `with`. No ejecuta el texto
de tareas ni llama a modelos. `siguiente` puede devolver `None`. Quien invoca
`marcar` es responsable de que el estado describa lo sucedido.

## Notas y búsqueda

```python
from memoria import Memoria
memoria = Memoria(raiz=directorio_dedicado, modelo_embed="")
memoria.apuntar("Nota sintética", origen="humano", commitear=False)
memoria.indexar([directorio_dedicado / "docs"])
resultados = memoria.recordar("consulta")
```

Los objetos crean directorios y bases SQLite bajo la raíz indicada. Si esa raíz tiene
repositorio Git, determinadas escrituras pueden crear commits locales. `commitear=False`
evita ese paso en `apuntar`; propuestas aceptadas y curación tienen su comportamiento
descrito en `notas.py`.

El origen histórico `ser` se conserva como alias de compatibilidad, junto a `humano`,
`regla`, `sistema` y `prueba`. Son cadenas elegidas por el llamador, no pruebas de identidad.
Un origen de modelo debe usar `proponer`; una persona puede revisar y aceptar. El filtro
automático no garantiza la veracidad de lo propuesto.

Los resultados incorporan cita y procedencia. Un índice puede quedar obsoleto si cambia
el archivo; las consultas léxicas marcan esa condición cuando la detectan. Revise los
datos antes de copiarlos a un servicio externo.

## Cliente del motor

```python
from nucleo.motor import Motor
motor = Motor({
    "motor": {"url": "http://127.0.0.1:9292"},
    "registro": {"ruta": None},
})
# Esta llamada sí enviaría texto al servidor elegido:
# motor.chat("ID_CONFIGURADO_POR_USTED", [{"role": "user", "content": "Hola"}])
```

Construir el objeto no inicia un servidor. Los métodos de inferencia, disponibilidad
y descarga de modelos cargados sí realizan solicitudes según su función. Las excepciones
distinguen peticiones rechazadas, resultados truncados y agotamiento de reintentos.

## Entradas históricas bloqueadas

`Colmena(...)` lanza `RuntimeError` antes de abrir configuración, memoria o cola.
`verificar(artefacto)` devuelve `ok=False`, `bloqueante="snapshot_historico"` y
`confianza="nula"`, sin señales. No ejecuta comandos ni imports. El código posterior
se conserva como referencia. Sus helpers privados no deben invocarse sobre entradas
no confiables.

## Barreras

`puede_ejecutar(lista_de_argumentos)` evalúa una política; no ejecuta el comando.
La allowlist pública no tiene entradas. Aprobar binarios cambia la política local y
requiere revisar los posibles efectos de sus argumentos. No sustituye aislamiento
del sistema operativo.

Los guardianes de batería/recursos pueden consultar sensores y herramientas del
sistema. Las pruebas públicas simulan sus entradas. Los límites heredados son ejemplos
configurables, no valores universales.
