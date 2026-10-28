#!/usr/bin/env python3
"""Demostración temporal, sintética y sin llamadas de red ni modelos."""
from pathlib import Path
import json
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nucleo.cola import Cola
from memoria import Memoria


def demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="colmena-demo-") as directory:
        root = Path(directory)
        with Cola(str(root / "cola.sqlite")) as queue:
            objective = queue.nuevo_objetivo("Preparar un catálogo ficticio de libros")
            first = queue.añadir_tarea(objective, "Contar tres títulos de ejemplo", modelo="demo")
            second = queue.añadir_tarea(objective, "Escribir resumen del catálogo", modelo="demo",
                                       depende_de=[first])
            claimed = queue.siguiente("demo", worker="demo-offline")
            assert claimed["id"] == first
            queue.marcar(first, "hecha", {"titulos": 3, "datos": "sintéticos"})
            claimed = queue.siguiente("demo", worker="demo-offline")
            assert claimed["id"] == second
            queue.marcar(second, "hecha", {"resumen": "El catálogo ficticio contiene tres títulos."})
            queue_result = {"tareas": 2, "estado": "hechas", "integridad": queue.integridad(),
                            "eventos": len(queue.eventos(limite=100))}

        # A network call is an error in this demo, even if a local model is running.
        with patch("urllib.request.urlopen", side_effect=AssertionError("La demo no usa red")):
            memory = Memoria(root, modelo_embed="")
            try:
                note = memory.apuntar("El catálogo ficticio contiene tres títulos de ejemplo.",
                                      tipo="hecho", origen="humano", commitear=False)
                indexed = memory.indexar([root / "memoria" / "diario"])
                matches = memory.recordar("catálogo ficticio")
                assert matches and all(m.get("procedencia") for m in matches)
                memory_result = {"archivos_indexados": indexed["reindexados"],
                                 "resultados": len(matches),
                                 "cita": note["cita"], "embeddings": "desactivados"}
            finally:
                memory.lexica.cerrar()
                memory.semantica.cerrar()
        return {"demo": "datos sintéticos; directorio temporal eliminado",
                "cola": queue_result, "memoria": memory_result}


if __name__ == "__main__":
    print(json.dumps(demo(), ensure_ascii=False, indent=2))
