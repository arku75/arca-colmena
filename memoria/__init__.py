"""Fachada de memoria: notas Markdown, búsqueda léxica y embeddings opcionales.

recordar() combina resultados con procedencia y prioriza coincidencias
léxicas. La capa semántica está desactivada por defecto. El constructor escribe
bases de datos en la raíz indicada: use un directorio dedicado. Los orígenes
son etiquetas de aplicación, no identidades autenticadas.
"""
from __future__ import annotations

import os
from pathlib import Path

from .lexica import Lexica, fts5_disponible
from .notas import MemoriaNoAutorizada, Notas
from .semantica import Semantica

__all__ = ["Memoria", "Notas", "Lexica", "Semantica", "MemoriaNoAutorizada", "fts5_disponible"]


class Memoria:
    """La fachada. Une los tres niveles y ofrece una sola puerta de lectura: `recordar()`."""

    def __init__(self, raiz: str | Path | None = None, *, url_motor: str | None = None,
                 modelo_embed: str | None = None) -> None:
        if raiz is None:
            raiz = os.environ.get("COLMENA_RAIZ") or Path(__file__).resolve().parents[1]
        self.raiz = Path(raiz).resolve()
        self.notas = Notas(self.raiz)
        self.lexica = Lexica(raiz=self.raiz)
        self.semantica = Semantica(url=url_motor, modelo=modelo_embed, raiz=self.raiz)

    # ── escritura (delegada, con la misma regla dura) ─────────────────────────
    def apuntar(self, texto: str, tipo: str = "nota", tags=(), **kw) -> dict:
        return self.notas.apuntar(texto, tipo, tags, **kw)

    def proponer(self, texto: str, origen: str, **kw) -> dict:
        return self.notas.proponer(texto, origen, **kw)

    def aceptar(self, pid: str, *, por: str = "ser") -> dict:
        return self.notas.aceptar(pid, por=por)

    def rechazar(self, pid: str, motivo: str, *, por: str = "ser") -> dict:
        return self.notas.rechazar(pid, motivo, por=por)

    def curar(self, **kw) -> dict:
        return self.notas.curar(**kw)

    def indexar(self, rutas=None, **kw) -> dict:
        """Por defecto indexa la propia memoria (notas) + los docs del proyecto."""
        if rutas is None:
            rutas = [self.raiz / "memoria", self.raiz / "docs"]
        return self.lexica.indexar(rutas, **kw)

    # ── LA entrada de lectura ────────────────────────────────────────────────
    def recordar(self, consulta: str, k: int = 8) -> list[dict]:
        """Busca en los 3 niveles y FUSIONA. Orden: primero coincidencias léxicas exactas
        (ripgrep/FTS5), luego semánticas. Cada resultado dice DE DÓNDE sale (fichero+línea, o
        nota+fecha) para que se pueda ir a comprobar. Nunca devuelve texto sin procedencia."""
        salida: list[dict] = []
        vistos: set[str] = set()
        degradado: list[str] = []

        def añadir(item: dict, estrato: int, nivel: str) -> None:
            proc = item.get("procedencia") or {}
            if not (proc.get("ruta") or proc.get("vector_id")):
                return                      # sin procedencia no entra. Sin excepciones.
            clave = item.get("cita") or " ".join(item.get("texto", "").split()).lower()[:160]
            if clave in vistos:
                for y in salida:
                    if y["_clave"] == clave and item["fuente"] not in y["tambien_en"]:
                        y["tambien_en"].append(item["fuente"])
                return
            vistos.add(clave)
            item["_clave"] = clave
            item["estrato"] = estrato
            item["nivel"] = nivel
            item.setdefault("tambien_en", [])
            salida.append(item)

        # 1 · las notas, en vivo (grep sobre el fichero, no sobre un índice que puede estar rancio)
        for r in self.notas.buscar_texto(consulta, limite=max(k * 3, 12)):
            if "error" in r:
                degradado.append(f"notas: {r['error']}")
                continue
            añadir(r, 1, "1/3 nota")

        # 2 · FTS5 sobre los ficheros del proyecto
        try:
            for r in self.lexica.buscar(consulta, limite=max(k * 3, 15)):
                ruta = r["procedencia"]["ruta"]
                nivel = "1/3 nota" if ruta.startswith("memoria/") else "2 proyecto"
                estrato = 1 if r["modo"] in ("frase", "prefijo") else 2
                if r.get("rancio"):        # el índice ya no coincide con el disco:
                    estrato += 1           # baja, y el aviso viaja con el resultado
                    degradado.append(f"indice rancio en {r['cita']}: reindexa")
                if ruta.startswith("memoria/"):
                    n = self.notas.nota_en(ruta, r["procedencia"]["linea"])
                    if n:                  # misma procedencia venga por grep o por índice
                        r["procedencia"].update(
                            {"id_nota": n["id"], "fecha": n["fecha"], "origen": n["origen"],
                             "tipo": n["tipo"], "invalidada": n.get("invalidada")})
                        if n.get("invalidada"):
                            estrato += 2   # retirada: se encuentra, pero nunca por delante
                            degradado.append(f"{n['id']} está INVALIDADA: {n['invalidada'][:60]}")
                añadir(r, estrato, nivel)
        except Exception as e:
            degradado.append(f"lexica: {type(e).__name__}: {e}")

        # 3 · semántica, solo si el motor está vivo (y jamás cargándolo a propósito)
        try:
            if self.semantica.disponible():
                for r in self.semantica.buscar(consulta, k=k):
                    añadir(r, 3, "2b semantica")
            else:
                degradado.append(f"semantica no disponible: {self.semantica.ultimo_motivo}")
        except Exception as e:
            degradado.append(f"semantica: {type(e).__name__}: {e}")

        salida.sort(key=lambda x: (x["estrato"], -float(x.get("puntuacion") or 0)))
        for x in salida:
            x.pop("_clave", None)
            if degradado:
                x["avisos"] = degradado
        return salida[:k]

    # ── rastreo (para cuando una memoria resulta ser falsa) ──────────────────
    def rastrear(self, id_nota: str) -> dict:
        nota = next((n for n in self.notas.leer_todo() if n["id"] == id_nota), None)
        return {"nota": nota, "commits": self.notas.historial(id_nota),
                "cita": (f"{os.path.relpath(nota['ruta'], self.raiz)}:{nota['linea']}"
                         if nota else None)}

    def estado(self) -> dict:
        return {"raiz": str(self.raiz), "notas": self.notas.estado(),
                "lexica": self.lexica.estado(), "semantica": self.semantica.estado()}
