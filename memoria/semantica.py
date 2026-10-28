"""Índice vectorial SQLite con embeddings opcionales de un servicio local.

El modelo de embeddings está vacío por defecto: disponible() no hace red en
ese estado. Configurarlo explícitamente habilita solicitudes HTTP de texto al
servicio elegido. NumPy es opcional; existe una ruta de cálculo con Python.
No se distribuyen modelos ni vectores del entorno original.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import numpy as np
    HAY_NUMPY = True
except Exception:                                   # pragma: no cover
    np = None
    HAY_NUMPY = False

URL_POR_DEFECTO = "http://127.0.0.1:9292"
MODELO_POR_DEFECTO = ""


def _leer_config(raiz: Path) -> tuple[str, str]:
    """config/colmena.toml manda; si no está todavía, valores del contrato."""
    cfg = raiz / "config" / "colmena.toml"
    url, modelo = URL_POR_DEFECTO, MODELO_POR_DEFECTO
    if cfg.exists():
        try:
            import tomllib
            d = tomllib.loads(cfg.read_text(encoding="utf-8"))
            url = d.get("motor", {}).get("url", url)
            modelo = d.get("modelos", {}).get("embed", modelo)
        except Exception:
            pass
    return url, modelo


class Semantica:
    def __init__(self, url: str | None = None, modelo: str | None = None,
                 ruta_db: str | Path | None = None, raiz: str | Path | None = None,
                 timeout_sonda: float = 2.0, timeout_embed: float = 600.0) -> None:
        if raiz is None:
            raiz = os.environ.get("COLMENA_RAIZ") or Path(__file__).resolve().parents[1]
        self.raiz = Path(raiz).resolve()
        cfg_url, cfg_modelo = _leer_config(self.raiz)
        self.url = (url or os.environ.get("COLMENA_URL_MOTOR") or cfg_url).rstrip("/")
        self.modelo = cfg_modelo if modelo is None else modelo
        self.timeout_sonda = timeout_sonda
        self.timeout_embed = timeout_embed
        self.ruta_db = Path(ruta_db) if ruta_db else self.raiz / "memoria" / "vectores.db"
        self.ruta_db.parent.mkdir(parents=True, exist_ok=True)
        self.ultimo_motivo = "sin sondear"
        self._cache_sonda: tuple[float, bool] = (0.0, False)
        self._matriz = None
        self._firma_matriz: tuple | None = None
        self.db = sqlite3.connect(str(self.ruta_db))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS vector(
            id INTEGER PRIMARY KEY, clave TEXT UNIQUE, texto TEXT NOT NULL,
            meta TEXT NOT NULL DEFAULT '{}', modelo TEXT NOT NULL, dim INTEGER NOT NULL,
            emb BLOB NOT NULL, creado TEXT NOT NULL);
        """)
        self.db.commit()

    # ── disponibilidad (sin tocar la GPU) ────────────────────────────────────
    def disponible(self, *, forzar: bool = False, cache_s: float = 5.0) -> bool:
        ahora = time.time()
        if not forzar and ahora - self._cache_sonda[0] < cache_s:
            return self._cache_sonda[1]
        if not self.modelo:
            # config/colmena.toml pone `embed = ""` mientras el GGUF se descarga.
            # Es una desactivación DELIBERADA: ni se sondea el endpoint.
            self.ultimo_motivo = ("config/colmena.toml: modelos.embed está vacío "
                                  "= memoria semántica desactivada")
            self._cache_sonda = (ahora, False)
            return False
        ok = False
        try:
            req = urllib.request.Request(self.url + "/v1/models", method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout_sonda) as r:
                datos = json.loads(r.read().decode("utf-8", "replace"))
            ids = [m.get("id") for m in datos.get("data", [])]
            if self.modelo in ids:
                ok, self.ultimo_motivo = True, f"{self.url} ofrece '{self.modelo}'"
            else:
                self.ultimo_motivo = (f"{self.url} responde pero no ofrece '{self.modelo}' "
                                      f"(hay: {', '.join(str(i) for i in ids[:8]) or 'ninguno'})")
        except urllib.error.URLError as e:
            self.ultimo_motivo = f"{self.url} no responde: {getattr(e, 'reason', e)}"
        except Exception as e:
            self.ultimo_motivo = f"{self.url} sonda fallida: {type(e).__name__}: {e}"
        self._cache_sonda = (ahora, ok)
        return ok

    # ── embeddings ───────────────────────────────────────────────────────────
    def _embed(self, textos: list[str]) -> list[list[float]]:
        cuerpo = json.dumps({"model": self.modelo, "input": textos}).encode()
        req = urllib.request.Request(self.url + "/v1/embeddings", data=cuerpo,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_embed) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        filas = sorted(d["data"], key=lambda x: x.get("index", 0))
        return [f["embedding"] for f in filas]

    @staticmethod
    def _normalizar(v: list[float]) -> list[float]:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    @staticmethod
    def _empaquetar(v: list[float]) -> bytes:
        return struct.pack(f"<{len(v)}f", *v)

    # ── indexar ──────────────────────────────────────────────────────────────
    def indexar(self, textos: list[str], metadatos: list[dict] | None = None, *,
                lote: int = 16) -> dict:
        """Nunca rompe el sistema: si el motor no está, lo dice y devuelve ok=False."""
        if not textos:
            return {"ok": True, "indexados": 0, "saltados": 0, "motivo": "nada que indexar"}
        if not self.disponible(forzar=True):
            return {"ok": False, "indexados": 0, "saltados": len(textos),
                    "motivo": self.ultimo_motivo, "degradado": True}
        metadatos = metadatos or [{} for _ in textos]
        if len(metadatos) != len(textos):
            raise ValueError("metadatos y textos deben tener la misma longitud")
        import hashlib
        pendientes: list[tuple[str, str, dict]] = []
        saltados = 0
        for t, m in zip(textos, metadatos):
            clave = hashlib.sha256(
                (self.modelo + "\x00" + t + "\x00" + json.dumps(m, sort_keys=True, ensure_ascii=False)
                 ).encode()).hexdigest()
            if self.db.execute("SELECT 1 FROM vector WHERE clave=?", (clave,)).fetchone():
                saltados += 1
                continue
            pendientes.append((clave, t, m))
        t0 = time.perf_counter()
        n = 0
        for i in range(0, len(pendientes), lote):
            trozo = pendientes[i:i + lote]
            try:
                vecs = self._embed([t for _, t, _ in trozo])
            except Exception as e:
                self.ultimo_motivo = f"fallo al embeber: {type(e).__name__}: {e}"
                self.db.commit()
                return {"ok": False, "indexados": n, "saltados": saltados,
                        "motivo": self.ultimo_motivo, "degradado": True}
            ahora = datetime.now().isoformat(timespec="seconds")
            for (clave, texto, meta), v in zip(trozo, vecs):
                v = self._normalizar(v)
                self.db.execute(
                    "INSERT OR IGNORE INTO vector(clave, texto, meta, modelo, dim, emb, creado) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (clave, texto, json.dumps(meta, ensure_ascii=False), self.modelo,
                     len(v), self._empaquetar(v), ahora))
                n += 1
        self.db.commit()
        self._matriz = None
        return {"ok": True, "indexados": n, "saltados": saltados, "modelo": self.modelo,
                "ms": round((time.perf_counter() - t0) * 1000, 1)}

    # ── buscar ───────────────────────────────────────────────────────────────
    def _cargar_matriz(self):
        firma = self.db.execute(
            "SELECT COUNT(*), COALESCE(MAX(id),0) FROM vector WHERE modelo=?", (self.modelo,)).fetchone()
        if self._matriz is not None and self._firma_matriz == firma:
            return self._matriz
        filas = self.db.execute(
            "SELECT id, texto, meta, dim, emb FROM vector WHERE modelo=? ORDER BY id", (self.modelo,)
        ).fetchall()
        if not filas:
            self._matriz, self._firma_matriz = ([], None, 0), firma
            return self._matriz
        dim = filas[0][3]
        filas = [f for f in filas if f[3] == dim]     # nunca mezclar espacios de embedding
        meta = [(f[0], f[1], f[2]) for f in filas]
        if HAY_NUMPY:
            M = np.frombuffer(b"".join(f[4] for f in filas), dtype=np.float32).reshape(len(filas), dim)
        else:
            M = [struct.unpack(f"<{dim}f", f[4]) for f in filas]
        self._matriz, self._firma_matriz = (meta, M, dim), firma
        return self._matriz

    def buscar(self, consulta: str, k: int = 8) -> list[dict]:
        """[] limpio si el motor no está. Cada resultado lleva su procedencia."""
        if not self.disponible():
            return []
        try:
            q = self._normalizar(self._embed([consulta])[0])
        except Exception as e:
            self.ultimo_motivo = f"fallo al embeber la consulta: {type(e).__name__}: {e}"
            return []
        meta, M, dim = self._cargar_matriz()
        if not meta or dim != len(q):
            if meta and dim != len(q):
                self.ultimo_motivo = f"dimensión distinta: consulta {len(q)} vs almacén {dim}"
            return []
        if HAY_NUMPY:
            qv = np.asarray(q, dtype=np.float32)
            sims = M @ qv
            top = np.argpartition(-sims, min(k, len(sims) - 1))[:k]
            orden = top[np.argsort(-sims[top])]
            pares = [(int(i), float(sims[i])) for i in orden]
        else:
            sims = [sum(a * b for a, b in zip(fila, q)) for fila in M]
            pares = sorted(enumerate(sims), key=lambda x: -x[1])[:k]
        out = []
        for i, s in pares:
            vid, texto, mj = meta[i]
            try:
                m = json.loads(mj)
            except Exception:
                m = {}
            proc = {"vector_id": vid, "almacen": str(self.ruta_db), "modelo": self.modelo}
            proc.update({kk: vv for kk, vv in m.items() if kk in ("ruta", "linea", "fecha", "id_nota", "origen")})
            out.append({"texto": texto, "fuente": "semantica", "puntuacion": round(s, 4),
                        "cita": m.get("cita") or f"vectores.db#{vid}",
                        "meta": m, "procedencia": proc})
        return out

    def estado(self) -> dict:
        n, dims, modelos = self.db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT dim), COUNT(DISTINCT modelo) FROM vector").fetchone()
        return {"disponible": self.disponible(), "motivo": self.ultimo_motivo, "url": self.url,
                "modelo": self.modelo, "vectores": n, "dims_distintas": dims,
                "modelos_distintos": modelos, "numpy": HAY_NUMPY,
                "db": str(self.ruta_db),
                "db_bytes": self.ruta_db.stat().st_size if self.ruta_db.exists() else 0,
                "almacen": "sqlite BLOB + fuerza bruta" + (" (numpy)" if HAY_NUMPY else " (python puro)")}

    def cerrar(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass
