"""Cliente HTTP/SSE para servicios locales compatibles con llama-swap.

Implementa chat, reintentos limitados, diagnóstico de streams interrumpidos,
salida estructurada y funciones de embeddings/reranking. Los identificadores
de modelos están vacíos en la edición pública. Ningún servidor se instala ni se
activa al importar el módulo. Esta fuente procede del snapshot histórico
2026-09-09; las pruebas públicas emplean exclusivamente un servidor simulado.
"""

from __future__ import annotations

import json
import logging
import os
import random
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Iterable

__all__ = [
    "Motor", "cargar_config", "CONFIG_DEF",
    "ErrorMotor", "ErrorPeticion", "RespuestaTruncada", "SinRespuesta", "RespuestaNoValida",
]

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_DEF: dict[str, Any] = {
    "motor": {
        "url": "http://127.0.0.1:9292",
        "timeout_s": 600,          # un modelo local puede tardar minutos: generoso a propósito
        "reintentos": 3,
        "backoff_s": 1.5,
        "conectar_timeout_s": 20,
    },
    "modelos": {
        "cerebro": "",
        "rapido": "",
        "critico": "",
        "router": "",
        "embed": "",
        "rerank": "",
    },
    "limites": {
        "vram_reservada_mib": 1024,
        "disco_minimo_gb": 80,
        "bateria_minima_pct": 40,
        "pasos_max_tarea": 40,
        "segundos_max_tarea": 3600,
    },
    "registro": {
        "ruta": None,  # el registro se activa explícitamente
    },
}

# Lo que jamás sale de aquí hacia el servidor. No es una preferencia: es la regla nº2 de
# `docs/contratos.md`.
PROHIBIDOS = ("max_tokens", "num_predict", "max_completion_tokens", "n_predict")

# Huellas de que llama-swap cortó la generación pero devolvió 200.
MARCAS_CORTE = (
    "recovered from upstream disconnection",
    "upstream disconnection during streaming",
    "upstream server disconnected",
    "unexpected EOF",
)

log = logging.getLogger("colmena.motor")


class ErrorMotor(Exception):
    """Base de los errores del motor."""


class ErrorPeticion(ErrorMotor):
    """4xx: la petición está mal. NO se reintenta (reintentar la repetiría igual de mal)."""

    def __init__(self, codigo: int, cuerpo: str) -> None:
        super().__init__(f"HTTP {codigo}: {cuerpo[:800]}")
        self.codigo = codigo
        self.cuerpo = cuerpo


class RespuestaTruncada(ErrorMotor):
    """200 con el stream cortado / respuesta vacía. SÍ se reintenta."""


class SinRespuesta(ErrorMotor):
    """Se agotaron los reintentos."""


class RespuestaNoValida(ErrorMotor):
    """El modelo devolvió algo que no cumple el esquema pedido."""


# --------------------------------------------------------------------------- config


def _fusion(base: dict, encima: dict) -> dict:
    out = dict(base)
    for k, v in (encima or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _fusion(out[k], v)
        else:
            out[k] = v
    return out


def cargar_config(ruta: str | None = None) -> dict:
    """Lee `config/colmena.toml` si existe y lo fusiona sobre los valores por defecto.

    No falla si el fichero no está todavía (otro agente lo escribe): devuelve los defaults.
    """
    ruta = ruta or os.path.join(RAIZ, "config", "colmena.toml")
    cfg = json.loads(json.dumps(CONFIG_DEF))  # copia profunda barata
    if os.path.exists(ruta):
        try:
            import tomllib
            with open(ruta, "rb") as fh:
                cfg = _fusion(cfg, tomllib.load(fh))
        except Exception as e:  # el sistema tiene que arrancar aunque el toml esté roto
            log.warning("no pude leer %s (%s); sigo con los valores por defecto", ruta, e)
    return cfg


# ---------------------------------------------------------------------------- motor


class Motor:
    """Cliente de llama-swap. Un objeto por proceso; es seguro compartirlo entre hilos
    salvo por el contador de slots (que solo sirve para avisar en el log)."""

    def __init__(self, cfg: dict | None = None) -> None:
        cfg = _fusion(CONFIG_DEF, cfg or {})
        self.cfg = cfg
        m = cfg["motor"]
        self.url = str(m["url"]).rstrip("/")
        self.timeout_s = float(m.get("timeout_s", 600))
        self.reintentos = int(m.get("reintentos", 3))
        self.backoff_s = float(m.get("backoff_s", 1.5))
        self.modelos = dict(cfg.get("modelos", {}))
        self.ruta_log = cfg.get("registro", {}).get("ruta")
        self._agente_de_slot: dict[int, str] = {}

    # ------------------------------------------------------------------ HTTP crudo

    def _url(self, ruta: str) -> str:
        return f"{self.url}/{ruta.lstrip('/')}"

    def _get(self, ruta: str, *, timeout: float | None = None) -> tuple[int, str]:
        req = urllib.request.Request(self._url(ruta), method="GET",
                                     headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or 15.0) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def _get_json(self, ruta: str, *, timeout: float | None = None) -> Any:
        code, cuerpo = self._get(ruta, timeout=timeout)
        if code >= 400:
            raise ErrorPeticion(code, cuerpo)
        try:
            return json.loads(cuerpo)
        except ValueError as e:
            raise ErrorMotor(f"{ruta} no devolvió JSON: {cuerpo[:300]}") from e

    def _post(self, ruta: str, cuerpo: dict, *, timeout: float | None = None,
              stream: bool = False):
        datos = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._url(ruta), data=datos, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream" if stream else "application/json"},
        )
        return urllib.request.urlopen(req, timeout=timeout or self.timeout_s)

    # --------------------------------------------------------------- construcción

    def _cuerpo_chat(self, modelo, mensajes, herramientas, gramatica, esquema, slot,
                     temperatura, stream, extra) -> dict:
        if gramatica and esquema:
            raise ValueError("gramatica y esquema son excluyentes: llama.cpp acepta uno u otro")
        for k in PROHIBIDOS:
            if extra and k in extra:
                raise ValueError(
                    f"{k} está PROHIBIDO en COLMENA (regla 2 de docs/contratos.md). "
                    "Sin topes de generación.")
        cuerpo: dict[str, Any] = {"model": modelo, "messages": list(mensajes), "stream": bool(stream)}
        if stream:
            # sin esto llama.cpp no manda el bloque `usage` en streaming y nos quedaríamos
            # sin tokens_in/tokens_out
            cuerpo["stream_options"] = {"include_usage": True}
        if herramientas:
            cuerpo["tools"] = herramientas
            cuerpo["tool_choice"] = (extra or {}).get("tool_choice", "auto")
        if temperatura is not None:
            cuerpo["temperature"] = float(temperatura)
        if slot is not None:
            cuerpo["id_slot"] = int(slot)
        if gramatica:
            cuerpo["grammar"] = gramatica
        if esquema:
            cuerpo["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": esquema.get("title", "salida"),
                                "schema": esquema, "strict": True},
            }
        for k, v in (extra or {}).items():
            if k != "tool_choice":
                cuerpo[k] = v
        return cuerpo

    # ------------------------------------------------------------------- lectura

    @staticmethod
    def _mezclar_tool_calls(acc: dict, trozos: Iterable[dict]) -> None:
        for tc in trozos or ():
            i = int(tc.get("index", len(acc)))
            d = acc.setdefault(i, {"id": None, "type": "function",
                                   "function": {"name": "", "arguments": ""}})
            if tc.get("id"):
                d["id"] = tc["id"]
            if tc.get("type"):
                d["type"] = tc["type"]
            f = tc.get("function") or {}
            if f.get("name"):
                d["function"]["name"] = f["name"]
            if f.get("arguments"):
                d["function"]["arguments"] += f["arguments"]

    def _leer_stream(self, resp) -> dict:
        texto: list[str] = []
        razona: list[str] = []
        tools: dict[int, dict] = {}
        usage: dict = {}
        fin: str | None = None
        visto_done = False
        crudo_cola: list[str] = []
        modelo_real = None
        for linea_b in resp:
            linea = linea_b.decode("utf-8", "replace").strip()
            if not linea:
                continue
            crudo_cola.append(linea)
            if len(crudo_cola) > 12:
                crudo_cola.pop(0)
            if not linea.startswith("data:"):
                continue  # comentarios SSE, cabeceras raras del proxy...
            dato = linea[5:].strip()
            if dato == "[DONE]":
                visto_done = True
                continue
            try:
                ch = json.loads(dato)
            except ValueError:
                continue
            if isinstance(ch, dict) and ch.get("error"):
                raise RespuestaTruncada(f"error en el stream: {json.dumps(ch['error'])[:400]}")
            modelo_real = ch.get("model") or modelo_real
            if ch.get("usage"):
                usage = ch["usage"]
            for c in ch.get("choices") or ():
                d = c.get("delta") or {}
                if d.get("content"):
                    texto.append(d["content"])
                if d.get("reasoning_content"):
                    razona.append(d["reasoning_content"])
                if d.get("tool_calls"):
                    self._mezclar_tool_calls(tools, d["tool_calls"])
                if c.get("finish_reason"):
                    fin = c["finish_reason"]
        return {
            "texto": "".join(texto),
            "razonamiento": "".join(razona),
            "tool_calls": [tools[k] for k in sorted(tools)],
            "usage": usage,
            "finish_reason": fin,
            "visto_done": visto_done,
            "cola": "\n".join(crudo_cola),
            "modelo": modelo_real,
        }

    @staticmethod
    def _leer_json(cuerpo: str) -> dict:
        try:
            j = json.loads(cuerpo)
        except ValueError as e:
            raise RespuestaTruncada(f"cuerpo no-JSON (¿stream cortado?): {cuerpo[:300]}") from e
        if j.get("error"):
            raise RespuestaTruncada(f"error en la respuesta: {json.dumps(j['error'])[:400]}")
        ch = (j.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        return {
            "texto": msg.get("content") or "",
            "razonamiento": msg.get("reasoning_content") or "",
            "tool_calls": msg.get("tool_calls") or [],
            "usage": j.get("usage") or {},
            "finish_reason": ch.get("finish_reason"),
            "visto_done": True,
            "cola": "",
            "modelo": j.get("model"),
        }

    @staticmethod
    def _diagnostico_corte(r: dict) -> str | None:
        """Devuelve el motivo si esta respuesta es un 200-con-stream-cortado, o None.

        Las tres heurísticas, por orden de fiabilidad (y ninguna sobra, ver la cabecera):
          1. el marcador de llama-swap, si por casualidad llegó al cuerpo;
          2. el stream acabó sin `[DONE]` y sin `finish_reason`  <- ESTE es el que saltó
             en el corte real medido el 9-sep;
          3. 200 impecable pero sin contenido ni tool_calls (el otro disfraz del unload).
        """
        blob = (r.get("cola") or "") + (r.get("texto") or "")
        bajo = blob.lower()
        for marca in MARCAS_CORTE:
            if marca in bajo:
                return f"marcador de llama-swap: {marca!r}"
        if not r.get("visto_done") and not r.get("finish_reason"):
            return "el stream terminó sin [DONE] y sin finish_reason"
        if not r.get("texto") and not r.get("tool_calls"):
            return f"respuesta vacía (finish_reason={r.get('finish_reason')!r})"
        return None

    # ---------------------------------------------------------------------- chat

    def chat(self, modelo: str, mensajes: list[dict], *,
             herramientas: list[dict] | None = None,
             gramatica: str | None = None,      # GBNF para salida estructurada
             esquema: dict | None = None,       # json_schema
             slot: int | None = None,           # 1 slot por agente = no rompe el prefix cache
             temperatura: float | None = None,
             # --- extras con valor por defecto: no cambian la firma del contrato ---
             agente: str | None = None,
             stream: bool = True,
             extra: dict | None = None,
             timeout_s: float | None = None) -> dict:
        """Devuelve {'texto':str,'tool_calls':list,'tokens_in':int,'tokens_out':int,'ms':int,'modelo':str}.
        Reintenta ante 5xx y ante 200-con-stream-cortado (llama-swap devuelve 200 en unloads).
        NUNCA pasa max_tokens/num_predict (regla de la casa).

        Claves extra en el dict devuelto: 'razonamiento', 'finish_reason', 'reintentos',
        'slot', 'agente', 'usage'.
        """
        if slot is not None:
            if int(slot) != 0:
                log.warning(
                    "slot=%s: con -np 1 el servidor solo tiene el slot 0 y devolverá 400. "
                    "Compruébalo con GET /upstream/<modelo>/slots antes de fijar slots.", slot)
            if agente:
                previo = self._agente_de_slot.get(int(slot))
                if previo and previo != agente:
                    log.warning(
                        "el slot %s pasa de '%s' a '%s': el prefijo del prompt cambia entero. "
                        "Un slot por agente y prefijos ESTABLES (lo variable, al final).",
                        slot, previo, agente)
                self._agente_de_slot[int(slot)] = agente

        cuerpo = self._cuerpo_chat(modelo, mensajes, herramientas, gramatica, esquema,
                                   slot, temperatura, stream, extra)
        t0 = time.monotonic()
        ultimo: Exception | None = None
        for intento in range(1, self.reintentos + 1):
            try:
                r = self._llamada_chat(cuerpo, stream, timeout_s)
                motivo = self._diagnostico_corte(r)
                if motivo:
                    raise RespuestaTruncada(motivo)
                ms = int((time.monotonic() - t0) * 1000)
                u = r.get("usage") or {}
                salida = {
                    "texto": r["texto"],
                    "razonamiento": r.get("razonamiento", ""),
                    "tool_calls": r["tool_calls"],
                    "tokens_in": int(u.get("prompt_tokens") or 0),
                    "tokens_out": int(u.get("completion_tokens") or 0),
                    "ms": ms,
                    # MEDIDO 9-sep: llama.cpp devuelve en `model` la RUTA del .gguf
                    # (por ejemplo '/models/example.gguf'), NO el id de llama-swap.
                    # Aquí se devuelve el id LÓGICO, que es con lo que compara el
                    # planificador contra modelo_cargado(); la ruta va aparte.
                    "modelo": modelo,
                    "modelo_servidor": r.get("modelo"),
                    "finish_reason": r.get("finish_reason"),
                    "reintentos": intento - 1,
                    "slot": slot,
                    "agente": agente,
                    "usage": u,
                }
                self._registrar({"op": "chat", "ok": True, "modelo": modelo,
                                 "servidor": salida["modelo_servidor"],
                                 "tokens_in": salida["tokens_in"],
                                 "tokens_out": salida["tokens_out"], "ms": ms,
                                 "reintentos": intento - 1, "slot": slot, "agente": agente,
                                 "gramatica": bool(gramatica), "esquema": bool(esquema),
                                 "herramientas": len(herramientas or []),
                                 "finish_reason": salida["finish_reason"],
                                 "tok_s": round(salida["tokens_out"] / max(ms / 1000, 1e-6), 2)})
                return salida
            except ErrorPeticion as e:
                # 4xx: reintentar da exactamente el mismo error. Se sale ya.
                self._registrar({"op": "chat", "ok": False, "pedido": modelo,
                                 "ms": int((time.monotonic() - t0) * 1000),
                                 "reintentos": intento - 1, "error": str(e)[:500],
                                 "clase": "peticion"})
                raise
            except (RespuestaTruncada, urllib.error.HTTPError, urllib.error.URLError,
                    socket.timeout, TimeoutError, ConnectionError, ErrorMotor) as e:
                ultimo = e
                espera = self.backoff_s * (2 ** (intento - 1)) * (0.8 + 0.4 * random.random())
                log.warning("chat(%s) intento %d/%d falló: %s — reintento en %.1f s",
                            modelo, intento, self.reintentos, e, espera)
                self._registrar({"op": "chat", "ok": False, "pedido": modelo,
                                 "intento": intento, "error": str(e)[:500],
                                 "clase": type(e).__name__, "espera_s": round(espera, 2)})
                if intento < self.reintentos:
                    time.sleep(espera)
        raise SinRespuesta(
            f"{self.reintentos} intentos contra {modelo} sin respuesta usable; último: {ultimo}"
        ) from ultimo

    def _llamada_chat(self, cuerpo: dict, stream: bool, timeout_s: float | None) -> dict:
        try:
            resp = self._post("/v1/chat/completions", cuerpo, timeout=timeout_s, stream=stream)
        except urllib.error.HTTPError as e:
            texto = e.read().decode("utf-8", "replace")
            if 400 <= e.code < 500 and e.code not in (408, 429):
                raise ErrorPeticion(e.code, texto) from None
            raise ErrorMotor(f"HTTP {e.code}: {texto[:400]}") from None
        with resp:
            if resp.status >= 400:
                raise ErrorMotor(f"HTTP {resp.status}")
            if stream:
                return self._leer_stream(resp)
            return self._leer_json(resp.read().decode("utf-8", "replace"))

    # ----------------------------------------------------------- salida estricta

    def json_estricto(self, modelo: str, mensajes: list[dict], esquema: dict, **kw) -> dict:
        """Chat con `json_schema` que devuelve un dict YA PARSEADO, o lanza excepción.

        Con gramática/esquema el formato no puede salir mal (el sampler no deja emitir un
        token que rompa el JSON). El try/except de abajo es el cinturón por si el servidor
        ignorase el `response_format`: entonces preferimos fallar ruidosamente a devolver
        texto suelto que otro módulo se creería.
        """
        r = self.chat(modelo, mensajes, esquema=esquema, **kw)
        crudo = (r.get("texto") or "").strip()
        try:
            return json.loads(crudo)
        except ValueError:
            # algunos modelos envuelven el JSON en ```json ... ```
            limpio = crudo.strip("`").strip()
            if limpio.lower().startswith("json"):
                limpio = limpio[4:].strip()
            i, j = limpio.find("{"), limpio.rfind("}")
            if i >= 0 and j > i:
                try:
                    return json.loads(limpio[i:j + 1])
                except ValueError:
                    pass
            raise RespuestaNoValida(
                f"{modelo} no devolvió JSON válido pese al esquema: {crudo[:400]!r}")

    # ------------------------------------------------------------ embed / rerank

    def embed(self, textos: list[str], modelo: str | None = None) -> list[list[float]]:
        modelo = modelo or self.modelos.get("embed", "embed")
        cuerpo = {"model": modelo, "input": list(textos)}
        t0 = time.monotonic()
        ultimo: Exception | None = None
        for intento in range(1, self.reintentos + 1):
            try:
                with self._post("/v1/embeddings", cuerpo) as resp:
                    j = json.loads(resp.read().decode("utf-8", "replace"))
                filas = sorted(j.get("data") or [], key=lambda d: d.get("index", 0))
                vec = [f["embedding"] for f in filas]
                if len(vec) != len(textos):
                    raise RespuestaTruncada(f"pedí {len(textos)} vectores y llegaron {len(vec)}")
                self._registrar({"op": "embed", "ok": True, "modelo": modelo, "n": len(textos),
                                 "ms": int((time.monotonic() - t0) * 1000),
                                 "reintentos": intento - 1,
                                 "dim": len(vec[0]) if vec else 0})
                return vec
            except urllib.error.HTTPError as e:
                texto = e.read().decode("utf-8", "replace")
                if 400 <= e.code < 500 and e.code not in (408, 429):
                    self._registrar({"op": "embed", "ok": False, "modelo": modelo,
                                     "error": texto[:300]})
                    raise ErrorPeticion(e.code, texto) from None
                ultimo = e
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                    ValueError, KeyError, RespuestaTruncada) as e:
                ultimo = e
            if intento < self.reintentos:
                time.sleep(self.backoff_s * (2 ** (intento - 1)))
        self._registrar({"op": "embed", "ok": False, "modelo": modelo, "error": str(ultimo)[:300]})
        raise SinRespuesta(f"embed contra {modelo} falló: {ultimo}") from ultimo

    def rerank(self, consulta: str, docs: list[str], modelo: str | None = None) -> list[float]:
        modelo = modelo or self.modelos.get("rerank", "rerank")
        cuerpo = {"model": modelo, "query": consulta, "documents": list(docs),
                  "top_n": len(docs)}
        t0 = time.monotonic()
        try:
            with self._post("/v1/rerank", cuerpo) as resp:
                j = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            texto = e.read().decode("utf-8", "replace")
            self._registrar({"op": "rerank", "ok": False, "modelo": modelo, "error": texto[:300]})
            raise ErrorPeticion(e.code, texto) from None
        puntos = [0.0] * len(docs)
        for r in j.get("results") or []:
            i = int(r.get("index", -1))
            if 0 <= i < len(docs):
                puntos[i] = float(r.get("relevance_score", r.get("score", 0.0)))
        self._registrar({"op": "rerank", "ok": True, "modelo": modelo, "n": len(docs),
                         "ms": int((time.monotonic() - t0) * 1000)})
        return puntos

    # --------------------------------------------------------------- el estado real

    def vivo(self) -> bool:
        try:
            code, _ = self._get("/health", timeout=5)
            return code == 200
        except Exception:
            return False

    def modelos_cargados(self) -> list[str]:
        """Todos los modelos que llama-swap tiene arriba ahora mismo."""
        try:
            j = self._get_json("/running", timeout=10)
        except Exception as e:
            log.warning("no pude leer /running: %s", e)
            return []
        filas = j.get("running") if isinstance(j, dict) else j
        salida = []
        for f in filas or ():
            if isinstance(f, str):
                salida.append(f)
                continue
            nombre = f.get("model") or f.get("id") or f.get("name")
            estado = (f.get("state") or f.get("status") or "").lower()
            if nombre and estado not in ("stopped", "stopping", "shutdown"):
                salida.append(nombre)
        return salida

    def modelo_cargado(self) -> str | None:
        """Qué modelo tiene la GPU AHORA (GET /running). Lo usa el planificador."""
        cargados = self.modelos_cargados()
        return cargados[0] if cargados else None

    def vram_libre_mib(self) -> int:
        """MiB libres en la GPU. Si NO se puede saber, devuelve 0 a propósito:
        el guardián de recursos debe bloquear cuando no hay medida, no seguir a ciegas."""
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return min(int(l.strip()) for l in out.stdout.strip().splitlines() if l.strip())
        except Exception as e:
            log.warning("nvidia-smi no contestó (%s); pruebo /metrics de llama-swap", e)
        try:  # plan B: las métricas prometheus del propio proxy
            code, cuerpo = self._get("/metrics", timeout=5)
            if code == 200:
                for linea in cuerpo.splitlines():
                    if linea.startswith("#"):
                        continue
                    if "gpu" in linea and ("free" in linea or "memory_free" in linea):
                        try:
                            v = float(linea.rsplit(" ", 1)[1])
                            return int(v / (1024 * 1024)) if v > 1 << 30 else int(v)
                        except (ValueError, IndexError):
                            continue
        except Exception:
            pass
        log.error("vram_libre_mib(): sin medida -> devuelvo 0 (fail-closed)")
        return 0

    def hay_peticion_en_vuelo(self, modelo: str) -> bool:
        """GET /upstream/<m>/slots -> is_processing. NUNCA descargar con esto en True:
        descargar durante una petición puede interrumpirla.

        Dos cautelas que no son opcionales:
          · Si el modelo NO está en /running, se devuelve False SIN tocar el upstream:
            pedir /upstream/<m>/... puede hacer que llama-swap cargue el modelo.
          · Si el modelo está cargado pero no se puede leer /slots, se devuelve True
            (fail-safe): ante la duda, no se descarga.
        """
        if modelo not in self.modelos_cargados():
            return False
        try:
            j = self._get_json(f"/upstream/{modelo}/slots", timeout=10)
        except Exception as e:
            log.warning("no pude leer los slots de %s (%s) -> asumo EN VUELO", modelo, e)
            self._registrar({"op": "slots", "modelo": modelo, "ok": False, "error": str(e)[:300],
                             "resultado": "asumo en vuelo"})
            return True
        filas = j if isinstance(j, list) else (j.get("slots") or [])
        ocupado = any(bool(s.get("is_processing")) for s in filas if isinstance(s, dict))
        self._registrar({"op": "slots", "modelo": modelo, "ok": True, "slots": len(filas),
                         "en_vuelo": ocupado})
        return ocupado

    def descargar(self, modelo: str | None = None, *, forzar: bool = False) -> tuple[bool, str]:
        """Descarga un modelo (o todos). Se NIEGA si hay una petición en vuelo.

        Consulta `hay_peticion_en_vuelo()` para evitar interrumpir una generación.
        """
        objetivos = [modelo] if modelo else self.modelos_cargados()
        if not objetivos:
            return True, "no había nada cargado"
        if not forzar:
            for m in objetivos:
                if self.hay_peticion_en_vuelo(m):
                    return False, f"{m} tiene una petición EN VUELO: no se descarga"
        ruta = f"/api/models/unload/{modelo}" if modelo else "/api/models/unload"
        try:
            with self._post(ruta, {}, timeout=120) as r:
                cuerpo = r.read().decode("utf-8", "replace")[:200]
                self._registrar({"op": "unload", "modelo": modelo or "*", "ok": True,
                                 "status": r.status, "forzado": forzar})
                return True, f"HTTP {r.status} {cuerpo}"
        except urllib.error.HTTPError as e:
            texto = e.read().decode("utf-8", "replace")[:300]
            self._registrar({"op": "unload", "modelo": modelo or "*", "ok": False,
                             "error": texto})
            return False, f"HTTP {e.code}: {texto}"
        except Exception as e:
            self._registrar({"op": "unload", "modelo": modelo or "*", "ok": False,
                             "error": str(e)[:300]})
            return False, str(e)

    # ------------------------------------------------------------------ registro

    def _registrar(self, fila: dict) -> None:
        """Una línea NDJSON por operación: modelo, tokens in/out, ms y si hubo reintento.
        Nunca revienta la llamada por un problema de log."""
        fila = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fila}
        log.info("%s", json.dumps(fila, ensure_ascii=False))
        if not self.ruta_log:
            return
        try:
            os.makedirs(os.path.dirname(self.ruta_log), exist_ok=True)
            with open(self.ruta_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(fila, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            log.debug("no pude escribir el log del motor: %s", e)
