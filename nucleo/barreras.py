#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Políticas experimentales de comandos, recursos y presupuestos.

La edición pública contiene una allowlist vacía. Estas comprobaciones NO son
un sandbox ni una frontera de seguridad: el diseño histórico presenta rutas
de ejecución no cubiertas y posibles variantes de argumentos no detectadas.
Ver docs/KNOWN_ISSUES.md. No confiar código adversarial a este módulo.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Rutas y configuracion
# ─────────────────────────────────────────────────────────────────────────────

RAIZ = Path(__file__).resolve().parent.parent
RUTA_ALLOWLIST = RAIZ / "config" / "allowlist.json"
RUTA_CONFIG = RAIZ / "config" / "colmena.toml"
RUTA_ESTADO = RAIZ / "estado" / "barreras_presupuesto.json"

# Defaults tomados LITERALMENTE de docs/contratos.md, por si colmena.toml aun no existe.
CONFIG_POR_DEFECTO = {
    "limites": {
        "vram_reservada_mib": 1024,
        "disco_minimo_gb": 80,
        "bateria_minima_pct": 40,
        "pasos_max_tarea": 40,
        "segundos_max_tarea": 3600,
        "repeticiones_max": 3,
    }
}

# Nunca, pase lo que pase, aunque alguien los meta en el JSON a mano.
# Segundo candado: la allowlist puede equivocarse; esto no.
DENEGADOS_SIEMPRE = {
    "rm", "rmdir", "unlink", "shred", "dd", "mkfs", "mkswap", "fdisk", "sfdisk", "parted",
    "sgdisk", "wipefs", "sudo", "su", "doas", "pkexec", "chmod", "chown", "chgrp", "chattr",
    "setfacl", "mount", "umount", "swapoff", "systemctl", "service", "init", "shutdown",
    "reboot", "poweroff", "halt", "kill", "pkill", "killall", "curl", "wget", "nc", "ncat",
    "netcat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet", "apt", "apt-get", "dpkg",
    "pacman", "dnf", "yum", "snap", "flatpak", "modprobe", "insmod", "rmmod", "iptables",
    "nft", "tee", "truncate", "crontab", "at", "docker", "podman", "nvidia-smi",
}

# Argumentos que convierten un binario permitido en una shell. Vetados salvo permitir_eval.
ESCAPES = {
    # -m NO se veta: `python3 -m pytest` es uso legitimo. -c si: es una shell de Python.
    "python3": ("-c",), "python": ("-c",), "python3.14": ("-c",),
    "node": ("-e", "--eval", "-p", "--print"),
    "bash": ("-c",), "sh": ("-c",), "zsh": ("-c",),
    "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprintf", "-fls", "-fprint"),
    "git": ("--exec-path", "--upload-pack", "--receive-pack"),
    "perl": ("-e", "-E"), "ruby": ("-e"), "awk": (), "gawk": (),
}

_cfg_cache: dict | None = None


def cargar_config(recargar: bool = False) -> dict:
    """Lee config/colmena.toml. Si no existe, usa los defaults del contrato y lo dice."""
    global _cfg_cache
    if _cfg_cache is not None and not recargar:
        return _cfg_cache
    cfg = json.loads(json.dumps(CONFIG_POR_DEFECTO))   # copia profunda
    cfg["_origen"] = "defaults de docs/contratos.md (config/colmena.toml NO existe)"
    if RUTA_CONFIG.is_file():
        try:
            import tomllib
            with open(RUTA_CONFIG, "rb") as f:
                disco = tomllib.load(f)
            for sec, vals in disco.items():
                if isinstance(vals, dict):
                    cfg.setdefault(sec, {}).update(vals)
                else:
                    cfg[sec] = vals
            cfg["_origen"] = str(RUTA_CONFIG)
        except Exception as e:
            cfg["_origen"] = f"{RUTA_CONFIG} ILEGIBLE ({type(e).__name__}: {e}) -> defaults"
    _cfg_cache = cfg
    return cfg


def _lim(clave, defecto=None):
    return cargar_config().get("limites", {}).get(clave, CONFIG_POR_DEFECTO["limites"].get(clave, defecto))


def _ahora() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _escribir_atomico(ruta: Path, texto: str) -> None:
    """tmp -> write -> flush -> fsync(fd) -> os.replace -> fsync del directorio. Regla 1 del contrato."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(texto)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, ruta)
    fd = os.open(ruta.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sha256_fichero(ruta: str | Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# ALLOWLIST
# ─────────────────────────────────────────────────────────────────────────────

def cargar_allowlist() -> dict:
    if not RUTA_ALLOWLIST.is_file():
        return {"version": 1, "politica": {"permitir_eval": False}, "entradas": []}
    try:
        return json.loads(RUTA_ALLOWLIST.read_text(encoding="utf-8"))
    except Exception as e:
        # Fallar cerrado: una allowlist corrupta NO autoriza nada.
        return {"version": 0, "politica": {"permitir_eval": False}, "entradas": [],
                "_error": f"allowlist ilegible: {type(e).__name__}: {e}"}


def guardar_allowlist(al: dict) -> None:
    al["actualizada"] = _ahora()
    _escribir_atomico(RUTA_ALLOWLIST, json.dumps(al, ensure_ascii=False, indent=2) + "\n")


def _buscar_entrada(al: dict, clave: str) -> dict | None:
    """Por ruta absoluta exacta; si no, por nombre (solo si es INEQUIVOCO)."""
    for e in al.get("entradas", []):
        if e.get("ruta") == clave:
            return e
    cands = [e for e in al.get("entradas", []) if e.get("nombre") == clave]
    if len(cands) == 1:
        return cands[0]
    return None


def puede_ejecutar(comando) -> tuple[bool, str]:
    """Allowlist LITERAL: ruta absoluta + sha256 del binario. Nunca globs. Falla cerrado."""
    if not comando or not isinstance(comando, (list, tuple)):
        return False, "comando vacio o no es una lista (nunca se ejecuta a traves de shell)"
    prog = str(comando[0])
    base = os.path.basename(prog)

    # Candado 1: la lista negra manda por encima de cualquier JSON.
    if base in DENEGADOS_SIEMPRE:
        return False, f"'{base}' esta en la lista de DENEGADOS SIEMPRE (no se puede aprobar ni a mano)"

    al = cargar_allowlist()
    if al.get("_error"):
        return False, al["_error"] + " -> no se autoriza nada"

    entrada = _buscar_entrada(al, prog)
    if entrada is None:
        if not os.path.isabs(prog):
            homonimos = [e["nombre"] for e in al.get("entradas", []) if e.get("nombre") == base]
            if len(homonimos) > 1:
                return False, (f"'{prog}' es ambiguo: hay {len(homonimos)} entradas con ese nombre; "
                               f"usa la ruta absoluta")
            return False, (f"'{prog}' no esta en la allowlist (y no es ruta absoluta: "
                           f"esto NUNCA se resuelve por PATH)")
        return False, f"'{prog}' no esta en la allowlist ({RUTA_ALLOWLIST})"

    ruta = entrada["ruta"]
    if not os.path.isabs(ruta):
        return False, f"entrada invalida en la allowlist: '{ruta}' no es ruta absoluta"
    if not os.path.isfile(ruta):
        return False, f"el binario aprobado '{ruta}' ya no existe -> hay que re-aprobarlo"
    if not os.access(ruta, os.X_OK):
        return False, f"el binario aprobado '{ruta}' no es ejecutable"

    # Candado 2: el hash. Si apt actualizo el binario, esto dice NO.
    try:
        actual = sha256_fichero(ruta)
    except OSError as e:
        return False, f"no se pudo leer '{ruta}' para comprobar su sha256: {e}"
    if actual != entrada.get("sha256"):
        return False, (f"el binario {entrada.get('nombre', ruta)} cambio de hash, hay que re-aprobarlo "
                       f"(aprobado {str(entrada.get('sha256'))[:12]}… , ahora {actual[:12]}… ; "
                       f"reaprobar con: bin/colmena-allowlist reaprobar {entrada.get('nombre')})")

    # Candado 3: argumentos que convierten el binario en una shell.
    if not al.get("politica", {}).get("permitir_eval", False):
        args = [str(a) for a in comando[1:]]
        malos = ESCAPES.get(entrada.get("nombre", base), ())
        for a in args:
            corto = a.split("=", 1)[0]
            if corto in malos:
                return False, (f"'{entrada.get('nombre', base)} {corto}' es ejecucion arbitraria "
                               f"(escape de la allowlist): prohibido salvo politica.permitir_eval")
        propios = entrada.get("args_prohibidos", [])
        for a in args:
            if a.split("=", 1)[0] in propios:
                return False, f"argumento prohibido para {entrada.get('nombre')}: {a}"

    return True, f"aprobado: {ruta} sha256={actual[:12]}… (desde {entrada.get('aprobado', '?')})"


def revisar_allowlist() -> list[dict]:
    """Estado real de cada entrada AHORA MISMO (para el CLI y para informe())."""
    al = cargar_allowlist()
    out = []
    for e in al.get("entradas", []):
        r = {"nombre": e.get("nombre"), "ruta": e.get("ruta"), "estado": "?", "detalle": ""}
        ruta = e.get("ruta", "")
        if not os.path.isfile(ruta):
            r["estado"] = "AUSENTE"
            r["detalle"] = "el binario ya no existe"
        else:
            try:
                actual = sha256_fichero(ruta)
            except OSError as ex:
                r["estado"] = "ILEGIBLE"
                r["detalle"] = str(ex)
            else:
                if actual == e.get("sha256"):
                    r["estado"] = "OK"
                    r["detalle"] = actual[:16] + "…"
                else:
                    r["estado"] = "CAMBIADO"
                    r["detalle"] = f"aprobado {str(e.get('sha256'))[:12]}… ahora {actual[:12]}…"
        out.append(r)
    return out


def aprobar(ruta: str, nota: str = "", por: str = "operador", args_prohibidos=None) -> tuple[bool, str]:
    """Anade (o re-aprueba) un binario. Registra fecha + hash. Respeta la lista negra."""
    # NO se hace realpath: se aprueba la ruta TAL CUAL se va a invocar (/usr/bin/python3),
    # y el sha256 se calcula siguiendo el symlink. Si alguien repunta el symlink a otro
    # binario, el hash cambia y esto lo caza igual. Resolverla convertiria "python3" en
    # "python3.14" y el comando real dejaria de estar aprobado.
    ruta = os.path.abspath(os.path.expanduser(ruta))
    base = os.path.basename(ruta)
    if base in DENEGADOS_SIEMPRE:
        return False, f"'{base}' esta en DENEGADOS_SIEMPRE: no se puede anadir por diseno"
    if not os.path.isfile(ruta):
        return False, f"no existe: {ruta}"
    if not os.access(ruta, os.X_OK):
        return False, f"no es ejecutable: {ruta}"
    al = cargar_allowlist()
    if al.get("_error"):
        return False, al["_error"]
    sha = sha256_fichero(ruta)
    entradas = [e for e in al.get("entradas", []) if e.get("ruta") != ruta]
    real = os.path.realpath(ruta)
    entradas.append({
        "nombre": base, "ruta": ruta, "sha256": sha,
        "destino_real": real if real != ruta else None,
        "aprobado": _ahora(), "aprobado_por": por, "nota": nota,
        "args_prohibidos": list(args_prohibidos or []),
    })
    al["entradas"] = sorted(entradas, key=lambda e: (e["nombre"], e["ruta"]))
    guardar_allowlist(al)
    return True, f"aprobado {base} -> {ruta} sha256={sha[:16]}…"


def retirar(clave: str) -> tuple[bool, str]:
    al = cargar_allowlist()
    if al.get("_error"):
        return False, al["_error"]
    antes = len(al.get("entradas", []))
    al["entradas"] = [e for e in al.get("entradas", [])
                      if e.get("ruta") != clave and e.get("nombre") != clave]
    if len(al["entradas"]) == antes:
        return False, f"no habia ninguna entrada '{clave}'"
    guardar_allowlist(al)
    return True, f"quitadas {antes - len(al['entradas'])} entrada(s) de '{clave}'"


# ─────────────────────────────────────────────────────────────────────────────
# GUARDIAN: BATERIA
# ─────────────────────────────────────────────────────────────────────────────

def _leer_bateria() -> dict:
    """Estado real. Esta maquina NO tiene energy_now: usa charge_*. upower si funciona."""
    # anulacion para pruebas: COLMENA_BATERIA="Discharging 30"
    sim = os.environ.get("COLMENA_BATERIA")
    if sim:
        try:
            estado, pct = sim.split()
            return {"fuente": "COLMENA_BATERIA (simulada)", "status": estado,
                    "capacity": int(pct), "salud_pct": None, "w": None, "queda": None}
        except ValueError:
            pass

    d = {"fuente": None, "status": None, "capacity": None, "salud_pct": None, "w": None, "queda": None}
    base = Path("/sys/class/power_supply")
    bats = sorted([p for p in base.glob("BAT*") if p.is_dir()]) if base.is_dir() else []
    if bats:
        b = bats[0]
        d["fuente"] = str(b)
        try:
            d["status"] = (b / "status").read_text().strip()
        except OSError:
            pass
        try:
            d["capacity"] = int((b / "capacity").read_text().strip())
        except OSError:
            pass
        try:   # salud: esta maquina expone charge_full / charge_full_design (no energy_*)
            full = int((b / "charge_full").read_text().strip())
            dis = int((b / "charge_full_design").read_text().strip())
            if dis:
                d["salud_pct"] = round(100.0 * full / dis, 1)
        except OSError:
            pass
    if (d["status"] is None or d["capacity"] is None) and shutil.which("upower"):
        try:
            dev = subprocess.run(["upower", "-e"], capture_output=True, text=True, timeout=10).stdout
            linea = next((l.strip() for l in dev.splitlines() if "BAT" in l.upper()), None)
            if linea:
                txt = subprocess.run(["upower", "-i", linea], capture_output=True, text=True,
                                     timeout=10).stdout
                d["fuente"] = d["fuente"] or f"upower {linea}"
                for l in txt.splitlines():
                    l = l.strip()
                    if l.startswith("state:") and d["status"] is None:
                        d["status"] = l.split(":", 1)[1].strip().capitalize()
                    elif l.startswith("percentage:") and d["capacity"] is None:
                        d["capacity"] = int(float(l.split(":", 1)[1].strip().rstrip("%").replace(",", ".")))
                    elif l.startswith("energy-rate:"):
                        d["w"] = l.split(":", 1)[1].strip()
                    elif l.startswith("time to empty:") or l.startswith("time to full:"):
                        d["queda"] = l.split(":", 1)[1].strip()
                    elif l.startswith("capacity:") and d["salud_pct"] is None:
                        try:
                            d["salud_pct"] = round(float(l.split(":", 1)[1].strip()
                                                         .rstrip("%").replace(",", ".")), 1)
                        except ValueError:
                            pass
        except Exception:
            pass
    return d


def guardian_bateria() -> tuple[bool, str]:
    """False si Discharging, o por debajo del minimo (40%). Falla CERRADO si no hay sensor."""
    minimo = int(_lim("bateria_minima_pct", 40))
    d = _leer_bateria()
    if d["status"] is None and d["capacity"] is None:
        # Sin bateria detectable: puede ser un sobremesa. Falla cerrado y lo dice.
        if not Path("/sys/class/power_supply").is_dir():
            return False, "no hay /sys/class/power_supply: no se puede saber si hay corriente (fallo cerrado)"
        return False, "no se pudo leer el estado de la bateria (fallo cerrado)"
    st = (d["status"] or "").lower()
    pct = d["capacity"]
    extra = ""
    if d["salud_pct"] is not None:
        extra += f", salud {d['salud_pct']}%"
    if d["w"]:
        extra += f", {d['w']}"
    if d["queda"]:
        extra += f", {d['queda']}"
    if pct is not None and pct < minimo:
        return False, f"bateria al {pct}% (< {minimo}% minimo), estado {d['status']}{extra}"
    if st.startswith("discharging"):
        return False, (f"esta con BATERIA (Discharging {pct}%){extra}: prohibido iniciar swaps de "
                       f"modelo ni tareas largas")
    return True, f"corriente OK: {d['status']} {pct}%{extra} [{d['fuente']}]"


# ─────────────────────────────────────────────────────────────────────────────
# GUARDIAN: RECURSOS (disco + VRAM)
# ─────────────────────────────────────────────────────────────────────────────

GIB = 1024 ** 3


def _disco(ruta: str) -> dict:
    try:
        u = shutil.disk_usage(ruta)
        return {"ruta": ruta, "libre_gib": round(u.free / GIB, 1), "total_gib": round(u.total / GIB, 1)}
    except OSError as e:
        return {"ruta": ruta, "libre_gib": None, "total_gib": None, "error": str(e)}


def _vram() -> dict:
    d = {"usada_mib": None, "total_mib": None, "libre_mib": None, "error": None}
    exe = shutil.which("nvidia-smi") or "/usr/bin/nvidia-smi"
    if not os.path.isfile(exe):
        d["error"] = "no hay nvidia-smi"
        return d
    try:
        p = subprocess.run([exe, "--query-gpu=memory.used,memory.total",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=20)
        if p.returncode != 0:
            d["error"] = (p.stderr or p.stdout or "").strip()[:200] or f"exit {p.returncode}"
            return d
        linea = next((l for l in p.stdout.splitlines() if l.strip()), "")
        usada, total = [int(x.strip()) for x in linea.split(",")[:2]]
        d.update(usada_mib=usada, total_mib=total, libre_mib=total - usada)
    except Exception as e:
        d["error"] = f"{type(e).__name__}: {e}"
    return d


def guardian_recursos() -> tuple[bool, str]:
    """False si disco < minimo (80 GiB) o si la VRAM libre no deja los 1024 MiB del escritorio.

    Si un proceso agota la VRAM, kwin_wayland no puede asignar y se cae la sesion grafica de operador.
    Ese margen NO se toca.
    """
    min_gb = float(_lim("disco_minimo_gb", 80))
    reserva = int(_lim("vram_reservada_mib", 1024))
    problemas, notas = [], []

    for ruta in (str(RAIZ),):
        if not os.path.isdir(ruta):
            notas.append(f"{ruta}: no existe (se ignora)")
            continue
        d = _disco(ruta)
        if d["libre_gib"] is None:
            problemas.append(f"{ruta}: no se pudo leer el disco ({d.get('error')})")
        elif d["libre_gib"] < min_gb:
            problemas.append(f"{ruta}: {d['libre_gib']} GiB libres (< {min_gb} GiB minimo)")
        else:
            notas.append(f"{ruta}: {d['libre_gib']} GiB libres de {d['total_gib']}")

    v = _vram()
    if v["error"]:
        problemas.append(f"VRAM: no se pudo leer ({v['error']}) -> fallo cerrado")
    else:
        notas.append(f"VRAM: {v['libre_mib']} MiB libres de {v['total_mib']} "
                     f"(usados {v['usada_mib']}, reserva del escritorio {reserva})")
        if v["libre_mib"] < reserva:
            problemas.append(f"VRAM: solo {v['libre_mib']} MiB libres, por debajo de los {reserva} "
                             f"MiB reservados para el escritorio de operador (kwin_wayland se caeria)")

    if problemas:
        return False, " | ".join(problemas) + (" || " + " | ".join(notas) if notas else "")
    return True, " | ".join(notas)


def vram_libre_mib() -> int | None:
    return _vram()["libre_mib"]


# ─────────────────────────────────────────────────────────────────────────────
# PRESUPUESTO (pasos, segundos, repeticion)
# ─────────────────────────────────────────────────────────────────────────────

def _cargar_estado() -> dict:
    if not RUTA_ESTADO.is_file():
        return {}
    try:
        return json.loads(RUTA_ESTADO.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _guardar_estado(e: dict) -> None:
    _escribir_atomico(RUTA_ESTADO, json.dumps(e, ensure_ascii=False, indent=1) + "\n")


def iniciar_tarea(tarea_id) -> None:
    """Arranca el cronometro y los contadores de una tarea. Idempotente."""
    e = _cargar_estado()
    k = str(tarea_id)
    if k not in e:
        e[k] = {"t0": time.time(), "pasos": 0, "huellas": [], "iniciada": _ahora()}
        _guardar_estado(e)


def reiniciar_tarea(tarea_id) -> None:
    e = _cargar_estado()
    e.pop(str(tarea_id), None)
    _guardar_estado(e)


def registrar_paso(tarea_id, huella: str = "") -> tuple[bool, str]:
    """Suma un paso (y su huella) y devuelve el veredicto del presupuesto YA actualizado.

    La huella es lo que identifica la accion (p.ej. sha256 de comando+argumentos+fichero).
    Las huellas repetidas se comparan con el umbral configurado.
    """
    e = _cargar_estado()
    k = str(tarea_id)
    d = e.setdefault(k, {"t0": time.time(), "pasos": 0, "huellas": [], "iniciada": _ahora()})
    d["pasos"] = int(d.get("pasos", 0)) + 1
    if huella:
        d.setdefault("huellas", []).append(str(huella))
        d["huellas"] = d["huellas"][-20:]
    d["ultimo"] = _ahora()
    _guardar_estado(e)
    return presupuesto(tarea_id)


def presupuesto(tarea_id) -> tuple[bool, str]:
    """False si la tarea paso de pasos_max o segundos_max, o si repitio la misma accion 3 veces."""
    pasos_max = int(_lim("pasos_max_tarea", 40))
    seg_max = int(_lim("segundos_max_tarea", 3600))
    rep_max = int(_lim("repeticiones_max", 3))

    e = _cargar_estado()
    d = e.get(str(tarea_id))
    if d is None:
        return True, (f"tarea {tarea_id} sin consumo registrado "
                      f"(presupuesto entero: {pasos_max} pasos / {seg_max}s)")

    pasos = int(d.get("pasos", 0))
    transcurrido = time.time() - float(d.get("t0", time.time()))
    huellas = [h for h in d.get("huellas", []) if h]

    if pasos > pasos_max:
        return False, f"tarea {tarea_id}: {pasos} pasos > maximo {pasos_max}"
    if transcurrido > seg_max:
        return False, f"tarea {tarea_id}: {int(transcurrido)}s > maximo {seg_max}s"
    if rep_max > 0 and len(huellas) >= rep_max:
        ultimas = huellas[-rep_max:]
        if len(set(ultimas)) == 1:
            return False, (f"tarea {tarea_id}: BUCLE — la misma accion {rep_max} veces seguidas "
                           f"(huella {ultimas[0][:16]}…). Se corta: repetir un paso ya hecho es el "
                           f"patrón que este presupuesto trata de limitar")
    return True, (f"tarea {tarea_id}: {pasos}/{pasos_max} pasos, {int(transcurrido)}/{seg_max}s, "
                  f"{len(huellas)} huellas")


# ─────────────────────────────────────────────────────────────────────────────
# INFORME
# ─────────────────────────────────────────────────────────────────────────────

def informe(tarea_id=None) -> str:
    """El estado de todos los guardianes de un vistazo (para el CLI y para el panel)."""
    cfg = cargar_config(recargar=True)
    lin = []
    lin.append("═══ BARRERAS DE COLMENA ═══  " + _ahora())
    lin.append(f"config: {cfg.get('_origen')}")
    lin.append(f"        vram_reservada={_lim('vram_reservada_mib')} MiB · "
               f"disco_minimo={_lim('disco_minimo_gb')} GB · bateria_minima={_lim('bateria_minima_pct')}% · "
               f"pasos_max={_lim('pasos_max_tarea')} · segundos_max={_lim('segundos_max_tarea')}")
    lin.append("")

    ok_b, msg_b = guardian_bateria()
    lin.append(f"[{'PASA' if ok_b else 'CORTA'}] bateria    {msg_b}")

    ok_r, msg_r = guardian_recursos()
    for i, trozo in enumerate(msg_r.split(" || ")):
        lin.append(f"[{'PASA' if ok_r else 'CORTA'}] recursos   {trozo}" if i == 0
                   else f"                    {trozo}")

    al = cargar_allowlist()
    estados = revisar_allowlist()
    malas = [e for e in estados if e["estado"] != "OK"]
    lin.append(f"[{'PASA' if not malas and estados else 'CORTA'}] allowlist  "
               f"{len(estados)} binarios aprobados, {len(malas)} con problema "
               f"(eval={'SI' if al.get('politica', {}).get('permitir_eval') else 'NO'}) "
               f"· {RUTA_ALLOWLIST.name}")
    for e in estados:
        marca = "  ok " if e["estado"] == "OK" else f" {e['estado'][:4]}"
        lin.append(f"           {marca} {e['nombre']:<12} {e['ruta']:<32} {e['detalle']}")
    if not estados:
        lin.append("           (allowlist VACIA: no se puede ejecutar nada — falla cerrado)")
    lin.append(f"           denegados siempre (no se pueden aprobar): {len(DENEGADOS_SIEMPRE)} nombres "
               f"incl. rm, sudo, dd, mkfs, chmod, chown, curl, wget")

    est = _cargar_estado()
    if tarea_id is not None:
        ok_p, msg_p = presupuesto(tarea_id)
        lin.append(f"[{'PASA' if ok_p else 'CORTA'}] presupuesto {msg_p}")
    else:
        lin.append(f"[ info] presupuesto {len(est)} tarea(s) con consumo registrado en "
                   f"{RUTA_ESTADO.relative_to(RAIZ) if RUTA_ESTADO.is_relative_to(RAIZ) else RUTA_ESTADO}")
        for k, d in list(est.items())[:5]:
            lin.append(f"            tarea {k}: {d.get('pasos', 0)} pasos, "
                       f"{int(time.time() - float(d.get('t0', time.time())))}s")

    lin.append("")
    todo = ok_b and ok_r and not malas and bool(estados)
    lin.append(f"VEREDICTO: {'se puede trabajar' if todo else 'NO se puede trabajar'}")
    return "\n".join(lin)


def todo_ok(tarea_id=None) -> tuple[bool, str]:
    """Los guardianes en una sola llamada, para el motor. Fail-fast."""
    for fn in (guardian_bateria, guardian_recursos):
        ok, msg = fn()
        if not ok:
            return False, f"{fn.__name__}: {msg}"
    if tarea_id is not None:
        ok, msg = presupuesto(tarea_id)
        if not ok:
            return False, f"presupuesto: {msg}"
    return True, "todos los guardianes pasan"


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "informe":
        print(informe(*(sys.argv[2:] or [])))
    else:
        print(informe())
