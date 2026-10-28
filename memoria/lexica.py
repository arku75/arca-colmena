"""Índice SQLite FTS5 incremental con citas por archivo y línea.

Compara tamaño/mtime y hashes de contenido antes de reindexar; los resultados
pueden contrastarse con el archivo actual para detectar índices obsoletos.
La edición pública restringe la indexación a la raíz elegida y omite archivos
de entorno y directorios privados habituales. Revise los documentos que indexe: no
existe una detección universal de datos sensibles.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

EXTENSIONES = {
    ".md", ".txt", ".py", ".sh", ".bash", ".zsh", ".toml", ".yaml", ".yml", ".json",
    ".jsonl", ".ini", ".cfg", ".conf", ".sql", ".c", ".h", ".cpp", ".rs", ".go",
    ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".csv", ".tsv", ".log", ".rst",
}
IGNORAR_DIR = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache",
               ".pytest_cache", ".locks", "estado", "logs", ".cache", "dist", "build",
               ".ssh", ".aws", ".gnupg", ".config"}
#: `memoria/propuestas/` NO se indexa: es el registro de AUDITORÍA (quién propuso qué),
#: no memoria. Si se indexa, cada propuesta —incluidas las envenenadas y las rechazadas—
#: compite en las búsquedas con la memoria aceptada y la empuja fuera del top-k. Medido:
#: con el REGISTRO dentro, buscar el veneno devolvía 4 líneas del registro y 0 notas.
#: Para investigar un envenenamiento se grepea a mano: notas.buscar_texto(q, rutas=[REGISTRO]).
NO_INDEXAR = ("memoria/propuestas/",)
MAX_BYTES = 4_000_000
MAX_LINEA = 4000


def fts5_disponible() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.close()
        return True
    except Exception:
        return False


class Lexica:
    def __init__(self, ruta_db: str | Path | None = None, raiz: str | Path | None = None) -> None:
        if raiz is None:
            raiz = os.environ.get("COLMENA_RAIZ") or Path(__file__).resolve().parents[1]
        self.raiz = Path(raiz).resolve()
        self.ruta_db = Path(ruta_db) if ruta_db else self.raiz / "memoria" / "indice.db"
        self.ruta_db.parent.mkdir(parents=True, exist_ok=True)
        if not fts5_disponible():
            raise RuntimeError("este SQLite no trae FTS5: sqlite3 :memory: \"CREATE VIRTUAL TABLE t USING fts5(x)\"")
        self.db = sqlite3.connect(str(self.ruta_db))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")   # cortes de luz reales (docs/contratos.md)
        self._esquema()

    def _esquema(self) -> None:
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS fichero(
            ruta TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, tam INTEGER NOT NULL,
            sha256 TEXT NOT NULL, lineas INTEGER NOT NULL, indexado TEXT NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS lineas USING fts5(
            ruta UNINDEXED, nlinea UNINDEXED, texto,
            tokenize = "unicode61 remove_diacritics 2");
        """)
        self.db.commit()

    # ── indexar ──────────────────────────────────────────────────────────────
    def _candidatos(self, rutas: list[str | Path]) -> list[Path]:
        out: list[Path] = []
        for r in rutas:
            p = Path(r)
            if not p.is_absolute():
                p = self.raiz / p
            if p.is_file():
                out.append(p.resolve())
                continue
            if not p.is_dir():
                continue
            for base, dirs, ficheros in os.walk(p):
                dirs[:] = [d for d in dirs if d not in IGNORAR_DIR and not d.startswith(".tmp-")]
                for f in ficheros:
                    fp = Path(base) / f
                    if fp.suffix.lower() in EXTENSIONES or f in {"MEMORIA.md", "TASK.md"}:
                        out.append(fp.resolve())
        raiz = str(self.raiz) + "/"
        out = [f for f in out
               if not any(str(f).startswith(raiz + pre) for pre in NO_INDEXAR)]
        # La raíz es un límite de lectura; también se resuelven los symlinks.
        out = [f for f in out if f.is_relative_to(self.raiz)
               and f.name != ".env" and not f.name.startswith(".env.")
               and not any(part in IGNORAR_DIR for part in f.relative_to(self.raiz).parts)]
        return sorted(set(out))

    @staticmethod
    def _sha(p: Path) -> str:
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for bloque in iter(lambda: fh.read(1 << 20), b""):
                h.update(bloque)
        return h.hexdigest()

    def indexar(self, rutas: list[str | Path] | str | Path, *, borrar_ausentes: bool = True) -> dict:
        """Devuelve los NÚMEROS: vistos / reindexados / saltados_por_stat / solo_mtime /
        borrados / lineas / ms. Sin números no se puede afirmar que es incremental."""
        if isinstance(rutas, (str, Path)):
            rutas = [rutas]
        t0 = time.perf_counter()
        vistos = reindexados = saltados = solo_mtime = borrados = ignorados = 0
        lineas_nuevas = 0
        conocidos = {r: (m, t, s) for r, m, t, s in
                     self.db.execute("SELECT ruta, mtime_ns, tam, sha256 FROM fichero")}
        candidatos = self._candidatos(rutas)
        vistos_set: set[str] = set()
        for p in candidatos:
            rel = os.path.relpath(p, self.raiz)
            vistos += 1
            vistos_set.add(rel)
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > MAX_BYTES:
                ignorados += 1
                continue
            previo = conocidos.get(rel)
            if previo and previo[0] == st.st_mtime_ns and previo[1] == st.st_size:
                saltados += 1                      # ni se abre el fichero
                continue
            try:
                sha = self._sha(p)
            except OSError:
                continue
            if previo and previo[2] == sha:        # `touch`: cambió el stat, no el contenido
                self.db.execute("UPDATE fichero SET mtime_ns=?, tam=? WHERE ruta=?",
                                (st.st_mtime_ns, st.st_size, rel))
                solo_mtime += 1
                continue
            try:
                texto = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            filas = [(rel, i, l[:MAX_LINEA]) for i, l in enumerate(texto.splitlines(), 1)
                     if l.strip()]
            self.db.execute("DELETE FROM lineas WHERE ruta=?", (rel,))
            self.db.executemany("INSERT INTO lineas(ruta, nlinea, texto) VALUES(?,?,?)", filas)
            self.db.execute(
                "INSERT OR REPLACE INTO fichero(ruta, mtime_ns, tam, sha256, lineas, indexado) "
                "VALUES(?,?,?,?,?,?)",
                (rel, st.st_mtime_ns, st.st_size, sha, len(filas),
                 datetime.now().isoformat(timespec="seconds")))
            reindexados += 1
            lineas_nuevas += len(filas)
        if borrar_ausentes:
            prefijos = [os.path.relpath(Path(r) if Path(r).is_absolute() else self.raiz / r, self.raiz)
                        for r in rutas]
            for rel in list(conocidos):
                if rel in vistos_set:
                    continue
                if any(rel == pre or rel.startswith(pre.rstrip("/") + "/") for pre in prefijos):
                    if not (self.raiz / rel).exists():
                        self.db.execute("DELETE FROM lineas WHERE ruta=?", (rel,))
                        self.db.execute("DELETE FROM fichero WHERE ruta=?", (rel,))
                        borrados += 1
        self.db.commit()
        return {"vistos": vistos, "reindexados": reindexados, "saltados_por_stat": saltados,
                "solo_mtime": solo_mtime, "borrados": borrados, "ignorados_por_tamaño": ignorados,
                "lineas_nuevas": lineas_nuevas, "ms": round((time.perf_counter() - t0) * 1000, 1)}

    # ── buscar ───────────────────────────────────────────────────────────────
    @staticmethod
    def _terminos(consulta: str) -> list[str]:
        limpio = "".join(ch if (ch.isalnum() or ch in "_-.áéíóúüñÁÉÍÓÚÜÑ") else " " for ch in consulta)
        return [t for t in limpio.split() if len(t) > 1]

    def buscar(self, consulta: str, limite: int = 20, *, verificar: bool = True) -> list[dict]:
        """Tres intentos, del más exacto al más laxo, y se dice CUÁL acertó:
        frase exacta → todos los términos (con prefijo) → cualquiera. La ambigüedad
        se etiqueta, no se esconde. Con `verificar` (por defecto), cada hit se contrasta
        contra el disco y se marca `rancio` si el índice ya no coincide."""
        term = self._terminos(consulta)
        if not term:
            return []
        intentos = [("frase", '"' + " ".join(term) + '"')]
        if len(term) > 1:
            intentos.append(("and", " AND ".join(f'"{t}"*' for t in term)))
            intentos.append(("or", " OR ".join(f'"{t}"*' for t in term)))
        else:
            intentos.append(("prefijo", f'"{term[0]}"*'))
        for modo, expr in intentos:
            try:
                filas = self.db.execute(
                    "SELECT ruta, nlinea, texto, snippet(lineas, 2, '»', '«', '…', 24), rank "
                    "FROM lineas WHERE lineas MATCH ? ORDER BY rank LIMIT ?",
                    (expr, limite)).fetchall()
            except sqlite3.OperationalError:
                continue
            if filas:
                salida = [{"texto": t.strip(), "snippet": sn, "fuente": "fts5", "modo": modo,
                           "puntuacion": -float(rk), "cita": f"{r}:{n}",
                           "procedencia": {"ruta": r, "linea": n, "indice": str(self.ruta_db)}}
                          for r, n, t, sn, rk in filas]
                return self._verificar_en_disco(salida) if verificar else salida
        return []

    def _verificar_en_disco(self, hits: list[dict]) -> list[dict]:
        """Contrasta citas con el archivo actual y marca resultados obsoletos."""
        cache: dict[str, list[str]] = {}
        for h in hits:
            r, n = h["procedencia"]["ruta"], h["procedencia"]["linea"]
            try:
                if r not in cache:
                    cache[r] = (self.raiz / r).read_text(encoding="utf-8", errors="replace").splitlines()
                real = cache[r][n - 1] if 0 < n <= len(cache[r]) else None
            except OSError:
                real = None
            if real is None:
                h["verificado"], h["rancio"] = False, True
                h["procedencia"]["aviso"] = "el fichero o la línea ya no existen: reindexa"
            elif real.strip()[:MAX_LINEA] != h["texto"]:
                h["verificado"], h["rancio"] = False, True
                h["texto_disco"] = real.strip()
                h["procedencia"]["aviso"] = "el índice está rancio: el disco dice otra cosa"
            else:
                h["verificado"], h["rancio"] = True, False
        return hits

    def estado(self) -> dict:
        f = self.db.execute("SELECT COUNT(*), COALESCE(SUM(lineas),0), MAX(indexado) FROM fichero").fetchone()
        n = self.db.execute("SELECT COUNT(*) FROM lineas").fetchone()[0]
        # el tamaño real incluye el WAL: dar solo el .db es un número que engaña
        tam = sum(Path(str(self.ruta_db) + suf).stat().st_size
                  for suf in ("", "-wal", "-shm") if Path(str(self.ruta_db) + suf).exists())
        return {"fts5": True, "ficheros": f[0], "lineas_registradas": f[1], "lineas_indexadas": n,
                "ultima_indexacion": f[2], "db": str(self.ruta_db), "db_bytes": tam,
                "sqlite": sqlite3.sqlite_version}

    def cerrar(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass
