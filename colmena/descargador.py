#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gestor experimental de descargas de modelos mediante aria2c.

Lee un manifiesto YAML explícito, guarda progreso y comprueba tamaño, archivos
.aria2, cabecera GGUF y SHA-256 cuando está suministrado. Comprobar la cabecera
no valida íntegramente un modelo. No se incluyen modelos ni sus credenciales.
El manifiesto público está vacío. --dry-run puede consultar tamaños remotos: no
es una opción offline. Las descargas y el uso real de GPU no se han validado
en esta publicación. Dependencias opcionales: PyYAML, requests y aria2c.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# --- rutas del proyecto (todo relativo a este fichero, nada cableado a mano) --
RAIZ      = Path(__file__).resolve().parent.parent
CONFIG    = RAIZ / "config" / "modelos.yaml"
DIR_ESTADO= RAIZ / "estado"
DIR_LOGS  = RAIZ / "logs"
ESTADO    = DIR_ESTADO / "descargas.json"
PIDFILE   = DIR_ESTADO / "descargador.pid"
LOG       = DIR_LOGS / "descargas.log"

ROLES_VALIDOS = {"general","codigo","razonamiento","vision","embeddings",
                 "reranker","draft","mmproj"}
MAX_INTENTOS   = 3             # vueltas SIN AVANZAR NI UN BYTE antes de rendirse
TOPE_VUELTAS   = 500           # tope duro de seguridad (nunca bucle infinito)
MARGEN_DISCO   = 1.10          # 10% de margen sobre lo que hace falta
REFRESCO_ESTADO= 1.0           # segundos entre volcados del JSON
SIN_PROGRESO_AVISO = 90        # s sin avanzar -> se dice en el mensaje

# Progreso de aria2c con --human-readable=false. La parte ETA es OPCIONAL:
# cuando la velocidad es 0 aria2 no la imprime.
RE_PROGRESO = re.compile(
    r"\[#(?P<gid>\w+)\s+(?P<hechos>\d+)B/(?P<total>\d+)B\((?P<pct>\d+)%\)"
    r"(?:\s+CN:(?P<cn>\d+))?(?:\s+DL:(?P<dl>\d+)B)?(?:\s+ETA:(?P<eta>\S+?))?\]"
)


# =============================================================================
# utilidades
# =============================================================================
def ahora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_lock_log = threading.Lock()

def log(msg: str) -> None:
    """Append al log, con fecha. Tambien a stdout (se ve en el tmux)."""
    linea = f"[{ahora()}] {msg}"
    with _lock_log:
        try:
            DIR_LOGS.mkdir(parents=True, exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write(linea + "\n")
        except Exception:
            pass
    print(linea, flush=True)


def humano(b: float) -> str:
    b = float(b or 0)
    for u, d in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if b >= d:
            return f"{b/d:.1f} {u}"
    return f"{int(b)} B"


def sha256_de(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as fh:
        for trozo in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(trozo)
    return h.hexdigest()


# =============================================================================
# el manifiesto
# =============================================================================
class Fichero:
    """Un fichero suelto a bajar (un modelo puede tener varios: GGUF partido)."""
    def __init__(self, url: str, destino: str, bytes_esp=None, sha256=None):
        self.url      = url
        self.destino  = Path(destino)
        self.bytes_esp= int(bytes_esp) if bytes_esp else None
        self.sha256   = (sha256 or "").strip().lower() or None
        # --- estado en vivo ---
        self.estado   = "pendiente"     # pendiente|bajando|verificando|ok|error
        self.hechos   = 0
        self.total    = self.bytes_esp or 0
        self.velocidad= 0
        self.mensaje  = ""
        self.proc     = None            # el Popen de aria2c
        self.ultimo_avance = time.time()

    @property
    def control(self) -> Path:
        return Path(str(self.destino) + ".aria2")

    def a_dict(self) -> dict:
        return {
            "fichero": self.destino.name,
            "destino": str(self.destino),
            "url": self.url,
            "estado": self.estado,
            "bytes_hechos": self.hechos,
            "bytes_total": self.total,
            "velocidad_bps": self.velocidad,
            "mensaje": self.mensaje,
        }


class Modelo:
    def __init__(self, d: dict):
        self.id        = str(d.get("id") or "").strip()
        self.rol       = str(d.get("rol") or "general").strip()
        self.repo      = str(d.get("repo") or "").strip()
        self.prioridad = int(d.get("prioridad", 50))
        self.activo    = bool(d.get("activo", True))
        self.notas     = (d.get("notas") or "").strip()
        self.ficheros: list[Fichero] = []

        brutos = d.get("ficheros")
        if brutos:                                   # forma B: varios trozos
            for b in brutos:
                self.ficheros.append(self._un_fichero(b, d))
        else:                                        # forma A: uno solo
            self.ficheros.append(self._un_fichero(d, d))

        # --- estado en vivo (agregado de sus ficheros) ---
        self.estado   = "pendiente"
        self.intentos = 0
        self.mensaje  = ""

    def _un_fichero(self, b: dict, padre: dict) -> Fichero:
        url = (b.get("url") or "").strip()
        if not url:
            # cable de comodidad: si no hay url pero si repo+fichero, se arma la
            # URL canonica de HuggingFace. Asi el manifiesto puede ser mas corto.
            repo = (b.get("repo") or padre.get("repo") or "").strip()
            fich = (b.get("fichero") or padre.get("fichero") or "").strip()
            if repo and fich:
                url = f"https://huggingface.co/{repo}/resolve/main/{fich}"
        destino = (b.get("destino") or "").strip()
        if not destino and url:
            raiz = padre.get("_raiz") or str(Path.home() / "models" / "colmena")
            destino = str(Path(raiz) / url.rsplit("/", 1)[-1].split("?")[0])
        return Fichero(url, destino, b.get("bytes"), b.get("sha256"))

    # --- agregados que ve la TUI ---
    @property
    def hechos(self) -> int:  return sum(f.hechos for f in self.ficheros)
    @property
    def total(self) -> int:   return sum(f.total for f in self.ficheros)
    @property
    def velocidad(self) -> int: return sum(f.velocidad for f in self.ficheros)
    @property
    def eta_s(self):
        v = self.velocidad
        if v <= 0 or self.total <= 0:
            return None
        falta = self.total - self.hechos
        return int(falta / v) if falta > 0 else 0

    def a_dict(self) -> dict:
        return {
            "id": self.id,
            "rol": self.rol,
            "repo": self.repo,
            "prioridad": self.prioridad,
            "estado": self.estado,
            "bytes_hechos": self.hechos,
            "bytes_total": self.total,
            "velocidad_bps": self.velocidad,
            "eta_s": self.eta_s,
            "intentos": self.intentos,
            "mensaje": self.mensaje,
            "ficheros": [f.a_dict() for f in self.ficheros],
        }


def cargar_manifiesto(ruta: Path = CONFIG):
    """Devuelve (paralelas, [Modelo]). Falla RUIDOSO si el YAML esta mal."""
    import yaml
    if not ruta.exists():
        raise SystemExit(f"ERROR: no existe el manifiesto {ruta}")
    with open(ruta, "r", encoding="utf-8") as fh:
        d = yaml.safe_load(fh) or {}
    paralelas = int(d.get("paralelas", 3))
    raiz      = d.get("raiz", str(Path.home() / "models" / "colmena"))
    brutos    = d.get("modelos") or []
    modelos, vistos, errores = [], set(), []
    for b in brutos:
        b = dict(b)
        b["_raiz"] = raiz
        m = Modelo(b)
        if not m.id:
            errores.append("hay una entrada sin `id`")
            continue
        if m.id in vistos:
            errores.append(f"`id` repetido: {m.id}")
        vistos.add(m.id)
        if m.rol not in ROLES_VALIDOS:
            errores.append(f"{m.id}: rol '{m.rol}' no valido "
                           f"({'|'.join(sorted(ROLES_VALIDOS))})")
        for f in m.ficheros:
            if not f.url:
                errores.append(f"{m.id}: fichero sin `url` (ni repo+fichero)")
            if not str(f.destino):
                errores.append(f"{m.id}: fichero sin `destino`")
            elif not f.destino.is_absolute():
                errores.append(f"{m.id}: `destino` no es ruta absoluta: {f.destino}")
        modelos.append(m)
    if errores:
        raise SystemExit("ERROR en el manifiesto:\n  - " + "\n  - ".join(errores))
    modelos.sort(key=lambda m: (m.prioridad, m.id))
    return paralelas, modelos


# =============================================================================
# verificacion
# =============================================================================
def verificar_fichero(f: Fichero, con_sha=True) -> tuple[bool, str]:
    """(ok, motivo). El motivo explica QUE fallo, para poder arreglarlo."""
    d = f.destino
    if not d.exists():
        return False, "no existe"
    if f.control.exists():
        # EL QUE DE VERDAD MANDA: si aria2 dejo su fichero de control, la
        # descarga no termino, por mucho que el tamaño cuadre.
        return False, "incompleto (queda su .aria2)"
    real = d.stat().st_size
    if f.bytes_esp and real != f.bytes_esp:
        return False, f"tamaño {real} != esperado {f.bytes_esp}"
    if d.suffix.lower() == ".gguf":
        with open(d, "rb") as fh:
            magic = fh.read(4)
        if magic != b"GGUF":
            return False, f"magic {magic!r} != b'GGUF'"
    if f.sha256 and con_sha:
        real_sha = sha256_de(d)
        if real_sha != f.sha256:
            return False, f"sha256 {real_sha[:16]}... != esperado {f.sha256[:16]}..."
    return True, "ok"


# =============================================================================
# espacio en disco
# =============================================================================
def tamaño_remoto(url: str, timeout=25):
    """HEAD para saber cuanto ocupa cuando el manifiesto no dio `bytes`."""
    try:
        import requests
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        n = r.headers.get("content-length") or r.headers.get("x-linked-size")
        return int(n) if n else None
    except Exception:
        return None


def comprobar_espacio(modelos, resolver_remoto=True) -> tuple[bool, str]:
    """
    Suma lo que HACE FALTA DE VERDAD por punto de montaje y compara con lo libre.
    Lo que ya esta reservado en disco (fichero preasignado a medias) NO se
    vuelve a contar: solo se pide el hueco que falta.
    """
    por_montaje: dict[str, int] = {}
    desconocidos = []
    for m in modelos:
        if not m.activo:
            continue
        for f in m.ficheros:
            ok, _ = verificar_fichero(f, con_sha=False)
            if ok:
                continue
            total = f.bytes_esp
            if total is None and resolver_remoto:
                total = tamaño_remoto(f.url)
                if total:
                    f.bytes_esp = total
                    f.total = total
            if total is None:
                desconocidos.append(f"{m.id}/{f.destino.name}")
                continue
            # OJO: se usa st_blocks*512 (espacio REALMENTE ocupado), no st_size.
            # Con --file-allocation=none el fichero es SPARSE: su st_size puede
            # decir 1,6 GB teniendo 11 MiB de bloques de verdad. Contar st_size
            # haria creer que ya esta reservado un sitio que no lo esta.
            ya = 0
            if f.destino.exists():
                st = f.destino.stat()
                ya = min(st.st_blocks * 512, total)
            falta = max(0, total - ya)
            punto = punto_de_montaje(f.destino)
            por_montaje[punto] = por_montaje.get(punto, 0) + falta

    partes = []
    todo_ok = True
    for punto, necesita in sorted(por_montaje.items()):
        libre = shutil.disk_usage(punto).free
        con_margen = int(necesita * MARGEN_DISCO)
        cabe = libre >= con_margen
        todo_ok = todo_ok and cabe
        partes.append(f"{punto}: hacen falta {humano(necesita)} "
                      f"(+10% = {humano(con_margen)}), libres {humano(libre)} "
                      f"-> {'CABE' if cabe else 'NO CABE'}")
    if not por_montaje:
        partes.append("no hace falta bajar nada nuevo")
    if desconocidos:
        partes.append("sin tamaño conocido (no se pudo contar): "
                      + ", ".join(desconocidos))
    return todo_ok, " | ".join(partes)


def punto_de_montaje(p: Path) -> str:
    p = p.resolve()
    d = p if p.is_dir() else p.parent
    while not d.exists() and d != d.parent:
        d = d.parent
    while not os.path.ismount(str(d)) and d != d.parent:
        d = d.parent
    return str(d)


# =============================================================================
# la descarga
# =============================================================================
def comando_aria2(f: Fichero, pid_padre: int) -> list[str]:
    f.destino.parent.mkdir(parents=True, exist_ok=True)
    return [
        "aria2c",
        "-x8", "-s8", "-k1M", "-c", "-j1",
        "--auto-file-renaming=false",     # nunca crear un .1 al lado
        "--allow-overwrite=false",        # nunca pisar lo que hay
        "--file-allocation=none",         # NO preasignar (leccion del 4-sep)
        "--human-readable=false",         # progreso en BYTES EXACTOS
        "--summary-interval=1",
        "--console-log-level=warn",
        "--retry-wait=10",
        "--max-tries=0",                  # con WiFi que va y viene: insiste
        "--timeout=60", "--connect-timeout=30",
        "--check-certificate=true",
        f"--stop-with-process={pid_padre}",
        f"--dir={f.destino.parent}",
        f"--out={f.destino.name}",
        f.url,
    ]


def descargar_fichero(f: Fichero, parar: threading.Event, pid_padre: int) -> int:
    """Lanza aria2c y va leyendo su progreso. Devuelve el codigo de salida."""
    cmd = comando_aria2(f, pid_padre)
    log(f"    aria2c <- {f.destino.name}")
    f.estado = "bajando"
    f.mensaje = ""
    f.ultimo_avance = time.time()
    # start_new_session=True: cada aria2c en su PROPIA sesion de procesos, para
    # que una señal dirigida al grupo del demonio (o de la terminal) no se lo
    # lleve por delante. Se sigue pudiendo parar por PID (p.terminate()) y
    # --stop-with-process sigue valiendo, porque los dos van por PID exacto.
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, errors="replace",
                         start_new_session=True)
    f.proc = p
    ultimo_hechos = f.hechos
    try:
        for linea in p.stdout:                       # streaming, no bloquea al morir
            linea = linea.rstrip("\n")
            m = RE_PROGRESO.search(linea)
            if m:
                f.hechos    = int(m.group("hechos"))
                f.total     = int(m.group("total")) or f.total
                f.velocidad = int(m.group("dl") or 0)
                if f.hechos > ultimo_hechos:
                    ultimo_hechos = f.hechos
                    f.ultimo_avance = time.time()
                    f.mensaje = ""
                else:
                    quieto = time.time() - f.ultimo_avance
                    if quieto > SIN_PROGRESO_AVISO:
                        f.mensaje = f"sin avanzar desde hace {int(quieto//60)}m (¿WiFi?)"
            elif linea.strip():
                bajo = linea.lower()
                # la coletilla "Compruebe el archivo de registro si se
                # producen errores..." la imprime aria2 SIEMPRE al salir: lleva
                # la palabra "error" pero no es un error. No ensuciar con ella.
                ruido = ("compruebe el archivo de registro" in bajo
                         or "si se producen errores" in bajo)
                if not ruido and ("error" in bajo or "exception" in bajo
                        or "warn" in bajo or "no se pudo" in bajo):
                    f.mensaje = linea.strip()[:160]
                    log(f"    aria2c[{f.destino.name}]: {linea.strip()[:200]}")
            if parar.is_set():
                break
    finally:
        if parar.is_set() and p.poll() is None:
            # SIGTERM: aria2 cierra limpio y GUARDA su .aria2 -> reanudable
            p.terminate()
            try:
                p.wait(timeout=25)
            except subprocess.TimeoutExpired:
                p.kill()
        rc = p.wait()
        f.velocidad = 0
        f.proc = None
    return rc


def procesar_modelo(m: Modelo, parar: threading.Event, pid_padre: int) -> None:
    """Baja (y verifica) todos los ficheros de un modelo. Hasta 3 intentos."""
    log(f"  >> {m.id} (prio {m.prioridad}, {len(m.ficheros)} fichero/s)")
    m.estado = "bajando"
    for f in m.ficheros:
        if parar.is_set():
            break
        ok, motivo = verificar_fichero(f)
        if ok:
            f.estado, f.mensaje = "ok", "ya estaba"
            f.hechos = f.total = f.destino.stat().st_size
            log(f"    = {f.destino.name}: ya estaba OK, no se toca")
            continue
        log(f"    - {f.destino.name}: {motivo} -> a bajar")

        # -------------------------------------------------------------------
        # El presupuesto de reintentos cuenta vueltas sin progreso. El tope global
        # de vueltas sigue limitando la descarga aunque haya avances parciales.
        exito = False
        sin_avance = 0
        vuelta = 0
        while not parar.is_set() and vuelta < TOPE_VUELTAS:
            vuelta += 1
            antes = f.hechos
            m.intentos = vuelta
            rc = descargar_fichero(f, parar, pid_padre)
            if parar.is_set():
                f.estado = "pendiente"
                f.mensaje = "parado por peticion (reanudable)"
                break
            f.estado = "verificando"
            ok, motivo = verificar_fichero(f)
            if ok:
                f.estado, f.mensaje = "ok", ""
                f.hechos = f.total = f.destino.stat().st_size
                log(f"    OK {f.destino.name} · {humano(f.total)} · verificado")
                exito = True
                break
            if f.hechos > antes:                      # avanzo -> no penaliza
                sin_avance = 0
                espera = 5
                f.mensaje = (f"cortado a media descarga (rc={rc}); reanudando "
                             f"desde {humano(f.hechos)}")
                log(f"    ~~ {f.destino.name}: cortado (rc={rc}) pero avanzo "
                    f"{humano(f.hechos - antes)} -> reanuda (vuelta {vuelta})")
            else:                                     # ni un byte -> penaliza
                sin_avance += 1
                espera = 15 * sin_avance
                f.mensaje = (f"sin avanzar {sin_avance}/{MAX_INTENTOS}: "
                             f"rc={rc} · {motivo}")
                log(f"    !! {f.destino.name}: rc={rc} · {motivo} "
                    f"(sin avanzar {sin_avance}/{MAX_INTENTOS})")
                if sin_avance >= MAX_INTENTOS:
                    break
            for _ in range(espera):
                if parar.is_set():
                    break
                time.sleep(1)
        if not exito and not parar.is_set():
            f.estado = "error"
            m.estado = "error"
            m.mensaje = f.mensaje
            log(f"  XX {m.id}: se rinde con {f.destino.name} · {f.mensaje}")
            return

    if parar.is_set():
        m.estado = "pendiente"
        m.mensaje = "parado (reanudable)"
    elif all(f.estado == "ok" for f in m.ficheros):
        m.estado, m.mensaje = "ok", ""
        log(f"  OK {m.id} COMPLETO · {humano(m.total)}")


# =============================================================================
# el fichero de estado (atomico)
# =============================================================================
def volcar_estado(modelos, paralelas: int, estado_demonio: str) -> None:
    activos = [m for m in modelos if m.activo]
    d = {
        "actualizado": ahora(),
        "actualizado_ts": time.time(),
        "demonio_pid": os.getpid(),
        "demonio_estado": estado_demonio,
        "paralelas": paralelas,
        "manifiesto": str(CONFIG),
        "totales": {
            "bytes_hechos": sum(m.hechos for m in activos),
            "bytes_total":  sum(m.total  for m in activos),
            "velocidad_bps":sum(m.velocidad for m in activos),
            "ok":        sum(1 for m in activos if m.estado == "ok"),
            "bajando":   sum(1 for m in activos if m.estado == "bajando"),
            "pendientes":sum(1 for m in activos if m.estado == "pendiente"),
            "error":     sum(1 for m in activos if m.estado == "error"),
            "modelos":   len(activos),
        },
        "modelos": [m.a_dict() for m in activos],
    }
    v = d["totales"]["velocidad_bps"]
    falta = d["totales"]["bytes_total"] - d["totales"]["bytes_hechos"]
    d["totales"]["eta_s"] = int(falta / v) if v > 0 and falta > 0 else None

    DIR_ESTADO.mkdir(parents=True, exist_ok=True)
    tmp = DIR_ESTADO / "descargas.json.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, ESTADO)          # <- atomico: la TUI nunca lee a medias


# =============================================================================
# pid-file
# =============================================================================
def demonio_vivo():
    """Devuelve el PID si ya hay un descargador corriendo, si no None."""
    if not PIDFILE.exists():
        return None
    try:
        pid = int(PIDFILE.read_text().strip())
    except Exception:
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except Exception:
        return None                                  # no existe ese proceso
    return pid if "descargador.py" in cmdline else None


# =============================================================================
# main
# =============================================================================
def main() -> int:
    global CONFIG
    ap = argparse.ArgumentParser(
        prog="descargador.py",
        description="COLMENA · demonio de descarga de modelos (aria2c en paralelo)")
    ap.add_argument("--config", default=str(CONFIG), help="ruta del manifiesto")
    ap.add_argument("--paralelas", type=int, default=None,
                    help="cuantas a la vez (por defecto, lo del manifiesto)")
    ap.add_argument("--dry-run", action="store_true",
                    help="no baja nada: dice que haria y si cabe en disco")
    ap.add_argument("--verificar", metavar="RUTA",
                    help="verifica un fichero suelto y sale")
    args = ap.parse_args()

    # --- modo verificar suelto (para probar la verificacion sin bajar nada) ---
    if args.verificar:
        f = Fichero("(sin url)", args.verificar)
        p = Path(args.verificar)
        if p.exists():
            f.bytes_esp = p.stat().st_size          # se comprueba magic + .aria2
        ok, motivo = verificar_fichero(f)
        print(f"{args.verificar}\n  -> {'OK' if ok else 'FALLA'}: {motivo}")
        if p.exists():
            with open(p, "rb") as fh:
                print(f"  tamaño: {p.stat().st_size} · magic: {fh.read(4)!r}")
        return 0 if ok else 1

    CONFIG = Path(args.config).resolve()
    paralelas, modelos = cargar_manifiesto(CONFIG)
    if args.paralelas:
        paralelas = args.paralelas
    activos = [m for m in modelos if m.activo]

    DIR_ESTADO.mkdir(parents=True, exist_ok=True)
    DIR_LOGS.mkdir(parents=True, exist_ok=True)

    log("=" * 72)
    log(f"COLMENA descargador · pid {os.getpid()} · manifiesto {CONFIG}")
    log(f"{len(activos)} modelo/s activos de {len(modelos)} · {paralelas} en paralelo")

    # --- ya hay uno corriendo? (un --dry-run no arranca nada: se deja pasar,
    #     asi se puede simular un manifiesto mientras el demonio trabaja) ---
    otro = demonio_vivo() if not args.dry_run else None
    if otro:
        log(f"YA HAY UN DESCARGADOR CORRIENDO (pid {otro}). No se lanza otro.")
        print(f"\n  Ya hay un descargador corriendo (pid {otro}).\n"
              f"  Para verlo:   bin/colmena-descargas\n"
              f"  Para pararlo: bin/colmena-bajar parar\n")
        return 3

    # --- espacio ANTES de nada ---
    cabe, detalle = comprobar_espacio(activos)
    log(f"DISCO · {detalle}")
    if not cabe:
        log("ABORTA: no cabe en disco. No se ha bajado nada.")
        print(f"\n  NO CABE EN DISCO.\n  {detalle}\n"
              f"  Libera sitio o pon `activo: false` a algun modelo.\n")
        return 2

    if args.dry_run:
        print("\n  --dry-run · esto es lo que se haria:\n")
        for m in activos:
            print(f"   [{m.prioridad}] {m.id}  ({m.rol})")
            for f in m.ficheros:
                ok, motivo = verificar_fichero(f, con_sha=False)
                print(f"       {'YA OK ' if ok else 'BAJAR '} {f.destino}"
                      f"   {humano(f.bytes_esp or 0)}   ({motivo})")
        print(f"\n   disco: {detalle}\n")
        return 0

    PIDFILE.write_text(str(os.getpid()))
    parar = threading.Event()

    def al_recibir_señal(signum, frame):
        log(f"señal {signum} recibida -> parando limpio "
            f"(aria2 guarda su .aria2, se reanuda luego)")
        parar.set()
    signal.signal(signal.SIGTERM, al_recibir_señal)
    signal.signal(signal.SIGINT,  al_recibir_señal)

    # --- hilo que vuelca el estado cada segundo ---
    def escritor():
        while not parar.is_set():
            try:
                volcar_estado(modelos, paralelas, "activo")
            except Exception as e:
                log(f"(escritor de estado) {e}")
            time.sleep(REFRESCO_ESTADO)
    hilo_estado = threading.Thread(target=escritor, daemon=True, name="estado")
    hilo_estado.start()

    # --- a bajar: N a la vez, por prioridad (el orden de submit la respeta) ---
    from concurrent.futures import ThreadPoolExecutor
    pid_padre = os.getpid()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, paralelas), thread_name_prefix="dl") as pool:
        futuros = [pool.submit(procesar_modelo, m, parar, pid_padre) for m in activos]
        for fu in futuros:
            try:
                fu.result()
            except Exception as e:
                log(f"EXCEPCION en un hilo de descarga: {e!r}")

    parar.set()
    estado_final = "parado" if any(m.estado == "pendiente" for m in activos) else "terminado"
    try:
        volcar_estado(modelos, paralelas, estado_final)
    except Exception:
        pass
    try:
        PIDFILE.unlink()
    except Exception:
        pass

    ok  = sum(1 for m in activos if m.estado == "ok")
    err = sum(1 for m in activos if m.estado == "error")
    log(f"FIN · {ok} ok · {err} con error · {len(activos)-ok-err} sin acabar "
        f"· {int(time.time()-t0)}s")
    log("=" * 72)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
