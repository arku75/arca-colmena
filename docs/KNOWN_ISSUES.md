# Estado y problemas conocidos

La documentación privada ya registraba estos bloqueos, contrastados con el código.
Publicar el archivo histórico no equivale a resolverlos.

| Hallazgo | Consecuencia | Tratamiento público |
| --- | --- | --- |
| Respuesta del modelo en `salida`, verificación principalmente sobre rutas de archivos. | Podía confundirse el estado de archivos existentes con la calidad de una respuesta nueva. | Constructor de `Colmena` bloqueado. |
| Extracción de rutas desde prosa sin frontera de workspace suficiente. | Una ruta podía apuntar fuera del proyecto. | No se activa el ciclo que consume esas rutas. |
| Compilación podía importar Python por defecto. | Verificar un archivo podía ejecutar su contenido. | `verificar` falla antes de cualquier señal. |
| Señal de humo aceptaba una cadena shell. | Algunas ejecuciones eludían la allowlist prevista. | Verificador desactivado; sin garantías de aislamiento. |
| Cobertura incompleta de variantes de argumentos. | Permitir un binario por hash no controla todos sus efectos. | Allowlist vacía; uso experimental. |
| Decisión final sin enlace completo entre artefacto, pruebas y evidencia. | Riesgo de declarar terminado un trabajo sin prueba suficiente. | Juicio automático desactivado. |
| Scripts dependientes del equipo y sus servicios. | No constituían instalación portable ni prueba aislada. | Excluidos; ejemplos públicos sintéticos. |

## Alcance de la desactivación

La protección está en las entradas públicas, además del marcador `PARAR`. Borrar
ese archivo no rehabilita el orquestador. Los helpers privados pueden ejecutar
programas o acceder a rutas si alguien los invoca deliberadamente: no son un sandbox.

## Trabajo necesario para evolucionar

Habría que definir artefactos estructurados y materializados en un workspace aislado,
ligar cada resultado a hashes y pruebas concretas y centralizar acciones autorizadas
en una frontera que no dependa de texto de modelos. Ejecutar código requeriría
aislamiento real y límites de recursos. Faltan pruebas de integración con un backend
concreto y aclarar los derechos de distribución.

Esta edición no implementa esa evolución ni anuncia su disponibilidad.
