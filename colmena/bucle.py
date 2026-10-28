"""Orquestador histórico de COLMENA, desactivado en la edición pública.

El lector de roles sigue siendo utilizable. Construir Colmena falla antes de
leer configuración, abrir bases de datos o contactar servicios. Se conserva
el diseño original como referencia; no es un agente autónomo operativo.
"""
from __future__ import annotations

import json, os, re, sys, time, tomllib
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from nucleo import motor as _motor          # noqa: E402
from nucleo import cola as _cola            # noqa: E402
from nucleo import barreras as _barreras    # noqa: E402
from nucleo import verificar as _verificar  # noqa: E402
try:
    from memoria import Memoria as _Memoria     # noqa: E402
except Exception:                                # la memoria nunca tumba el bucle
    _Memoria = None


# ─────────────────────────────────────────────────────────────────────────────
# Roles
# ─────────────────────────────────────────────────────────────────────────────
class Rol:
    """Un rol = un fichero agentes/<nombre>.md. El prefijo NUNCA cambia entre llamadas."""

    def __init__(self, ruta: Path):
        txt = ruta.read_text(encoding="utf-8")
        self.nombre = ruta.stem
        self.meta: dict = {}
        cuerpo = txt
        if txt.startswith("---"):
            fin = txt.find("\n---", 3)
            if fin > 0:
                for linea in txt[3:fin].strip().splitlines():
                    if ":" in linea:
                        k, v = linea.split(":", 1)
                        self.meta[k.strip()] = v.strip()
                cuerpo = txt[fin + 4:]
        # El prefijo es todo lo anterior a la marca de entrada. Es SAGRADO: no cambia.
        self.prefijo = cuerpo.split("--- ENTRADA ---")[0].strip()
        self.modelo = self.meta.get("modelo", "rapido")
        self.slot = int(self.meta.get("slot", 0) or 0)
        self.esquema_nombre = self.meta.get("esquema")

    def esquema(self) -> dict | None:
        if not self.esquema_nombre:
            return None
        p = RAIZ / "agentes" / "esquemas" / self.esquema_nombre
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    def mensajes(self, entrada: str) -> list[dict]:
        return [{"role": "system", "content": self.prefijo},
                {"role": "user", "content": entrada}]


def cargar_roles() -> dict[str, Rol]:
    d = RAIZ / "agentes"
    return {p.stem: Rol(p) for p in sorted(d.glob("*.md")) if not p.stem.startswith("_")}


# ─────────────────────────────────────────────────────────────────────────────
# La colmena
# ─────────────────────────────────────────────────────────────────────────────
class Colmena:
    def __init__(self, cfg_path: str | None = None):
        raise RuntimeError("Snapshot histórico: ejecución autónoma desactivada; consulte docs/KNOWN_ISSUES.md")
        # Referencia histórica: código original no alcanzable desde la API pública.
        p = Path(cfg_path or RAIZ / "config" / "colmena.toml")
        self.cfg = tomllib.load(open(p, "rb"))
        self.motor = _motor.Motor(self.cfg)
        db = Path(self.cfg["rutas"]["estado"]) / "colmena.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        self.cola = _cola.Cola(str(db))
        self.roles = cargar_roles()
        # Con -np 1 (lo que usa operador) solo existe el slot 0: fijar slots da 400.
        # El aislamiento entre agentes lo da -cram + --cache-idle-slots, no los slots.
        try:
            self.memoria = _Memoria(RAIZ) if _Memoria else None
        except Exception:
            self.memoria = None
        self.log = Path(self.cfg["rutas"]["logs"]) / "bucle.log"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        # Contadores del crítico: si dispara 0 veces, HAY QUE DECIRLO.
        self.critico_llamado = 0
        self.critico_encontro = 0

    # ── utilidades ───────────────────────────────────────────────────────────
    def _mod(self, logico: str) -> str:
        """nombre lógico ('cerebro') -> id real en llama-swap ('qwen')."""
        return self.cfg["modelos"].get(logico, "") or logico

    def _apuntar(self, txt: str) -> None:
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%F %T')} {txt}\n")

    def _familias_distintas(self, a: str, b: str) -> bool:
        """¿El crítico es de otra familia que el autor? Es LO que hace que criticar sirva."""
        def fam(m: str) -> str:
            m = (m or "").lower()
            for f in ("qwen", "llama", "hermes", "gpt-oss", "gptoss", "gemma", "mistral",
                      "deepseek", "glm", "phi", "lfm"):
                if f in m:
                    return "gpt-oss" if f in ("gpt-oss", "gptoss") else f
            return m
        return fam(a) != fam(b)

    # ── barreras ─────────────────────────────────────────────────────────────
    def _puedo_seguir(self) -> tuple[bool, str]:
        if (RAIZ / "PARAR").exists():
            return False, "existe el fichero PARAR (botón rojo de operador)"
        for g in (_barreras.guardian_bateria, _barreras.guardian_recursos):
            ok, msg = g()
            if not ok:
                return False, msg
        return True, "ok"

    # ── planificar ───────────────────────────────────────────────────────────
    def objetivo(self, texto: str, *, prioridad: int = 5) -> int:
        """Un objetivo de operador entra, se parte en tareas, y quedan en la cola."""
        oid = self.cola.nuevo_objetivo(texto, prioridad=prioridad)
        rol = self.roles["planificador"]
        r = self.motor.chat(self._mod(rol.modelo), rol.mensajes(texto),
                            esquema=rol.esquema(), agente=rol.nombre)
        plan = self._json(r)
        if plan.get("pregunta_a_ser"):
            self._preguntar(plan["pregunta_a_ser"], oid)
        mapa: dict[int, int] = {}
        for t in plan.get("tareas", []):
            deps = [mapa[d] for d in (t.get("depende_de") or []) if d in mapa]
            tid = self.cola.añadir_tarea(
                oid, t["descripcion"], modelo=t.get("modelo", "rapido"),
                depende_de=deps,
                meta={"como_se_comprueba": t.get("como_se_comprueba", "")})
            mapa[t.get("id", len(mapa) + 1)] = tid
        self._apuntar(f"objetivo {oid}: {len(mapa)} tareas · {texto[:70]}")
        return oid

    @staticmethod
    def _json(r: dict) -> dict:
        """El esquema fuerza JSON válido; esto es solo el cinturón por si acaso."""
        t = (r.get("texto") or "").strip()
        try:
            return json.loads(t)
        except Exception:
            m = re.search(r"\{.*\}", t, re.S)
            return json.loads(m.group(0)) if m else {}

    def _preguntar(self, pregunta: str, oid: int | None = None) -> None:
        """No se despierta a operador a las 4 de la mañana. Se le deja escrito."""
        f = Path(self.cfg["rutas"]["estado"]) / "PREGUNTAS.md"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"\n## {time.strftime('%F %T')}"
                     + (f" · objetivo {oid}" if oid else "") + f"\n{pregunta}\n")
        self._apuntar(f"PREGUNTA a operador: {pregunta[:90]}")

    # ── un paso completo ─────────────────────────────────────────────────────
    def paso(self) -> dict | None:
        ok, motivo = self._puedo_seguir()
        if not ok:
            self._apuntar(f"parado: {motivo}")
            return {"parado": motivo}

        cargado = self.motor.modelo_cargado()
        # El planificador etiqueta con nombres LÓGICOS; la cola razona con ellos.
        logico_cargado = next((k for k, v in self.cfg["modelos"].items()
                               if v and v == cargado), None)
        tarea = self.cola.siguiente(logico_cargado, worker="bucle")
        if not tarea:
            return None

        tid = tarea["id"]
        self._apuntar(f"tarea {tid} [{tarea.get('modelo')}] {tarea['descripcion'][:70]}")
        # EL CABLE que faltaba: sin esto, presupuesto() no ve nada y los topes de pasos,
        # de segundos y el detector de bucles son decorativos.
        _barreras.iniciar_tarea(tid)

        # 1 · HACER
        ok_p, msg_p = _barreras.registrar_paso(tid, self.cola.huella(tid, "hacer",
                                                                     tarea["descripcion"]))
        if not ok_p:
            self.cola.marcar(tid, "bloqueada", {"presupuesto": msg_p})
            self._preguntar(f"La tarea {tid} agotó su presupuesto: {msg_p}", None)
            return {"tarea": tid, "estado": "bloqueada", "porque": msg_p}
        rol = self.roles["obrero"]
        modelo_autor = self._mod(tarea.get("modelo") or rol.modelo)
        entrada = tarea["descripcion"] + self._lo_que_ya_sabemos(tarea["descripcion"])
        r = self.motor.chat(modelo_autor, rol.mensajes(entrada), agente=rol.nombre)
        artefacto = {"tipo": "texto", "cwd": str(RAIZ),
                     "rutas": self._rutas_citadas(r.get("texto", "")),
                     "salida": r.get("texto", "")}
        self.cola.registrar(tid, "hecho", {"ms": r.get("ms"), "tokens": r.get("tokens_out")})

        # 2 · VERIFICAR — las señales objetivas mandan
        v = _verificar.verificar(artefacto)
        self.cola.registrar(tid, "verificado", {"ok": v["ok"], "bloqueante": v.get("bloqueante")})

        # 3 · REPARAR si falla algo objetivo (máximo 2: a la tercera se para y se pregunta)
        intentos = 0
        maximo = int(self.cfg.get("ciclo", {}).get("reparaciones_max", 2))
        while not v["ok"] and intentos < maximo:
            intentos += 1
            ok_p, msg_p = _barreras.registrar_paso(tid, self.cola.huella(tid, "reparar", intentos))
            if not ok_p:
                self._apuntar(f"tarea {tid}: presupuesto agotado reparando ({msg_p})")
                break
            rep = self.roles["reparador"]
            # El reparador recibe el fallo sin atribuirle la autoría del artefacto.
            entrada = _verificar.resumen_para_modelo(v)
            rr = self.motor.chat(self._mod(rep.modelo), rep.mensajes(entrada),
                                 agente=rep.nombre)
            artefacto["salida"] = rr.get("texto", "")
            v = _verificar.verificar(artefacto)
            self.cola.registrar(tid, f"reparacion_{intentos}", {"ok": v["ok"]})
        if not v["ok"]:
            self.cola.marcar(tid, "fallida", {"bloqueante": v.get("bloqueante")})
            self._preguntar(
                f"La tarea {tid} («{tarea['descripcion'][:80]}») falló {maximo} veces seguidas.\n"
                f"Bloqueante: {v.get('bloqueante')}\n"
                f"Dos fallos seguidos = parar y replantear. No hago un tercer intento a ciegas.")
            return {"tarea": tid, "estado": "fallida"}

        # 4 · CRITICAR solo lo que ninguna señal objetiva cubre
        criticas = []
        if self.cfg.get("ciclo", {}).get("critico_activo", True):
            crit = self.roles["critico"]
            modelo_critico = self._mod(crit.modelo)
            if not self._familias_distintas(modelo_autor, modelo_critico):
                # Esto NO se calla: es la diferencia entre revisar y fingir que se revisa.
                aviso = (f"EL CRÍTICO ES DE LA MISMA FAMILIA QUE EL AUTOR "
                         f"({modelo_autor} vs {modelo_critico}). Revise la independencia "
                         f"de la evaluación antes de aceptar su conclusión.")
                self.cola.registrar(tid, "aviso_critico", {"motivo": aviso})
                self._apuntar("AVISO · " + aviso)
            self.critico_llamado += 1
            cr = self.motor.chat(modelo_critico,
                                 crit.mensajes(self._anonimo(artefacto)),
                                 esquema=crit.esquema(), agente=crit.nombre)
            c = self._json(cr)
            # Una crítica sin evidencia que la confirme NO es una crítica: se descarta.
            criticas = [h for h in c.get("hallazgos", [])
                        if (h.get("evidencia_que_lo_confirmaria") or "").strip()]
            if criticas:
                self.critico_encontro += 1
            self.cola.registrar(tid, "critica", {"n": len(criticas),
                                                 "descartadas": len(c.get("descartadas", []))})

        # 5 · JUEZ
        juez = self.roles["juez"]
        entrada = json.dumps({"objetivo": tarea["descripcion"],
                              "senales": [{k: s[k] for k in ("nombre", "ok", "omitida")}
                                          for s in v["señales"]],
                              "criticas": criticas}, ensure_ascii=False, indent=1)
        jr = self.motor.chat(self._mod(juez.modelo), juez.mensajes(entrada),
                             esquema=juez.esquema(), agente=juez.nombre)
        ver = self._json(jr)
        estado = "hecha" if ver.get("terminada") else "pendiente"
        self.cola.marcar(tid, estado, {"veredicto": ver, "reparaciones": intentos})
        self._aprender(tarea, v, criticas, intentos, estado)
        self._apuntar(f"tarea {tid} -> {estado} · {ver.get('porque', '')[:70]}")
        return {"tarea": tid, "estado": estado, "criticas": len(criticas)}

    def _lo_que_ya_sabemos(self, consulta: str) -> str:
        """Lo que la colmena ya sabe sobre esto, CON su procedencia.
        Sin procedencia no se puede comprobar, y una memoria envenenada sería indetectable."""
        if not self.memoria:
            return ""
        try:
            trozos = self.memoria.recordar(consulta, k=5)
        except Exception:
            return ""
        if not trozos:
            return ""
        lineas = []
        for t in trozos:
            proc = t.get("cita") or t.get("procedencia") or t.get("ruta") or "?"
            txt = (t.get("texto") or t.get("linea") or "").strip()[:220]
            if txt:
                lineas.append(f"  · {txt}   [{proc}]")
        if not lineas:
            return ""
        return ("\n\nLO QUE YA SABEMOS SOBRE ESTO (comprueba la procedencia antes de fiarte):\n"
                + "\n".join(lineas))

    def _aprender(self, tarea: dict, v: dict, criticas: list, intentos: int, estado: str) -> None:
        """Lo aprendido se propone para revisión antes de incorporarlo a la memoria."""
        if not self.memoria or estado != "hecha":
            return
        if intentos == 0 and not criticas:
            return          # salió a la primera y nadie objetó: no hay lección que guardar
        partes = [f"Tarea «{tarea['descripcion'][:90]}»: salió"]
        partes.append(f"tras {intentos} reparación(es)" if intentos else "a la primera")
        if v.get("bloqueante"):
            partes.append(f"; lo que la bloqueaba era «{v['bloqueante']}»")
        if criticas:
            partes.append(f"; el revisor señaló: {criticas[0].get('que','')[:110]}")
        try:
            self.memoria.proponer(" ".join(partes), origen="modelo:bucle", tipo="observacion")
        except Exception:
            pass

    @staticmethod
    def _rutas_citadas(texto: str) -> list[str]:
        """Rutas que el obrero dice haber tocado. Lo que verificar() irá a mirar."""
        return list(dict.fromkeys(re.findall(r"(?:^|\s)([\w./-]+\.(?:py|js|ts|sh|json|toml|md|yaml))",
                                             texto or "")))[:20]

    @staticmethod
    def _anonimo(artefacto: dict) -> str:
        """El crítico NUNCA sabe de quién es el trabajo ni ve el razonamiento del autor."""
        return ("Revisa este artefacto. Viene de fuera; no sabes quién lo escribió.\n\n"
                + (artefacto.get("salida") or "")[:12000])

    # ── el bucle largo ───────────────────────────────────────────────────────
    def correr(self, max_pasos: int = 0) -> dict:
        n = hechas = 0
        t0 = time.time()
        while True:
            if max_pasos and n >= max_pasos:
                break
            r = self.paso()
            if r is None:
                self._apuntar("cola vacía")
                break
            if r.get("parado"):
                break
            n += 1
            hechas += 1 if r.get("estado") == "hecha" else 0
        # El informe honesto del crítico
        if self.critico_llamado >= 50 and self.critico_encontro == 0:
            self._apuntar("EL CRÍTICO NO EXISTE: 0 hallazgos en "
                          f"{self.critico_llamado} revisiones. Revisar el prompt o el modelo.")
        return {"pasos": n, "hechas": hechas, "minutos": round((time.time() - t0) / 60, 1),
                "critico_llamado": self.critico_llamado,
                "critico_encontro": self.critico_encontro}
