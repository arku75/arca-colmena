#!/usr/bin/env python3
"""Pruebas HTTP/SSE aisladas contra un servidor simulado en 127.0.0.1.

No contactan modelos reales ni instalan servicios. El modo real histórico se
excluyó de la edición pública. Ejecutar: python3 pruebas/test_motor.py
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from nucleo.motor import (  # noqa: E402
    Motor, ErrorPeticion, RespuestaNoValida, SinRespuesta,
)

FALLOS: list[str] = []
PASOS = 0


def ok(cond: bool, titulo: str, detalle: str = "") -> None:
    global PASOS
    PASOS += 1
    print(f"  [{'OK  ' if cond else 'FALLO'}] {titulo}" + (f"  -> {detalle}" if detalle else ""))
    if not cond:
        FALLOS.append(titulo)


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


# ============================================================ MITAD 1: SERVIDOR FALSO

TROZO_OK = [
    'data: {"id":"1","model":"falso","choices":[{"index":0,"delta":{"role":"assistant"}}]}',
    'data: {"id":"1","model":"falso","choices":[{"index":0,"delta":{"content":"hola "}}]}',
    'data: {"id":"1","model":"falso","choices":[{"index":0,"delta":{"content":"mundo"}}]}',
    'data: {"id":"1","model":"falso","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
    'data: {"id":"1","model":"falso","choices":[],"usage":{"prompt_tokens":11,"completion_tokens":2}}',
    "data: [DONE]",
]

# Lo que de verdad manda llama-swap cuando algo descarga el modelo a mitad: HTTP 200 y el
# stream se corta con ese aviso. Un cliente ingenuo se lo cree y devuelve texto vacío.
TROZO_CORTADO = [
    'data: {"id":"1","model":"falso","choices":[{"index":0,"delta":{"content":"ho"}}]}',
    "data: {\"error\":{\"message\":\"recovered from upstream disconnection during streaming\"}}",
]


class Guion:
    """Lo que el servidor falso va a hacer, petición a petición."""

    def __init__(self, pasos):
        self.pasos = list(pasos)
        self.i = 0
        self.cuerpos = []
        self.lock = threading.Lock()

    def siguiente(self, cuerpo):
        with self.lock:
            self.cuerpos.append(cuerpo)
            p = self.pasos[min(self.i, len(self.pasos) - 1)]
            self.i += 1
            return p


GUION: Guion | None = None


class Falso(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silencio
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        cuerpo = json.loads(self.rfile.read(n) or b"{}")
        que = GUION.siguiente(cuerpo)
        if que == "ok":
            datos = ("\n".join(TROZO_OK) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)
        elif que == "cortado":
            datos = ("\n".join(TROZO_CORTADO) + "\n\n").encode()
            self.send_response(200)                      # ← 200, ese es el veneno
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)
        elif que == "vacio":
            datos = ('data: {"choices":[{"index":0,"delta":{"content":""},'
                     '"finish_reason":"stop"}]}\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)
        elif que == "503":
            self.send_response(503)
            self.send_header("Content-Length", "22")
            self.end_headers()
            self.wfile.write(b"upstream no disponible")
        elif que == "400":
            self.send_response(400)
            self.send_header("Content-Length", "24")
            self.end_headers()
            self.wfile.write(b"Invalid slot ID: 7 (max)")


def prueba_falso() -> None:
    global GUION
    titulo("MOTOR · reintentos (servidor que falla a propósito)")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Falso)
    puerto = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = {"motor": {"url": f"http://127.0.0.1:{puerto}", "reintentos": 3, "backoff_s": 0.05,
                     "timeout_s": 20},
           "registro": {"ruta": None}}
    m = Motor(cfg)

    GUION = Guion(["ok"])
    r = m.chat("falso", [{"role": "user", "content": "hola"}])
    ok(r["texto"] == "hola mundo", "camino feliz: agrega los deltas del stream", repr(r["texto"]))
    ok(r["tokens_in"] == 11 and r["tokens_out"] == 2,
       "lee usage del último chunk (stream_options.include_usage)",
       f"in={r['tokens_in']} out={r['tokens_out']} ms={r['ms']}")
    ok(r["reintentos"] == 0, "sin reintentos cuando va bien")

    GUION = Guion(["cortado", "ok"])
    r = m.chat("falso", [{"role": "user", "content": "hola"}])
    ok(r["texto"] == "hola mundo" and r["reintentos"] == 1,
       "200 CON STREAM CORTADO ('recovered from upstream disconnection') -> reintenta y acierta",
       f"texto={r['texto']!r} reintentos={r['reintentos']}")
    ok(GUION.i == 2, "y solo hizo falta 1 reintento", f"peticiones={GUION.i}")

    GUION = Guion(["vacio", "ok"])
    r = m.chat("falso", [{"role": "user", "content": "hola"}])
    ok(r["texto"] == "hola mundo" and r["reintentos"] == 1,
       "200 con respuesta VACÍA (el otro disfraz del unload) -> también reintenta",
       f"peticiones={GUION.i}")

    GUION = Guion(["503", "503", "ok"])
    r = m.chat("falso", [{"role": "user", "content": "hola"}])
    ok(r["texto"] == "hola mundo" and r["reintentos"] == 2,
       "5xx dos veces -> reintenta con backoff y acierta a la tercera",
       f"reintentos={r['reintentos']} peticiones={GUION.i}")

    GUION = Guion(["cortado"])
    t0 = time.monotonic()
    try:
        m.chat("falso", [{"role": "user", "content": "hola"}])
        ok(False, "si nunca responde, debe lanzar SinRespuesta", "no lanzó nada")
    except SinRespuesta as e:
        ok(GUION.i == 3, "si nunca responde: 3 intentos y SinRespuesta (no cuelga, no miente)",
           f"peticiones={GUION.i}, {str(e)[:70]}...")
    ok(time.monotonic() - t0 > 0.1, "hubo backoff entre intentos",
       f"{time.monotonic() - t0:.2f}s con backoff_s=0.05")

    GUION = Guion(["400"])
    try:
        m.chat("falso", [{"role": "user", "content": "hola"}], slot=7)
        ok(False, "un 400 debe lanzar ErrorPeticion", "no lanzó")
    except ErrorPeticion as e:
        ok(GUION.i == 1, "un 400 NO se reintenta (repetirlo daría el mismo 400)",
           f"peticiones={GUION.i}: {e}")

    # la regla de la casa, comprobada de verdad sobre el cuerpo que sale por el cable
    GUION = Guion(["ok"])
    m.chat("falso", [{"role": "user", "content": "hola"}], temperatura=0.3, slot=0)
    enviado = GUION.cuerpos[-1]
    ok(not any(k in enviado for k in ("max_tokens", "num_predict", "n_predict")),
       "NINGÚN tope de generación viaja en el cuerpo (regla de la casa)",
       f"claves enviadas: {sorted(enviado)}")
    ok(enviado.get("stream") is True and enviado["stream_options"]["include_usage"] is True,
       "va en streaming y pide el usage")
    try:
        m.chat("falso", [{"role": "user", "content": "x"}], extra={"max_tokens": 100})
        ok(False, "colar max_tokens por 'extra' debe reventar", "pasó")
    except ValueError as e:
        ok(True, "colar max_tokens por 'extra' lanza ValueError", str(e)[:80])

    srv.shutdown()
    srv.server_close()


def main() -> int:
    cual = sys.argv[1] if len(sys.argv) > 1 else "falso"
    if cual not in ("todo", "falso"):
        raise SystemExit("Solo están disponibles pruebas aisladas contra el servidor simulado.")
    prueba_falso()
    print("\n" + "=" * 68)
    if FALLOS:
        print(f"RESULTADO: {PASOS - len(FALLOS)}/{PASOS} — FALLOS: {FALLOS}")
        return 1
    print(f"RESULTADO: {PASOS}/{PASOS} comprobaciones OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
