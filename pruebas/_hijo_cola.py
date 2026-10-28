#!/usr/bin/env python3
"""Proceso VÍCTIMA para la prueba de durabilidad de nucleo/cola.py.

No se ejecuta a mano: lo lanza `pruebas/test_cola.py` y lo mata a lo bruto.

Modos:
  bucle <db>            escribe sin parar y anuncia por stdout CADA commit ya confirmado.
                        El padre le manda SIGKILL en mitad. Invariante a demostrar:
                        todo lo anunciado tiene que estar en la base al reabrirla.
  transaccion <db>      abre una transacción, mete 300 filas, NO hace commit y se suicida
                        con os._exit(1). Invariante: nada de eso puede aparecer.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nucleo.cola import Cola  # noqa: E402


def modo_bucle(ruta: str) -> None:
    c = Cola(ruta)
    oid = c.nuevo_objetivo("apagon: objetivo del hijo", prioridad=7)
    tid = c.añadir_tarea(oid, "tarea que se queda a medias", modelo="qwen05-libre")
    t = c.siguiente("qwen05-libre", worker="hijo-victima")
    assert t is not None and t["id"] == tid
    c.registrar(tid, "paso", {"detalle": "iba por la mitad",
                              "huella": Cola.huella(tid, "paso", {"n": 1})})
    print(f"EN_CURSO {tid}", flush=True)
    print(f"OBJETIVO {oid}", flush=True)

    n = 0
    while True:
        n += 1
        t2 = c.añadir_tarea(oid, f"tarea-bucle-{n}", modelo="m-bucle")
        c.registrar(t2, "creada_en_bucle", {"n": n})
        # OJO: este print sale DESPUÉS de que el COMMIT (con fsync, synchronous=FULL)
        # haya devuelto el control. Lo que se imprime aquí es una promesa de durabilidad.
        print(f"COMMIT {n} {t2}", flush=True)


def modo_transaccion(ruta: str) -> None:
    c = Cola(ruta)
    oid = c.nuevo_objetivo("apagon: transaccion a medias", prioridad=3)
    print(f"OBJETIVO {oid}", flush=True)
    bueno = c.añadir_tarea(oid, "esta SI esta confirmada", modelo="m")
    print(f"CONFIRMADA {bueno}", flush=True)

    # Transacción abierta a mano: 300 tareas que NO deben sobrevivir.
    c.db.execute("BEGIN IMMEDIATE")
    for i in range(300):
        c.db.execute(
            "INSERT INTO tareas(objetivo_id,descripcion,modelo,prioridad,estado,"
            "espera_desde,meta,creado,creado_ts) VALUES(?,?,?,?,'pendiente',0,'{}','x',0)",
            (oid, f"fantasma-{i}", "m", 5),
        )
    print("SIN_COMMIT 300", flush=True)
    sys.stdout.flush()
    os._exit(1)  # muerte súbita con la transacción abierta


if __name__ == "__main__":
    modo, ruta = sys.argv[1], sys.argv[2]
    if modo == "bucle":
        modo_bucle(ruta)
    elif modo == "transaccion":
        modo_transaccion(ruta)
    else:
        raise SystemExit(f"modo desconocido: {modo}")
