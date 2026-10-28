"""Notas Markdown con procedencia, propuestas y curación manual.

Las etiquetas de origen separan apuntes directos y propuestas de modelos.
Se trata de una política de la aplicación, no de autenticación ni aislamiento
frente a código que pueda editar los archivos o elegir su propia etiqueta.
Los commits son locales y solo se intentan si la raíz contiene un repositorio.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path

# ── constantes de política ────────────────────────────────────────────────────

#: Orígenes que pueden escribir DIRECTAMENTE en la memoria (prefijo antes de ':').
ORIGENES_CONFIABLES = frozenset({"ser", "humano", "regla", "sistema", "prueba"})

#: Tipos que `curar()` asciende a MEMORIA.md. Lo demás se queda en el diario.
TIPOS_PERDURABLES = frozenset({"decision", "leccion", "preferencia", "contrato"})

#: Tipos que una REGLA determinista puede aceptar sola. El resto exige a operador.
TIPOS_AUTOACEPTABLES = frozenset({"observacion", "hecho"})

#: Patrones que descalifican una propuesta para la aceptación automática.
#: No pretenden ser un antivirus: son el filtro barato que evita las clases de
#: envenenamiento documentadas (instrucción encubierta, exfiltración, credencial).
PATRONES_PELIGROSOS = [
    (r"\bignora\w*\s+(las\s+)?(instrucciones|reglas|lo\s+anterior)", "instrucción encubierta"),
    (r"\b(ignore|disregard)\s+(all\s+)?(previous|prior)\s+instructions?", "instrucción encubierta (en)"),
    (r"\ba partir de ahora\b|\bde ahora en adelante\b|\bfrom now on\b", "reescritura de política"),
    (r"\bno\s+(se\s+lo\s+)?(digas|cuentes|menciones)\b|\bdon'?t tell\b", "ocultación"),
    (r"\brm\s+-rf\b|\bsudo\b|\bchmod\s+777\b|\bcurl\b|\bwget\b|\bnc\s+-\w*e", "comando peligroso"),
    (r"(api[_\- ]?key|secret|password|contrase\w+a|token)\s*[:=]", "credencial"),
    (r"https?://", "URL (una URL en memoria la revisa operador)"),
    (r"(^|\s)(/etc/|/root/|~/\.ssh|/home/[^/\s]+/EIDOS|~/\.claude)", "ruta fuera del proyecto"),
    (r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "caracteres de control"),
]

_RE_CABECERA = re.compile(
    r"^###\s+(?P<id>n-\d{8}-\d{6}-[0-9a-f]{4})\s+·\s+"
    r"(?P<fecha>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\s+·\s+"
    r"tipo=(?P<tipo>[\w\-]+)\s+·\s+origen=(?P<origen>\S+)\s+·\s+tags=(?P<tags>.*)$"
)


class MemoriaNoAutorizada(RuntimeError):
    """Se intentó escribir en la memoria de verdad desde un origen que solo puede proponer."""


# ── utilidades de escritura durable (regla 1 de docs/contratos.md) ────────────

def _escribir_atomico(ruta: Path, contenido: str) -> None:
    """tmp → write → flush → fsync(fd) → os.replace → fsync(dir). Sin atajos."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ruta.parent), prefix=".tmp-", suffix="-" + ruta.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contenido)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp crea con 0600 y os.replace conserva el modo: la memoria acabaría
        # ilegible para el resto del sistema. Se hereda el modo previo, o 0644.
        os.chmod(tmp, (ruta.stat().st_mode & 0o777) if ruta.exists() else 0o644)
        os.replace(tmp, ruta)
        dfd = os.open(str(ruta.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _anexar_jsonl(ruta: Path, obj: dict) -> None:
    """Append-only con fsync. El registro de auditoría nunca se reescribe."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# ── el módulo ─────────────────────────────────────────────────────────────────

class Notas:
    def __init__(self, raiz: str | Path | None = None) -> None:
        if raiz is None:
            raiz = os.environ.get("COLMENA_RAIZ") or Path(__file__).resolve().parents[1]
        self.raiz = Path(raiz).resolve()
        self.dir = self.raiz / "memoria"
        self.memoria_md = self.dir / "MEMORIA.md"
        self.diario = self.dir / "diario"
        self.propuestas = self.dir / "propuestas"
        self.pendientes = self.propuestas / "pendientes"
        self.rechazadas = self.propuestas / "rechazadas"
        self.registro = self.propuestas / "REGISTRO.jsonl"
        self.locks = self.dir / ".locks"
        for d in (self.diario, self.pendientes, self.rechazadas, self.locks):
            d.mkdir(parents=True, exist_ok=True)
        self._motor_texto: tuple[str, list[str], str | None] | None = None

    # ── candados ─────────────────────────────────────────────────────────────
    @contextmanager
    def _lock(self, nombre: str):
        f = self.locks / f"{nombre}.lock"
        fh = open(f, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    @staticmethod
    def _familia(origen: str) -> str:
        return origen.split(":", 1)[0].strip().lower()

    def _exigir_origen_confiable(self, origen: str, accion: str) -> None:
        if self._familia(origen) not in ORIGENES_CONFIABLES:
            raise MemoriaNoAutorizada(
                f"origen '{origen}' no puede {accion} directamente. "
                f"Un modelo/agente usa proponer(); acepta operador o una regla determinista. "
                f"Orígenes con permiso de escritura: {sorted(ORIGENES_CONFIABLES)}"
            )

    # ── git ──────────────────────────────────────────────────────────────────
    def _git(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        base = ["git", "-C", str(self.raiz),
                "-c", "user.name=COLMENA", "-c", "user.email=colmena@local"]
        return subprocess.run(base + list(args), capture_output=True, text=True, timeout=timeout)

    def es_repo(self) -> bool:
        return (self.raiz / ".git").exists()

    def commit(self, rutas: list[Path], mensaje: str) -> dict:
        """Commit SOLO de las rutas dadas (pathspec), para no arrastrar el trabajo
        de otros agentes que estén tocando el mismo repo a la vez.
        Devuelve {'ok', 'hash', 'motivo'} — nunca finge un commit que no ocurrió."""
        if not self.es_repo():
            return {"ok": False, "hash": None, "motivo": "no es un repo git"}
        rel = [str(Path(r).resolve().relative_to(self.raiz)) for r in rutas
               if Path(r).exists()]
        if not rel:
            return {"ok": False, "hash": None, "motivo": "nada que commitear"}
        ultimo = ""
        for intento in range(4):  # index.lock: otro agente puede estar commiteando
            # -f: las rutas van explícitas y todas son de memoria/. Es el cinturón
            # contra un .gitignore ajeno que deje la memoria sin traza (ya pasó).
            add = self._git("add", "-f", "--", *rel)
            if add.returncode != 0:
                ultimo = add.stderr.strip()
                time.sleep(0.4 * (intento + 1))
                continue
            hecho = self._git("commit", "-q", "-m", mensaje, "--", *rel)
            if hecho.returncode == 0:
                h = self._git("rev-parse", "--short", "HEAD").stdout.strip()
                return {"ok": True, "hash": h, "motivo": ""}
            salida = (hecho.stdout + hecho.stderr).strip()
            if "nothing to commit" in salida or "sin cambios" in salida or "nada para hacer commit" in salida:
                h = self._git("rev-parse", "--short", "HEAD").stdout.strip()
                return {"ok": False, "hash": h, "motivo": "sin cambios que commitear"}
            ultimo = salida
            time.sleep(0.4 * (intento + 1))
        return {"ok": False, "hash": None, "motivo": ultimo or "commit falló"}

    # ── escribir en la memoria de verdad ─────────────────────────────────────
    def _nuevo_id(self, texto: str, cuando: datetime) -> str:
        h = hashlib.sha256(f"{texto}{time.time_ns()}".encode()).hexdigest()[:4]
        return f"n-{cuando:%Y%m%d-%H%M%S}-{h}"

    def ruta_diario(self, dia: date | None = None) -> Path:
        return self.diario / f"{(dia or date.today()):%Y-%m-%d}.md"

    def apuntar(self, texto: str, tipo: str = "nota", tags: list[str] | tuple[str, ...] = (),
                *, origen: str = "ser", commitear: bool = True,
                propuesta_id: str | None = None, aceptada_por: str | None = None) -> dict:
        """Escribe una nota en el diario de hoy. SOLO para orígenes confiables.

        Un modelo NO llega aquí: `proponer()` es su única puerta (ver MemoriaNoAutorizada)."""
        self._exigir_origen_confiable(origen, "apuntar")
        texto = texto.strip()
        if not texto:
            raise ValueError("nota vacía")
        ahora = datetime.now()
        nid = self._nuevo_id(texto, ahora)
        tags = [t.strip().lstrip("#") for t in tags if str(t).strip()]
        extra = ""
        if propuesta_id:
            extra += f" · propuesta={propuesta_id}"
        if aceptada_por:
            extra += f" · aceptada_por={aceptada_por}"
        cabecera = (f"### {nid} · {ahora:%Y-%m-%d %H:%M:%S} · tipo={tipo} · "
                    f"origen={origen} · tags={','.join(tags)}{extra}")
        cuerpo = texto.replace("\n### ", "\n#### ")  # que nada de dentro finja ser cabecera
        ruta = self.ruta_diario(ahora.date())
        with self._lock("diario"):
            previo = ruta.read_text(encoding="utf-8") if ruta.exists() else (
                f"# Diario de COLMENA · {ahora:%Y-%m-%d}\n"
                f"> Nivel 1 (contexto del día) y nivel 3 (lo del agente). "
                f"Lo que perdure lo asciende `curar()` a MEMORIA.md.\n")
            nuevo = previo.rstrip("\n") + "\n\n" + cabecera + "\n" + cuerpo + "\n"
            _escribir_atomico(ruta, nuevo)
            linea = nuevo[:nuevo.index(cabecera)].count("\n") + 1
        res = {"id": nid, "ruta": str(ruta), "linea": linea, "linea_texto": linea + 1,
               "tipo": tipo, "origen": origen, "tags": tags,
               "fecha": f"{ahora:%Y-%m-%d %H:%M:%S}",
               "cita": f"{ruta.relative_to(self.raiz)}:{linea}"}
        res["commit"] = self.commit([ruta], f"memoria: nota {nid} ({tipo}, origen={origen})") if commitear else {
            "ok": False, "hash": None, "motivo": "commitear=False"}
        return res

    # ── leer ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _parsear(ruta: Path) -> list[dict]:
        if not ruta.exists():
            return []
        notas: list[dict] = []
        lineas = ruta.read_text(encoding="utf-8", errors="replace").splitlines()
        actual: dict | None = None
        for i, l in enumerate(lineas, 1):
            m = _RE_CABECERA.match(l)
            if m:
                if actual:
                    actual["texto"] = "\n".join(actual["texto"]).strip()
                    notas.append(actual)
                d = m.groupdict()
                actual = {"id": d["id"], "fecha": d["fecha"], "tipo": d["tipo"],
                          "origen": d["origen"], "tags": [t for t in d["tags"].split(",") if t],
                          "linea": i, "ruta": str(ruta), "texto": [], "invalidada": None}
            elif actual is not None:
                if l.startswith("_INVALIDADA") and not actual["texto"]:
                    actual["invalidada"] = l.strip("_")
                else:
                    actual["texto"].append(l)
        if actual:
            actual["texto"] = "\n".join(actual["texto"]).strip()
            notas.append(actual)
        return notas

    def invalidar(self, id_nota: str, motivo: str, *, por: str = "ser") -> dict:
        """Bi-temporal, como el `recuerda` de operador: una memoria falsa NO se borra, se invalida
        con fecha y motivo. Borrarla destruiría la prueba de que estuvo ahí y de quién la metió."""
        self._exigir_origen_confiable(por, "invalidar")
        for f in sorted(self.diario.glob("*.md")):
            texto = f.read_text(encoding="utf-8")
            lineas = texto.splitlines()
            for i, l in enumerate(lineas):
                m = _RE_CABECERA.match(l)
                if not m or m.group("id") != id_nota:
                    continue
                if i + 1 < len(lineas) and lineas[i + 1].startswith("_INVALIDADA"):
                    return {"ok": False, "motivo": "ya estaba invalidada", "id": id_nota}
                sello = (f"_INVALIDADA {datetime.now():%Y-%m-%d %H:%M:%S} · por={por} · "
                         f"motivo: {' '.join(motivo.split())}_")
                lineas.insert(i + 1, sello)
                with self._lock("diario"):
                    _escribir_atomico(f, "\n".join(lineas) + "\n")
                tocados = [f]
                if self.memoria_md.exists():           # si ya se había curado, se marca allí también
                    md = self.memoria_md.read_text(encoding="utf-8")
                    if f"[{id_nota}]" in md:
                        md = "\n".join(
                            (ln + f"  ⛔ INVALIDADA {datetime.now():%Y-%m-%d} · {motivo}")
                            if f"[{id_nota}]" in ln and "⛔" not in ln else ln
                            for ln in md.splitlines()) + "\n"
                        with self._lock("memoria_md"):
                            _escribir_atomico(self.memoria_md, md)
                        tocados.append(self.memoria_md)
                _anexar_jsonl(self.registro, {"evento": "invalidada", "nota": id_nota, "por": por,
                                              "motivo": motivo,
                                              "cuando": f"{datetime.now():%Y-%m-%d %H:%M:%S}"})
                tocados.append(self.registro)
                commit = self.commit(tocados, f"memoria: INVALIDADA {id_nota} (por={por}): {motivo}")
                return {"ok": True, "id": id_nota, "ruta": str(f), "linea": i + 1,
                        "motivo": motivo, "por": por, "commit": commit}
        return {"ok": False, "motivo": f"no encuentro la nota {id_nota}", "id": id_nota}

    def nota_en(self, ruta: str | Path, linea: int) -> dict | None:
        """¿A qué nota pertenece esta línea? Lo usa la fachada para que un resultado que
        llega por el ÍNDICE lleve la misma procedencia que uno que llega por grep:
        id, fecha, origen y —clave— si la nota está INVALIDADA. Sin esto, una memoria
        retirada volvía a colarse como verdad por la puerta de FTS5."""
        p = Path(ruta)
        if not p.is_absolute():
            p = self.raiz / p
        clave = str(p)
        cache = getattr(self, "_cache_notas", None)
        if cache is None:
            cache = self._cache_notas = {}
        st = p.stat().st_mtime_ns if p.exists() else 0
        if cache.get(clave, (None, None))[0] != st:
            cache[clave] = (st, self._parsear(p))
        duena = None
        for n in cache[clave][1]:
            if n["linea"] <= linea and (duena is None or n["linea"] > duena["linea"]):
                duena = n
        return duena

    def leer_hoy(self, dia: date | None = None) -> list[dict]:
        return self._parsear(self.ruta_diario(dia))

    def leer_todo(self) -> list[dict]:
        notas: list[dict] = []
        for f in sorted(self.diario.glob("*.md")):
            notas.extend(self._parsear(f))
        return notas

    # ── búsqueda de texto (ripgrep por debajo, con cascada honesta) ───────────
    def _detectar_motor(self) -> tuple[str, list[str], str | None]:
        """Usa un ejecutable rg o grep; no busca ejecutables privados del entorno."""
        if self._motor_texto:
            return self._motor_texto
        real = shutil.which("rg")
        if real:
            self._motor_texto = ("rg", [real], None)
            return self._motor_texto
        g = shutil.which("grep") or "/usr/bin/grep"
        self._motor_texto = ("grep", [g], None)
        return self._motor_texto

    def buscar_texto(self, consulta: str, rutas: list[str | Path] | None = None,
                     limite: int = 40) -> list[dict]:
        """grep vivo sobre los ficheros de memoria (no sobre un índice que puede estar rancio).
        Si la línea cae dentro de una nota, se devuelve la nota entera con su procedencia."""
        objetivos = [str(p) for p in (rutas or [self.memoria_md, self.diario]) if Path(p).exists()]
        if not objetivos or not consulta.strip():
            return []
        nombre, base, ejec = self._detectar_motor()
        if nombre.startswith("rg"):
            cmd = base + ["--json", "-n", "-i", "--fixed-strings", "--", consulta, *objetivos]
        else:
            cmd = base + ["-rniIF", "--", consulta, *objetivos]
        try:
            p = subprocess.run(cmd, executable=ejec, capture_output=True, text=True, timeout=30)
        except Exception as e:  # el buscador no puede tumbar la memoria
            return [{"error": f"{type(e).__name__}: {e}", "motor": nombre}]
        crudos: list[tuple[str, int, str]] = []
        if nombre.startswith("rg"):
            for l in p.stdout.splitlines():
                try:
                    d = json.loads(l)
                except Exception:
                    continue
                if d.get("type") == "match":
                    crudos.append((d["data"]["path"]["text"], d["data"]["line_number"],
                                   d["data"]["lines"]["text"].rstrip("\n")))
        else:
            for l in p.stdout.splitlines():
                partes = l.split(":", 2)
                if len(partes) == 3 and partes[1].isdigit():
                    crudos.append((partes[0], int(partes[1]), partes[2]))
        cache: dict[str, list[dict]] = {}
        salida: list[dict] = []
        for ruta, nlinea, texto in crudos[:limite]:
            notas = cache.setdefault(ruta, self._parsear(Path(ruta)))
            duena = None
            for n in notas:
                if n["linea"] <= nlinea and (duena is None or n["linea"] > duena["linea"]):
                    duena = n
            rel = os.path.relpath(ruta, self.raiz)
            item = {"texto": texto.strip(), "fuente": "notas", "motor": nombre,
                    "cita": f"{rel}:{nlinea}",
                    "procedencia": {"ruta": rel, "linea": nlinea,
                                    "id_nota": duena["id"] if duena else None,
                                    "fecha": duena["fecha"] if duena else None,
                                    "origen": duena["origen"] if duena else None,
                                    "tipo": duena["tipo"] if duena else None,
                                    "invalidada": duena.get("invalidada") if duena else None}}
            if duena:
                item["nota"] = duena["texto"]
            salida.append(item)
        return salida

    # ── el canal del modelo: proponer / revisar / aceptar / rechazar ─────────
    def proponer(self, texto: str, origen: str, *, tipo: str = "observacion",
                 tags: list[str] | tuple[str, ...] = (), cita: str | None = None) -> dict:
        """El modelo escribe AQUÍ, y solo aquí. Nada de esto es memoria todavía."""
        texto = texto.strip()
        if not texto:
            raise ValueError("propuesta vacía")
        ahora = datetime.now()
        pid = "p-" + self._nuevo_id(texto, ahora)[2:]
        prop = {"id": pid, "texto": texto, "tipo": tipo, "origen": origen,
                "tags": [t.strip().lstrip("#") for t in tags if str(t).strip()],
                "cita": cita, "propuesta_en": f"{ahora:%Y-%m-%d %H:%M:%S}", "estado": "pendiente"}
        veredicto = self.revisar(prop)
        prop["veredicto_regla"] = veredicto
        with self._lock("propuestas"):
            _escribir_atomico(self.pendientes / f"{pid}.json",
                              json.dumps(prop, ensure_ascii=False, indent=2) + "\n")
            _anexar_jsonl(self.registro, {"evento": "propuesta", "cuando": prop["propuesta_en"],
                                          **{k: prop[k] for k in ("id", "origen", "tipo", "texto", "cita")},
                                          "veredicto_regla": veredicto})
        return prop

    def revisar(self, prop: dict) -> dict:
        """La REGLA DETERMINISTA. Sin modelo, sin juicio: comprobaciones o nada.
        Solo puede decir SÍ a lo más inocuo; todo lo demás sube a operador."""
        señales: list[dict] = []
        texto = prop.get("texto", "")

        def s(nombre: str, ok: bool, detalle: str = "") -> bool:
            señales.append({"señal": nombre, "ok": bool(ok), "detalle": detalle})
            return bool(ok)

        ok = s("longitud", 8 <= len(texto) <= 2000, f"{len(texto)} caracteres (8..2000)")
        malos = [m for pat, m in PATRONES_PELIGROSOS if re.search(pat, texto, re.I)]
        ok &= s("sin patrones peligrosos", not malos, ", ".join(malos))
        ok &= s("tipo auto-aceptable", prop.get("tipo") in TIPOS_AUTOACEPTABLES,
                f"tipo={prop.get('tipo')} · auto: {sorted(TIPOS_AUTOACEPTABLES)}")
        cita = prop.get("cita")
        if not cita:
            ok &= s("cita verificable", False, "sin cita: un hecho sin procedencia no entra solo")
        else:
            ok &= s("cita verificable", *self._verificar_cita(cita))
        norm = " ".join(texto.lower().split())
        dup = next((n for n in self.leer_todo() if " ".join(n["texto"].lower().split()) == norm), None)
        ok &= s("no duplicada", dup is None, f"ya existe como {dup['id']}" if dup else "")
        return {"ok": bool(ok), "señales": señales,
                "motivo": "" if ok else "; ".join(x["señal"] for x in señales if not x["ok"])}

    def _verificar_cita(self, cita: str) -> tuple[bool, str]:
        """`ruta:linea` o `ruta`. La ruta tiene que existir DENTRO del proyecto."""
        ruta, _, nl = cita.partition(":")
        p = (self.raiz / ruta) if not os.path.isabs(ruta) else Path(ruta)
        try:
            p = p.resolve()
            p.relative_to(self.raiz)
        except Exception:
            return False, f"'{ruta}' cae fuera de {self.raiz}"
        if not p.is_file():
            return False, f"'{ruta}' no existe"
        if nl.isdigit():
            try:
                with p.open("rb") as source:
                    n = sum(1 for _ in source)
            except Exception as e:
                return False, str(e)
            if int(nl) > n:
                return False, f"{ruta} tiene {n} líneas, la cita pide la {nl}"
        return True, f"{cita} existe"

    def listar_propuestas(self) -> list[dict]:
        out = []
        for f in sorted(self.pendientes.glob("p-*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception as e:
                out.append({"id": f.stem, "error": str(e)})
        return out

    def aceptar(self, pid: str, *, por: str = "ser") -> dict:
        """Mueve una propuesta a la memoria DE VERDAD y hace commit.
        `por='regla'` solo pasa si la regla determinista dice OK. `por='ser'` manda."""
        f = self.pendientes / f"{pid}.json"
        if not f.exists():
            return {"ok": False, "motivo": f"no hay propuesta pendiente {pid}"}
        prop = json.loads(f.read_text(encoding="utf-8"))
        familia = self._familia(por)
        if familia not in {"ser", "humano", "regla"}:
            return {"ok": False, "motivo": f"'{por}' no puede aceptar (solo operador o una regla)"}
        if familia == "regla":
            v = self.revisar(prop)
            if not v["ok"]:
                return {"ok": False, "motivo": f"la regla la rechaza: {v['motivo']}", "veredicto": v}
        texto = prop["texto"]
        if prop.get("cita"):
            texto += f"\n_cita: {prop['cita']}_"
        texto += f"\n_propuesta por: {prop['origen']} el {prop['propuesta_en']}_"
        nota = self.apuntar(texto, prop.get("tipo", "observacion"), prop.get("tags", []),
                            origen=("ser" if familia in {"ser", "humano"} else "regla"),
                            propuesta_id=pid, aceptada_por=por, commitear=False)
        with self._lock("propuestas"):
            f.unlink(missing_ok=True)
            _anexar_jsonl(self.registro, {
                "evento": "aceptada", "cuando": nota["fecha"], "id": pid, "por": por,
                "nota": nota["id"], "cita": nota["cita"], "origen_propuesta": prop["origen"]})
        nota["commit"] = self.commit(
            [Path(nota["ruta"]), self.registro],
            f"memoria: aceptada {pid} -> {nota['id']} (por={por}, propuso={prop['origen']})")
        nota["propuesta"] = prop
        return {"ok": True, **nota}

    def rechazar(self, pid: str, motivo: str, *, por: str = "ser") -> dict:
        f = self.pendientes / f"{pid}.json"
        if not f.exists():
            return {"ok": False, "motivo": f"no hay propuesta pendiente {pid}"}
        prop = json.loads(f.read_text(encoding="utf-8"))
        prop["estado"] = "rechazada"
        prop["motivo_rechazo"] = motivo
        prop["rechazada_por"] = por
        with self._lock("propuestas"):
            _escribir_atomico(self.rechazadas / f"{pid}.json",
                              json.dumps(prop, ensure_ascii=False, indent=2) + "\n")
            f.unlink(missing_ok=True)
            _anexar_jsonl(self.registro, {"evento": "rechazada", "id": pid, "por": por,
                                          "motivo": motivo,
                                          "cuando": f"{datetime.now():%Y-%m-%d %H:%M:%S}"})
        return {"ok": True, "id": pid, "motivo": motivo}

    # ── curar: ascender del diario a MEMORIA.md ──────────────────────────────
    def _leer_memoria_md(self) -> tuple[str, set[str]]:
        if not self.memoria_md.exists():
            cab = ("# MEMORIA DE COLMENA (curada)\n"
                   "> Lo que perdura. Cada línea dice de dónde salió: `[id] (fecha, origen, diario)`.\n"
                   "> Aquí no escribe ningún modelo: llega por `curar()` desde el diario, y `curar()`\n"
                   "> solo asciende notas de origen confiable. `git log -p memoria/MEMORIA.md` es la verdad.\n")
            return cab, set()
        txt = self.memoria_md.read_text(encoding="utf-8")
        return txt, set(re.findall(r"\[(n-\d{8}-\d{6}-[0-9a-f]{4})\]", txt))

    def curar(self, *, simular: bool = False, tipos: frozenset[str] = TIPOS_PERDURABLES) -> dict:
        """Determinista: asciende a MEMORIA.md las notas perdurables de origen confiable
        que aún no estén, conservando su procedencia. No resume, no reescribe, no interpreta."""
        txt, ya = self._leer_memoria_md()
        ascendidas, omitidas = [], []
        for n in self.leer_todo():
            if n["id"] in ya:
                continue
            if n["tipo"] not in tipos:
                omitidas.append({"id": n["id"], "motivo": f"tipo={n['tipo']} no perdurable"})
                continue
            if self._familia(n["origen"]) not in ORIGENES_CONFIABLES:
                omitidas.append({"id": n["id"], "motivo": f"origen={n['origen']} no confiable"})
                continue
            if n.get("invalidada"):
                omitidas.append({"id": n["id"], "motivo": "invalidada"})
                continue
            ascendidas.append(n)
        if not ascendidas or simular:
            return {"ascendidas": [a["id"] for a in ascendidas], "n_ascendidas": len(ascendidas),
                    "ya_estaban": len(ya), "omitidas": omitidas, "simulado": simular,
                    "commit": {"ok": False, "hash": None, "motivo": "simulacion" if simular else "nada que ascender"}}
        bloques: dict[str, list[str]] = {}
        for n in ascendidas:
            rel = os.path.relpath(n["ruta"], self.raiz)
            una = " ".join(n["texto"].split("\n_")[0].split())
            bloques.setdefault(n["tipo"], []).append(
                f"- [{n['id']}] ({n['fecha']}, origen={n['origen']}, {rel}:{n['linea']}) {una}")
        with self._lock("memoria_md"):
            txt, _ = self._leer_memoria_md()
            nuevo = txt.rstrip("\n")
            for tipo, lineas in sorted(bloques.items()):
                cab = f"\n\n## {tipo}\n"
                if cab.strip() in nuevo:
                    idx = nuevo.index(f"## {tipo}\n") + len(f"## {tipo}\n")
                    fin = nuevo.find("\n## ", idx)
                    fin = len(nuevo) if fin == -1 else fin
                    nuevo = nuevo[:fin].rstrip("\n") + "\n" + "\n".join(lineas) + nuevo[fin:]
                else:
                    nuevo += cab + "\n".join(lineas)
            _escribir_atomico(self.memoria_md, nuevo.rstrip("\n") + "\n")
        commit = self.commit([self.memoria_md],
                             f"memoria: curar asciende {len(ascendidas)} nota(s) a MEMORIA.md")
        return {"ascendidas": [a["id"] for a in ascendidas], "n_ascendidas": len(ascendidas),
                "ya_estaban": len(ya), "omitidas": omitidas, "simulado": False, "commit": commit}

    def estado(self) -> dict:
        notas = self.leer_todo()
        _, ya = self._leer_memoria_md()
        return {"notas": len(notas),
                "invalidadas": sum(1 for n in notas if n.get("invalidada")),
                "dias": len(list(self.diario.glob("*.md"))),
                "curadas": len(ya), "pendientes": len(list(self.pendientes.glob("p-*.json"))),
                "rechazadas": len(list(self.rechazadas.glob("p-*.json"))),
                "motor_texto": self._detectar_motor()[0], "repo_git": self.es_repo(),
                "por_origen": {o: sum(1 for n in notas if n["origen"] == o)
                               for o in sorted({n["origen"] for n in notas})}}

    def historial(self, id_nota: str) -> list[dict]:
        """El rastro en git de una nota: qué commit la metió, cuándo y con qué mensaje.
        Es lo que convierte «esta memoria es falsa» en «este commit la metió, se revierte»."""
        if not self.es_repo():
            return []
        p = self._git("log", "--format=%h\x1f%an\x1f%ad\x1f%s", "--date=iso", "-S", id_nota,
                      "--", "memoria")
        out = []
        for l in p.stdout.splitlines():
            partes = l.split("\x1f")
            if len(partes) == 4:
                out.append({"commit": partes[0], "autor": partes[1], "fecha": partes[2],
                            "mensaje": partes[3]})
        return out
