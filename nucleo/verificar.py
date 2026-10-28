#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verificador histórico conservado para estudio, desactivado públicamente.

verificar() devuelve siempre un resultado bloqueado sin importar archivos,
crear procesos ni ejecutar comandos. El resto del código documenta el diseño
original: sus helpers privados NO son una API segura y no deben ejecutarse
sobre entradas no confiables. Ver docs/KNOWN_ISSUES.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

TIMEOUT_POR_DEFECTO_S = 120

# Senales DURAS: si ninguna de estas dio 'ok', la verificacion no prueba gran cosa.
SENALES_DURAS = ("compila", "tests_preexistentes", "ejecucion")

# Secretos. Cada patron con su nombre para poder decir QUE se encontro sin filtrarlo entero.
PATRONES_SECRETO = [
    ("clave de anthropic",      re.compile(r"sk-ant-[A-Za-z0-9_\-]{4,}")),
    ("clave estilo openai/sk-", re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")),
    ("token de github",         re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("clave de aws",            re.compile(r"\bAKIA[0-9A-Z]{12,}")),
    ("clave privada PEM",       re.compile(r"BEGIN\s+(RSA\s+|EC\s+|OPENSSH\s+|DSA\s+)?PRIVATE KEY")),
    ("clave de tailscale",      re.compile(r"\btskey-[A-Za-z0-9\-]{8,}")),
    ("token de slack",          re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}")),
    ("clave de google",         re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
]

# Trabajo a medias que se cuela como "hecho".
PATRONES_MARCADOR = [
    re.compile(r"^\s*#\s*(TODO|FIXME|XXX|HACK)\b", re.M),
    re.compile(r"^\s*//\s*(TODO|FIXME|XXX|HACK)\b", re.M),
    re.compile(r"<<<<<<<\s|^=======$|^>>>>>>>\s", re.M),          # conflicto de merge sin resolver
    re.compile(r"^\s*pass\s*#\s*(implementar|pendiente|todo)", re.M | re.I),
    re.compile(r"raise\s+NotImplementedError"),
    re.compile(r"^\s*\.\.\.\s*#\s*(implementar|pendiente)", re.M | re.I),
]

# Artefactos de compilacion/caches: NO son cambios del agente, son ruido de las herramientas.
_RX_RUIDO = re.compile(
    r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.tox|\.venv|venv|"
    r"node_modules|\.next|dist|build|target|\.eggs|[^/]+\.egg-info)(/|$)"
    r"|\.(pyc|pyo|pyd|class|o|so|a|log)$")


def _es_ruido(rel: str) -> bool:
    return bool(_RX_RUIDO.search(rel.replace("\\", "/")))


# Ficheros que son tests (para separarlos de lo demas).
_EXT_CODIGO_TEST = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs")


def _es_fichero_de_test(rel: str) -> bool:
    """Un fichero que forma parte de la BATERIA de pruebas (para saber quien lo escribio).

    Dentro de tests/ solo cuenta el CODIGO: un .txt de datos no es un test (medido: sin
    esto, pruebas/cache-b3/texto_A.txt se contaba como test del agente).
    conftest.py SI cuenta: quien toca las fixtures puede falsear toda la bateria.
    """
    p = rel.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    partes = p.split("/")
    if base.startswith("test_") and base.endswith(".py"):
        return True
    if base.endswith("_test.py") or base.endswith("_test.go") or base.endswith("_test.rs"):
        return True
    if re.search(r"\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$", base):
        return True
    if base == "conftest.py":
        return True
    if any(d in ("tests", "test", "pruebas", "__tests__") for d in partes[:-1]):
        return base.endswith(_EXT_CODIGO_TEST)
    return False


def _pytest_lo_recoge(rel: str) -> bool:
    """¿pytest lo coleccionaria? Un auxiliar como pruebas/_hijo_cola.py NO se le pasa:
    pytest saldria con codigo 5 ('no tests collected') y pareceria un fallo."""
    base = rel.rsplit("/", 1)[-1]
    return base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py"))


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────────────────────

def _correr(cmd, cwd=None, timeout=TIMEOUT_POR_DEFECTO_S, entrada=None, env=None):
    """Ejecuta SIN shell. Devuelve (codigo, salida_unida). -1 = timeout, -2 = no ejecutable.

    PYTHONDONTWRITEBYTECODE=1 siempre: el verificador NO debe ensuciar el arbol que verifica.
    (Medido: sin esto, py_compile y pytest dejaban __pycache__/*.pyc y luego la senal del diff
    los denunciaba como 'ficheros no declarados'. El verificador se acusaba a si mismo.)
    """
    entorno = dict(env if env is not None else os.environ)
    entorno["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        p = subprocess.run(
            cmd, cwd=cwd, timeout=timeout, input=entrada,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
            env=entorno,
        )
        return p.returncode, p.stdout or ""
    except subprocess.TimeoutExpired as e:
        parcial = e.output or ""
        if isinstance(parcial, bytes):
            parcial = parcial.decode("utf-8", "replace")
        return -1, f"[TIMEOUT tras {timeout}s]\n{parcial}"
    except FileNotFoundError:
        return -2, f"[no existe el ejecutable: {cmd[0]}]"
    except OSError as e:
        return -2, f"[no se pudo ejecutar {cmd[0]}: {e}]"


def _recortar(txt: str, n: int = 4000) -> str:
    txt = txt or ""
    if len(txt) <= n:
        return txt
    mitad = n // 2
    return txt[:mitad] + f"\n[... {len(txt) - n} caracteres recortados ...]\n" + txt[-mitad:]


def _hay(prog: str) -> str | None:
    return shutil.which(prog)


def _lenguaje(ruta: Path) -> str:
    """Detecta el lenguaje por EXTENSION y, si no la hay o no la conoce, por SHEBANG."""
    ext = ruta.suffix.lower()
    por_ext = {
        ".py": "python", ".pyi": "python",
        ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
        ".ts": "ts", ".tsx": "ts", ".mts": "ts", ".cts": "ts",
        ".sh": "bash", ".bash": "bash",
        ".json": "json",
        ".yaml": "yaml", ".yml": "yaml",
        ".toml": "toml",
        ".md": "texto", ".txt": "texto", ".rst": "texto",
    }
    if ext in por_ext:
        return por_ext[ext]
    # shebang
    try:
        with open(ruta, "rb") as f:
            primera = f.readline(200).decode("utf-8", "replace")
    except OSError:
        return "desconocido"
    if primera.startswith("#!"):
        sb = primera.lower()
        if "python" in sb:
            return "python"
        if "node" in sb:
            return "js"
        if "bash" in sb or "/sh" in sb or " sh" in sb:
            return "bash"
    return "desconocido"


def _rutas_reales(artefacto: dict) -> list[Path]:
    cwd = Path(artefacto.get("cwd") or ".").resolve()
    out = []
    for r in artefacto.get("rutas") or []:
        p = Path(r)
        if not p.is_absolute():
            p = cwd / p
        if p.exists() and p.is_file():
            out.append(p)
    return out


def sha256_fichero(ruta: str | Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def _git(cwd, *args, timeout=30):
    return _correr(["git", "-C", str(cwd), *args], timeout=timeout)


def _es_repo_git(cwd) -> bool:
    cod, _ = _git(cwd, "rev-parse", "--git-dir", timeout=10)
    return cod == 0


# ─────────────────────────────────────────────────────────────────────────────
# LA PIEZA CRITICA: distinguir el test que YA existia del que escribio el agente
# ─────────────────────────────────────────────────────────────────────────────

def tests_preexistentes(cwd: str, desde_commit: str | None = "HEAD") -> dict:
    """Separa los tests que YA existian de los que ha escrito el agente en ESTE trabajo.

    Un test creado o modificado en el diff que se esta verificando NO es verificacion:
    se distingue de las pruebas anteriores según la política del diseño histórico.

    Devuelve:
      {'preexistentes': [rutas relativas],   # existian en `desde_commit` y NO se han tocado
       'del_agente'   : [rutas relativas],   # creados o modificados ahora -> NO CUENTAN
       'ok'           : bool,                # False = no se pudo determinar (sin git)
       'motivo'       : str}

    Sin git NO se puede distinguir una cosa de la otra: se devuelve ok=False y quien
    llama debe marcar la senal como OMITIDA (jamas como 'ok'). Fail-safe.
    """
    cwd = str(Path(cwd).resolve())
    desde_commit = desde_commit or "HEAD"
    res = {"preexistentes": [], "del_agente": [], "ok": False, "motivo": ""}

    if not _es_repo_git(cwd):
        res["motivo"] = ("no es un repositorio git: es imposible distinguir un test preexistente "
                         "de uno escrito por el agente -> la senal de tests NO cuenta")
        return res

    cod, _ = _git(cwd, "rev-parse", "--verify", "--quiet", f"{desde_commit}^{{commit}}")
    if cod != 0:
        res["motivo"] = (f"el commit base '{desde_commit}' no existe (repo sin commits?): "
                         f"no se puede saber que test ya existia -> la senal de tests NO cuenta")
        return res

    # 1) lo que se ha tocado respecto al commit base: working tree + index + sin trackear
    tocados: set[str] = set()
    # (el ruido de __pycache__ se filtra al usarlo, no al recogerlo: git puede listarlo)
    for args in (["diff", "--name-only", desde_commit],
                 ["diff", "--name-only", "--cached", desde_commit],
                 ["diff", "--name-only", desde_commit, "--"],
                 ["ls-files", "--others", "--exclude-standard"]):
        cod, salida = _git(cwd, *args)
        if cod == 0:
            tocados.update(l.strip() for l in salida.splitlines() if l.strip())

    # 2) lo que EXISTIA en el commit base
    cod, salida = _git(cwd, "ls-tree", "-r", "--name-only", desde_commit)
    en_base = set(l.strip() for l in salida.splitlines() if l.strip()) if cod == 0 else set()

    # 3) todos los ficheros de test que hay AHORA en el arbol de trabajo
    ahora: set[str] = set()
    cod, salida = _git(cwd, "ls-files")
    if cod == 0:
        ahora.update(l.strip() for l in salida.splitlines() if l.strip())
    ahora.update(t for t in tocados)
    # y los que estan en disco aunque git no los liste. Con PODA: sin ella, un repo con
    # node_modules o .venv hace que esto tarde minutos.
    _PODAR = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
              ".mypy_cache", ".ruff_cache", ".tox", "dist", "build", "target", ".next"}
    for raiz, dirs, ficheros in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in _PODAR]
        for f in ficheros:
            rel = os.path.relpath(os.path.join(raiz, f), cwd)
            if _es_fichero_de_test(rel):
                ahora.add(rel)

    for rel in sorted(ahora):
        if _es_ruido(rel) or not _es_fichero_de_test(rel):
            continue
        if not (Path(cwd) / rel).is_file():
            continue                      # borrado
        if rel in tocados or rel not in en_base:  # nuevo o modificado en ESTE trabajo
            res["del_agente"].append(rel)  # nuevo o modificado en este trabajo
        else:
            res["preexistentes"].append(rel)

    res["ok"] = True
    res["motivo"] = (f"{len(res['preexistentes'])} test(s) preexistente(s), "
                     f"{len(res['del_agente'])} escrito(s)/tocado(s) por el agente (no cuentan)")
    return res


# ─────────────────────────────────────────────────────────────────────────────
# SENAL 1 — compila / importa
# ─────────────────────────────────────────────────────────────────────────────

_RUNNER_COMPILA = r'''
import py_compile, sys, tempfile, os
ruta = sys.argv[1]
fd, cf = tempfile.mkstemp(suffix=".pyc")   # el .pyc va a un temporal: no se ensucia el arbol
os.close(fd)
try:
    py_compile.compile(ruta, cfile=cf, doraise=True)
except py_compile.PyCompileError as e:
    print(e.msg if hasattr(e, "msg") else str(e)); sys.exit(1)
except Exception as e:
    print("%s: %s" % (type(e).__name__, e)); sys.exit(1)
finally:
    try: os.unlink(cf)
    except OSError: pass
print("compila OK")
'''

_RUNNER_IMPORT = r'''
import importlib.util, sys, traceback
ruta = sys.argv[1]
try:
    spec = importlib.util.spec_from_file_location("_colmena_probe_", ruta)
    if spec is None or spec.loader is None:
        print("no se pudo crear el spec de import"); sys.exit(3)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_colmena_probe_"] = mod
    spec.loader.exec_module(mod)
except SystemExit as e:
    if e.code not in (0, None):
        print("el modulo llamo a sys.exit(%r) al importarlo" % (e.code,)); sys.exit(4)
except BaseException:
    traceback.print_exc(); sys.exit(5)
print("import OK")
'''


def _senal_compila(art, rutas, timeout):
    detalles, fallos, omitidos = [], [], []
    hacer_import = art.get("importar", True)
    cwd = str(Path(art.get("cwd") or ".").resolve())

    for p in rutas:
        lang = _lenguaje(p)
        nombre = str(p)
        if lang == "python":
            cod, out = _correr([sys.executable, "-c", _RUNNER_COMPILA, nombre],
                               cwd=cwd, timeout=timeout)
            if cod != 0:
                fallos.append(f"[python:py_compile] {nombre}\n{out.strip()}")
                continue
            detalles.append(f"[python:py_compile] {nombre} OK")
            if hacer_import:
                env = os.environ.copy()
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                cod, out = _correr([sys.executable, "-c", _RUNNER_IMPORT, nombre],
                                   cwd=cwd, timeout=min(timeout, 30), env=env)
                if cod != 0:
                    fallos.append(f"[python:import] {nombre}\n{out.strip()}")
                else:
                    detalles.append(f"[python:import] {nombre} OK")
        elif lang == "js":
            if _hay("node"):
                cod, out = _correr(["node", "--check", nombre], cwd=cwd, timeout=timeout)
                (detalles if cod == 0 else fallos).append(
                    f"[js:node --check] {nombre}" + ("" if cod == 0 else f"\n{out.strip()}"))
            else:
                omitidos.append(f"[js] {nombre}: no hay node")
        elif lang == "ts":
            if _hay("tsc"):
                cod, out = _correr(["tsc", "--noEmit", nombre], cwd=cwd, timeout=timeout)
                (detalles if cod == 0 else fallos).append(
                    f"[ts:tsc --noEmit] {nombre}" + ("" if cod == 0 else f"\n{out.strip()}"))
            elif _hay("node"):
                omitidos.append(f"[ts] {nombre}: no hay tsc (node no sabe comprobar TypeScript)")
            else:
                omitidos.append(f"[ts] {nombre}: no hay tsc ni node")
        elif lang == "bash":
            if _hay("bash"):
                cod, out = _correr(["bash", "-n", nombre], cwd=cwd, timeout=timeout)
                (detalles if cod == 0 else fallos).append(
                    f"[bash -n] {nombre}" + ("" if cod == 0 else f"\n{out.strip()}"))
            else:
                omitidos.append(f"[bash] {nombre}: no hay bash")
        elif lang == "json":
            try:
                json.loads(Path(p).read_text(encoding="utf-8", errors="replace"))
                detalles.append(f"[json] {nombre} OK")
            except Exception as e:
                fallos.append(f"[json] {nombre}\n{type(e).__name__}: {e}")
        elif lang == "yaml":
            try:
                import yaml  # type: ignore
                yaml.safe_load(Path(p).read_text(encoding="utf-8", errors="replace"))
                detalles.append(f"[yaml] {nombre} OK")
            except ImportError:
                omitidos.append(f"[yaml] {nombre}: PyYAML no instalado")
            except Exception as e:
                fallos.append(f"[yaml] {nombre}\n{type(e).__name__}: {e}")
        elif lang == "toml":
            try:
                import tomllib
                with open(p, "rb") as f:
                    tomllib.load(f)
                detalles.append(f"[toml] {nombre} OK")
            except Exception as e:
                fallos.append(f"[toml] {nombre}\n{type(e).__name__}: {e}")
        elif lang == "texto":
            omitidos.append(f"[texto] {nombre}: nada que compilar")
        else:
            omitidos.append(f"[?] {nombre}: lenguaje no reconocido (ni extension ni shebang)")

    if fallos:
        return False, False, "\n".join(fallos + (["--"] + detalles if detalles else []))
    if not detalles:
        return True, True, "\n".join(omitidos) or "no habia nada compilable"
    return True, False, "\n".join(detalles + omitidos)


# ─────────────────────────────────────────────────────────────────────────────
# SENAL 2 — tipos
# ─────────────────────────────────────────────────────────────────────────────

def _senal_tipos(art, rutas, timeout):
    cwd = str(Path(art.get("cwd") or ".").resolve())
    pys = [str(p) for p in rutas if _lenguaje(p) == "python"]
    tss = [str(p) for p in rutas if _lenguaje(p) == "ts"]
    detalles, fallos, omitidos = [], [], []

    if pys:
        if _hay("mypy"):
            cod, out = _correr(["mypy", "--no-error-summary", "--ignore-missing-imports", *pys],
                               cwd=cwd, timeout=timeout)
            (detalles if cod == 0 else fallos).append(
                "[mypy] OK" if cod == 0 else f"[mypy]\n{out.strip()}")
        elif _hay("pyright"):
            cod, out = _correr(["pyright", "--outputjson", *pys], cwd=cwd, timeout=timeout)
            (detalles if cod == 0 else fallos).append(
                "[pyright] OK" if cod == 0 else f"[pyright]\n{out.strip()}")
        else:
            omitidos.append("[python] no hay mypy ni pyright instalados")

    if tss:
        if _hay("tsc"):
            cod, out = _correr(["tsc", "--noEmit", *tss], cwd=cwd, timeout=timeout)
            (detalles if cod == 0 else fallos).append(
                "[tsc] OK" if cod == 0 else f"[tsc]\n{out.strip()}")
        else:
            omitidos.append("[ts] no hay tsc instalado")

    if fallos:
        return False, False, "\n".join(fallos)
    if not detalles:
        return True, True, "\n".join(omitidos) or "no habia nada que tipar"
    return True, False, "\n".join(detalles + omitidos)


# ─────────────────────────────────────────────────────────────────────────────
# SENAL 3 — tests que YA existian
# ─────────────────────────────────────────────────────────────────────────────

def _senal_tests(art, rutas, timeout):
    cwd = str(Path(art.get("cwd") or ".").resolve())
    info = tests_preexistentes(cwd, art.get("desde_commit", "HEAD"))
    aviso = None
    if info["del_agente"]:
        # Redactado en neutro A PROPOSITO: este texto acaba en resumen_para_modelo() y ahi no
        # se le puede decir al modelo que el codigo es suyo (baja la tasa de correccion).
        aviso = ("%d fichero(s) de test se han creado o modificado en este trabajo; NO cuentan como "
                 "verificacion: %s" % (len(info["del_agente"]), ", ".join(info["del_agente"][:10])))

    if not info["ok"]:
        return True, True, info["motivo"], aviso

    pre = info["preexistentes"]
    if not pre:
        return True, True, ("no hay ningun test PREEXISTENTE que ejecutar. " + info["motivo"] +
                            ". Esta verificacion no prueba que el codigo funcione."), aviso

    pys = [t for t in pre if _pytest_lo_recoge(t)]
    otros = [t for t in pre if not t.endswith(".py")]
    auxiliares = [t for t in pre if t.endswith(".py") and not _pytest_lo_recoge(t)]
    partes, hubo_fallo, hubo_ok = [], False, False

    if pys:
        runner = None
        if _hay("pytest"):
            runner = ["pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
        else:
            cod, _o = _correr([sys.executable, "-c", "import pytest"], cwd=cwd, timeout=20)
            if cod == 0:
                runner = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
        if runner:
            cod, out = _correr([*runner, *pys], cwd=cwd, timeout=timeout)
            partes.append(f"$ {' '.join(runner[:2])} {' '.join(pys)}\n{out.strip()}")
            if cod == 0:
                hubo_ok = True
            elif cod == 5:
                partes.append("(pytest no recogio ningun test: no cuenta ni a favor ni en contra)")
            else:
                hubo_fallo = True
        else:
            cod, out = _correr([sys.executable, "-m", "unittest", "-v",
                                *[t[:-3].replace("/", ".") for t in pys]], cwd=cwd, timeout=timeout)
            partes.append(f"$ python -m unittest\n{out.strip()}")
            if cod == 0:
                hubo_ok = True
            elif cod == -2:
                partes.append("no hay pytest ni unittest utilizable")
            else:
                hubo_fallo = True

    if otros:
        # npm/cargo/make ejecutan TODA la bateria: no se puede excluir lo que escribio el agente.
        if info["del_agente"]:
            partes.append("tests no-Python preexistentes (%s) NO ejecutados: el runner del proyecto "
                          "correria tambien los tests que escribio el agente y eso no seria "
                          "verificacion independiente" % ", ".join(otros[:5]))
        else:
            hecho = False
            pkg = Path(cwd) / "package.json"
            if pkg.is_file() and _hay("npm"):
                try:
                    tiene = "test" in (json.loads(pkg.read_text()) or {}).get("scripts", {})
                except Exception:
                    tiene = False
                if tiene:
                    cod, out = _correr(["npm", "test", "--silent"], cwd=cwd, timeout=timeout)
                    partes.append(f"$ npm test\n{out.strip()}")
                    hubo_ok = hubo_ok or cod == 0
                    hubo_fallo = hubo_fallo or cod != 0
                    hecho = True
            if not hecho and (Path(cwd) / "Cargo.toml").is_file() and _hay("cargo"):
                cod, out = _correr(["cargo", "test", "-q"], cwd=cwd, timeout=timeout)
                partes.append(f"$ cargo test\n{out.strip()}")
                hubo_ok = hubo_ok or cod == 0
                hubo_fallo = hubo_fallo or cod != 0
                hecho = True
            if not hecho:
                mk = Path(cwd) / "Makefile"
                if mk.is_file() and _hay("make") and re.search(r"^test\s*:", mk.read_text(errors="replace"), re.M):
                    cod, out = _correr(["make", "test"], cwd=cwd, timeout=timeout)
                    partes.append(f"$ make test\n{out.strip()}")
                    hubo_ok = hubo_ok or cod == 0
                    hubo_fallo = hubo_fallo or cod != 0
                    hecho = True
            if not hecho:
                partes.append("tests no-Python preexistentes detectados pero sin runner disponible: "
                              + ", ".join(otros[:5]))

    cab = f"tests preexistentes: {', '.join(pre)}"
    if auxiliares:
        cab += (f"\n(auxiliares no ejecutables por pytest, solo vigilados: {', '.join(auxiliares)})")
    salida = cab + "\n" + "\n".join(partes)
    if hubo_fallo:
        return False, False, salida, aviso
    if hubo_ok:
        return True, False, salida, aviso
    return True, True, salida + "\n(no se pudo ejecutar ningun runner)", aviso


# ─────────────────────────────────────────────────────────────────────────────
# SENAL 4 — ejecucion real
# ─────────────────────────────────────────────────────────────────────────────

def _senal_ejecucion(art, rutas, timeout):
    humo = art.get("humo") or art.get("criterio_hecho_cmd")
    if not humo:
        return True, True, ("el artefacto no declara comando de humo ('humo'). "
                            "Sin ejecucion real, nadie ha visto correr esto.")
    cwd = str(Path(art.get("cwd") or ".").resolve())
    cmd = humo if isinstance(humo, (list, tuple)) else ["bash", "-c", humo]
    t = art.get("humo_timeout_s", timeout)
    cod, out = _correr(list(cmd), cwd=cwd, timeout=t)
    espera = art.get("humo_espera_exit", 0)
    txt = f"$ {humo if isinstance(humo, str) else ' '.join(cmd)}\nexit={cod}\n{out.strip()}"
    contiene = art.get("humo_contiene")
    if cod != espera:
        return False, False, txt
    if contiene and contiene not in out:
        return False, False, txt + f"\n[no aparece lo esperado en la salida: {contiene!r}]"
    return True, False, txt


# ─────────────────────────────────────────────────────────────────────────────
# SENAL 5 — linter
# ─────────────────────────────────────────────────────────────────────────────

def _senal_linter(art, rutas, timeout):
    cwd = str(Path(art.get("cwd") or ".").resolve())
    pys = [str(p) for p in rutas if _lenguaje(p) == "python"]
    jss = [str(p) for p in rutas if _lenguaje(p) in ("js", "ts")]
    shs = [str(p) for p in rutas if _lenguaje(p) == "bash"]
    detalles, fallos, omitidos = [], [], []

    if pys:
        if _hay("ruff"):
            cod, out = _correr(["ruff", "check", *pys], cwd=cwd, timeout=timeout)
            (detalles if cod == 0 else fallos).append("[ruff] OK" if cod == 0 else f"[ruff]\n{out.strip()}")
        else:
            omitidos.append("[python] no hay ruff instalado")
    if jss:
        if _hay("eslint"):
            cod, out = _correr(["eslint", *jss], cwd=cwd, timeout=timeout)
            # eslint sin config sale con codigo != 0 y un mensaje claro: eso es "omitida", no "fallo"
            if cod != 0 and re.search(r"(couldn't find|no configuration|ESLint couldn't find)", out, re.I):
                omitidos.append("[eslint] el proyecto no tiene configuracion de eslint")
            else:
                (detalles if cod == 0 else fallos).append(
                    "[eslint] OK" if cod == 0 else f"[eslint]\n{out.strip()}")
        else:
            omitidos.append("[js/ts] no hay eslint instalado")
    if shs:
        if _hay("shellcheck"):
            cod, out = _correr(["shellcheck", *shs], cwd=cwd, timeout=timeout)
            (detalles if cod == 0 else fallos).append(
                "[shellcheck] OK" if cod == 0 else f"[shellcheck]\n{out.strip()}")
        else:
            omitidos.append("[bash] no hay shellcheck instalado")

    if fallos:
        return False, False, "\n".join(fallos)
    if not detalles:
        return True, True, "\n".join(omitidos) or "no habia nada que lintar"
    return True, False, "\n".join(detalles + omitidos)


# ─────────────────────────────────────────────────────────────────────────────
# SENAL 6 — el diff
# ─────────────────────────────────────────────────────────────────────────────

def _diff_del_artefacto(art) -> str:
    d = art.get("diff")
    if d:
        return d
    cwd = art.get("cwd")
    if cwd and _es_repo_git(cwd):
        base = art.get("desde_commit", "HEAD")
        cod, out = _git(cwd, "diff", base)
        untracked = ""
        cod2, lst = _git(cwd, "ls-files", "--others", "--exclude-standard")
        if cod2 == 0:
            for rel in [l.strip() for l in lst.splitlines() if l.strip()]:
                if _es_ruido(rel):
                    continue
                f = Path(cwd) / rel
                try:
                    if f.is_file() and f.stat().st_size < 2_000_000:
                        cuerpo = f.read_text(encoding="utf-8", errors="replace")
                        untracked += (f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n"
                                      f"--- /dev/null\n+++ b/{rel}\n"
                                      + "".join("+" + l + "\n" for l in cuerpo.splitlines()))
                except OSError:
                    pass
        if cod == 0:
            return (out or "") + untracked
        return untracked
    return ""


def _senal_diff(art, rutas, timeout):
    diff = _diff_del_artefacto(art)
    problemas, notas = [], []

    if not diff.strip():
        notas.append("no hay diff que analizar (ni 'diff' en el artefacto ni cambios en git)")

    # -- ficheros tocados vs declarados
    tocados = sorted(set(re.findall(r"^\+\+\+ b/(.+)$", diff, re.M)) |
                     set(re.findall(r"^diff --git a/\S+ b/(\S+)$", diff, re.M)))
    tocados = [t for t in tocados if t != "/dev/null" and not _es_ruido(t)]
    declaradas = art.get("rutas") or []
    if declaradas and tocados:
        cwd = Path(art.get("cwd") or ".").resolve()
        decl_rel = set()
        for r in declaradas:
            p = Path(r)
            try:
                decl_rel.add(str(p.relative_to(cwd)) if p.is_absolute() else str(p))
            except ValueError:
                decl_rel.add(str(p))
        fuera = [t for t in tocados if t not in decl_rel]
        if fuera:
            problemas.append("toco ficheros NO declarados: " + ", ".join(fuera[:10]))
        notas.append(f"ficheros en el diff: {', '.join(tocados[:10])}")

    # -- tamano del cambio
    anadidas = len([l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")])
    borradas = len([l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")])
    notas.append(f"+{anadidas} / -{borradas} lineas")
    umbral = art.get("borrado_max", None)
    if umbral is not None and borradas > umbral:
        problemas.append(f"borro {borradas} lineas y el maximo declarado era {umbral}")
    elif borradas > 50 and borradas > 5 * max(anadidas, 1):
        problemas.append(f"cambio desproporcionado: borro {borradas} lineas y solo anadio {anadidas}")

    # -- secretos (en el diff Y en los ficheros declarados)
    cuerpos = [("diff", diff)]
    for p in rutas:
        try:
            if p.stat().st_size < 2_000_000:
                cuerpos.append((str(p), p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
    vistos = set()
    for donde, cuerpo in cuerpos:
        for nombre, rx in PATRONES_SECRETO:
            m = rx.search(cuerpo)
            if m and (nombre, donde) not in vistos:
                vistos.add((nombre, donde))
                trozo = m.group(0)
                tapado = trozo[:6] + "…" + trozo[-2:] if len(trozo) > 10 else trozo[:4] + "…"
                problemas.append(f"SECRETO: {nombre} en {donde} ({tapado})")

    # -- trabajo a medias
    if not art.get("permitir_marcadores", False):
        marcas = []
        for donde, cuerpo in cuerpos:
            if donde == "diff":
                cuerpo = "\n".join(l[1:] for l in cuerpo.splitlines() if l.startswith("+")
                                   and not l.startswith("+++"))
            for rx in PATRONES_MARCADOR:
                m = rx.search(cuerpo)
                if m:
                    marcas.append(f"{donde}: {m.group(0).strip()[:60]}")
        if marcas:
            problemas.append("trabajo a medias: " + " | ".join(sorted(set(marcas))[:6]))

    salida = "\n".join(problemas + (["--"] if problemas and notas else []) + notas)
    if problemas:
        return False, False, salida
    return True, False, salida or "diff limpio"


# ─────────────────────────────────────────────────────────────────────────────
# LA FUNCION DEL CONTRATO
# ─────────────────────────────────────────────────────────────────────────────

_CADENA = [
    ("compila",             _senal_compila),
    ("tipos",               _senal_tipos),
    ("tests_preexistentes", _senal_tests),
    ("ejecucion",           _senal_ejecucion),
    ("linter",              _senal_linter),
    ("diff",                _senal_diff),
]


def verificar(artefacto: dict) -> dict:
    """Cadena de senales objetivas, de la mas dura a la mas blanda, PARANDO en la primera que falle.

    Devuelve {'ok', 'señales':[{'nombre','ok','omitida','salida','ms'}], 'bloqueante', 'confianza', 'avisos', 'ms'}
    """
    return {
        "ok": False,
        "señales": [],
        "bloqueante": "snapshot_historico",
        "confianza": "nula",
        "independiente": False,
        "avisos": ["Verificación automática desactivada: consulte docs/KNOWN_ISSUES.md"],
        "ms": 0,
    }
    # Referencia histórica: código original no alcanzable desde la API pública.
    t_total = time.perf_counter()
    art = dict(artefacto or {})
    art.setdefault("cwd", ".")
    timeout = int(art.get("timeout_s", TIMEOUT_POR_DEFECTO_S))
    rutas = _rutas_reales(art)

    senales, avisos, bloqueante = [], [], None

    faltan = [r for r in (art.get("rutas") or [])
              if not (Path(r) if Path(r).is_absolute() else Path(art["cwd"]) / r).is_file()]
    if faltan:
        avisos.append("rutas declaradas que no existen: " + ", ".join(map(str, faltan[:8])))

    for nombre, fn in _CADENA:
        t0 = time.perf_counter()
        try:
            r = fn(art, rutas, timeout)
            if len(r) == 4:
                ok, omitida, salida, aviso = r
                if aviso:
                    avisos.append(aviso)
            else:
                ok, omitida, salida = r
        except Exception as e:                       # una senal rota NO puede dar 'ok'
            import traceback
            ok, omitida = False, False
            salida = f"[la senal '{nombre}' reventó]\n{traceback.format_exc()}"
        ms = int((time.perf_counter() - t0) * 1000)
        senales.append({"nombre": nombre, "ok": bool(ok), "omitida": bool(omitida),
                        "salida": _recortar(salida), "ms": ms})
        if not ok:
            bloqueante = nombre
            break                                    # fail-fast: cada senal cuesta tiempo

    duras_ok = [s["nombre"] for s in senales if s["ok"] and not s["omitida"]
                and s["nombre"] in SENALES_DURAS]
    if bloqueante:
        confianza = "nula"
    elif "tests_preexistentes" in duras_ok or "ejecucion" in duras_ok:
        confianza = "alta"
    elif duras_ok:
        confianza = "media"
    else:
        confianza = "nula"
        avisos.append("ninguna senal DURA pudo ejecutarse: esto no prueba que el codigo funcione")

    # 'independiente' = algo EJECUTO el codigo con un examen que no escribio el agente.
    # Es el booleano que debe mirar quien decida si una tarea esta 'hecha'.
    independiente = bloqueante is None and any(
        s in duras_ok for s in ("tests_preexistentes", "ejecucion"))

    return {
        "ok": bloqueante is None,
        "señales": senales,
        "bloqueante": bloqueante,
        "confianza": confianza,
        "independiente": independiente,
        "avisos": avisos,
        "ms": int((time.perf_counter() - t_total) * 1000),
    }


# ─────────────────────────────────────────────────────────────────────────────
# El texto que se le da al reparador — SIN decirle que el codigo es suyo
# ─────────────────────────────────────────────────────────────────────────────

_ATRIBUCION = re.compile(
    r"\b(tu c[oó]digo|tu implementaci[oó]n|tu parche|tu soluci[oó]n|tu fichero|tu test|"
    r"your code|your implementation|your patch|your solution|"
    r"escribiste|has escrito|generaste|has generado|"
    r"el agente (escribi[oó]|cre[oó]|modific[oó]))\b", re.I)

_QUE_PIDE = {
    "compila":             "que el fichero compile y se pueda importar sin lanzar excepcion",
    "tipos":               "que el comprobador de tipos no reporte errores",
    "tests_preexistentes": "que la bateria de tests que ya existia siga pasando entera",
    "ejecucion":           "que el comando de humo termine con codigo de salida 0",
    "linter":              "que el linter no reporte errores",
    "diff":                "que el cambio toque solo lo declarado, sin secretos ni trabajo a medias",
}


def resumen_para_modelo(resultado: dict, *, max_chars: int = 1800) -> str:
    """Convierte el resultado en un texto CORTO y ANONIMO para el modelo reparador.

    No atribuye la autoría del artefacto al modelo que recibe el fallo.
    """
    if not resultado:
        return "No hay resultado de verificacion."

    if resultado.get("ok"):
        hechas = [s["nombre"] for s in resultado.get("señales", []) if s["ok"] and not s["omitida"]]
        omit = [s["nombre"] for s in resultado.get("señales", []) if s["omitida"]]
        txt = "Este codigo pasa las comprobaciones ejecutadas: " + (", ".join(hechas) or "ninguna") + "."
        if omit:
            txt += " No se pudieron ejecutar: " + ", ".join(omit) + "."
        if resultado.get("confianza") == "nula":
            txt += " Aviso: ninguna comprobacion dura pudo ejecutarse, asi que esto no demuestra que funcione."
        elif not resultado.get("independiente", False):
            txt += (" Aviso: no lo ha ejecutado ninguna prueba independiente; "
                    "hace falta un test que ya existiera o un comando de humo.")
        for a in resultado.get("avisos", []):
            txt += " " + _ATRIBUCION.sub("este trabajo", a)
        return txt

    nombre = resultado.get("bloqueante") or "desconocida"
    salida = ""
    for s in resultado.get("señales", []):
        if s["nombre"] == nombre:
            salida = s.get("salida", "")
            break

    # anonimizar: fuera atribuciones, fuera rutas absolutas de la casa, fuera nombres de agente
    salida = _ATRIBUCION.sub("este codigo", salida)
    salida = re.sub(r"/home/[A-Za-z0-9_.\-]+/", "", salida)
    salida = re.sub(r"/tmp/[A-Za-z0-9_.\-]+/", "", salida)
    salida = salida.replace(str(Path.home()) + "/", "")

    # el final de la salida es donde esta el error de verdad (traceback, pytest, compilador)
    if len(salida) > max_chars:
        salida = "[...]\n" + salida[-max_chars:]

    pide = _QUE_PIDE.get(nombre, "que la comprobacion pase")
    return (f"Este codigo no pasa una comprobacion automatica.\n"
            f"Comprobacion que falla: {nombre}\n"
            f"Salida de la herramienta:\n{salida.strip()}\n"
            f"Objetivo de la correccion: {pide}. "
            f"Devuelve el fichero corregido entero.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI minima: python3 -m nucleo.verificar <fichero...> [--cwd DIR] [--humo CMD]
# ─────────────────────────────────────────────────────────────────────────────

def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Verifica artefactos con senales objetivas.")
    ap.add_argument("rutas", nargs="*")
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--desde", default="HEAD", help="commit base para separar lo preexistente")
    ap.add_argument("--humo", default=None, help="comando de humo que se ejecuta de verdad")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--resumen", action="store_true", help="imprime el texto para el reparador")
    a = ap.parse_args(argv)

    art = {"tipo": "codigo", "rutas": a.rutas, "cwd": a.cwd, "desde_commit": a.desde}
    if a.humo:
        art["humo"] = a.humo
    r = verificar(art)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif a.resumen:
        print(resumen_para_modelo(r))
    else:
        estado = "OK" if r["ok"] else f"BLOQUEA EN: {r['bloqueante']}"
        print(f"== verificar: {estado}  (confianza {r['confianza']}, {r['ms']} ms)")
        for s in r["señales"]:
            marca = "omitida" if s["omitida"] else ("ok" if s["ok"] else "FALLA ")
            print(f"  [{marca:>7}] {s['nombre']:<20} {s['ms']:>6} ms")
            cuerpo = (s["salida"] or "").strip()
            if cuerpo and (not s["ok"] or s["omitida"]):
                for l in cuerpo.splitlines()[:20]:
                    print(f"            | {l}")
        for av in r["avisos"]:
            print(f"  aviso: {av}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
