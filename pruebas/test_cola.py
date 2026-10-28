#!/usr/bin/env python3
"""Prueba REAL de nucleo/cola.py. Sin mocks: SQLite de verdad y kill -9 de verdad.

    python3 pruebas/test_cola.py            # todo
    python3 pruebas/test_cola.py apagon     # solo la parte del corte

Lo que demuestra, en este orden:
  1. PRAGMAs: WAL + synchronous=FULL puestos de verdad en la conexión.
  2. El ascensor: `siguiente()` prefiere lo que ya puede usar el modelo cargado.
  3. Anti-inanición: lo que lleva esperando mucho SALE, aunque obligue a cambiar de modelo.
  4. Dependencias: una tarea no sale antes que aquello de lo que depende.
  5. `ya_hecho()`: la huella impide repetir un paso ya dado.
  6. El journal es append-only de verdad (el UPDATE revienta).
  7. `podar()` archiva a .ndjson.gz antes de borrar, y las huellas sobreviven.
  8. APAGÓN: kill -9 en mitad de una escritura y os._exit(1) con la transacción abierta.
"""

import gzip
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from nucleo.cola import Cola  # noqa: E402

FALLOS: list[str] = []
PASOS = 0


def ok(cond: bool, titulo: str, detalle: str = "") -> None:
    global PASOS
    PASOS += 1
    marca = "OK  " if cond else "FALLO"
    print(f"  [{marca}] {titulo}" + (f"  -> {detalle}" if detalle else ""))
    if not cond:
        FALLOS.append(titulo)


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


class Reloj:
    """Reloj falso: así se prueba la inanición sin esperar 20 minutos de verdad."""

    def __init__(self, t0: float | None = None) -> None:
        self.t = t0 if t0 is not None else time.time()

    def __call__(self) -> float:
        return self.t

    def avanza_min(self, m: float) -> None:
        self.t += m * 60.0


# --------------------------------------------------------------------------- 1 pragmas

def prueba_pragmas(tmp: str) -> None:
    titulo("1 · PRAGMAs de durabilidad")
    with Cola(os.path.join(tmp, "p.db")) as c:
        jm = c.db.execute("PRAGMA journal_mode").fetchone()[0]
        sy = c.db.execute("PRAGMA synchronous").fetchone()[0]
        fk = c.db.execute("PRAGMA foreign_keys").fetchone()[0]
        ok(str(jm).lower() == "wal", "journal_mode = WAL", str(jm))
        ok(int(sy) == 2, "synchronous = FULL (2)", f"valor={sy}")
        ok(int(fk) == 1, "foreign_keys = ON", f"valor={fk}")
        ok(c.integridad() == "ok", "integrity_check", c.integridad())


# ---------------------------------------------------------------------- 2 el ascensor

def prueba_ascensor(tmp: str) -> None:
    titulo("2 · El ascensor: prefiere el modelo YA cargado")
    r = Reloj()
    with Cola(os.path.join(tmp, "a.db"), reloj=r, espera_max_min=20) as c:
        o = c.nuevo_objetivo("objetivo unico", prioridad=5)
        t_otro = c.añadir_tarea(o, "necesita el modelo GRANDE", modelo="qwen36-largo")
        t_carg = c.añadir_tarea(o, "puede usar el cargado", modelo="qwen05-libre")
        t_cual = c.añadir_tarea(o, "le vale cualquiera", modelo="*")

        elegida = c.siguiente("qwen05-libre", worker="w1")
        ok(elegida["id"] == t_otro or elegida["id"] in (t_carg, t_cual),
           "devuelve una tarea", f"id={elegida['id']}")
        ok(elegida["id"] == t_carg,
           "con qwen05-libre cargado elige la del modelo cargado (no la que forzaría swap)",
           f"elegida={elegida['id']} ({elegida['descripcion']}), puntos={elegida['puntos']:.1f}")
        ok(elegida["estado"] == "en_curso" and elegida["intento"] == 1,
           "la reclama: estado=en_curso e intento=1",
           f"estado={elegida['estado']} intento={elegida['intento']}")

        c.marcar(t_carg, "hecha", {"salida": "ya"})
        seg = c.siguiente("qwen05-libre", worker="w1")
        ok(seg["id"] == t_cual, "la segunda es la de modelo '*' (tampoco fuerza swap)",
           f"elegida={seg['id']} ({seg['descripcion']})")
        c.marcar(t_cual, "hecha", {})

        ter = c.siguiente("qwen05-libre", worker="w1")
        ok(ter["id"] == t_otro, "cuando se agota lo del modelo cargado, sí sale la otra",
           f"elegida={ter['id']} ({ter['descripcion']})")
        c.marcar(t_otro, "hecha", {})
        ok(c.siguiente("qwen05-libre") is None, "cola vacía -> None")

        # dos trabajadores no pueden llevarse la misma tarea
        t1 = c.añadir_tarea(o, "unica", modelo="*")
        a = c.siguiente("qwen05-libre", worker="wA")
        b = c.siguiente("qwen05-libre", worker="wB")
        ok(a is not None and a["id"] == t1 and b is None,
           "reclamo atómico: el segundo trabajador NO se lleva la misma tarea",
           f"wA={a['id'] if a else None} wB={b['id'] if b else None}")


# ------------------------------------------------------------------ 3 anti-inanición

def prueba_inanicion(tmp: str) -> None:
    titulo("3 · Anti-inanición: lo que espera mucho acaba saliendo")
    r = Reloj()
    with Cola(os.path.join(tmp, "h.db"), reloj=r, espera_max_min=20) as c:
        o = c.nuevo_objetivo("mixto", prioridad=5)
        vieja = c.añadir_tarea(o, "VIEJA, necesita otro modelo", modelo="q8")

        # Simulamos 8 horas en las que no paran de entrar tareas del modelo cargado.
        elegidas = []
        for ronda in range(24):
            r.avanza_min(20)
            nueva = c.añadir_tarea(o, f"nueva-{ronda} del modelo cargado", modelo="qwen05-libre")
            t = c.siguiente("qwen05-libre", worker="w")
            elegidas.append(t["id"])
            c.marcar(t["id"], "hecha", {})
            if t["id"] == vieja:
                break

        salio = vieja in elegidas
        pos = elegidas.index(vieja) + 1 if salio else -1
        espera_real = None
        f = c.tarea(vieja)
        ok(salio, "la tarea vieja SALE pese a que siempre había trabajo del modelo cargado",
           f"salió en la ronda {pos} de {len(elegidas)}; estado final={f['estado']}")
        # cota dura: sale en cuanto lleva espera_max_min más que su rival más nueva
        ok(pos <= 3, "y sale pronto (cota = espera_max_min de ventaja, no 'algún día')",
           f"ronda {pos} (cada ronda = 20 min simulados)")

    # La frontera exacta, cada caso en su propia base (una cola sucia no demuestra nada).
    for ventaja, gana_la_vieja in ((25, True), (5, False)):
        r2 = Reloj()
        with Cola(os.path.join(tmp, f"h{ventaja}.db"), reloj=r2, espera_max_min=20) as c2:
            o = c2.nuevo_objetivo("frontera", prioridad=5)
            vieja = c2.añadir_tarea(o, f"vieja q8 ({ventaja} min de ventaja)", modelo="q8")
            r2.avanza_min(ventaja)
            nueva = c2.añadir_tarea(o, "nueva del modelo cargado", modelo="qwen05-libre")
            cand = c2.siguiente("qwen05-libre", reclamar=False)
            esperada = vieja if gana_la_vieja else nueva
            ok(cand["id"] == esperada,
               f"con {ventaja} min de ventaja gana "
               + ("la que fuerza swap" if gana_la_vieja else "la del modelo cargado"),
               f"gana '{cand['descripcion']}' con {cand['puntos']:.1f} puntos")


# ------------------------------------------------------------------ 4 dependencias

def prueba_dependencias(tmp: str) -> None:
    titulo("4 · Dependencias")
    with Cola(os.path.join(tmp, "d.db")) as c:
        o = c.nuevo_objetivo("con deps")
        a = c.añadir_tarea(o, "primero A", modelo="*")
        b = c.añadir_tarea(o, "luego B", modelo="*", depende_de=[a])
        x = c.siguiente(None, worker="w")
        ok(x["id"] == a, "B no sale antes que A", f"salió {x['id']}")
        ok(c.siguiente(None) is None, "y B sigue bloqueada mientras A está en curso")
        c.marcar(a, "hecha", {})
        y = c.siguiente(None, worker="w")
        ok(y is not None and y["id"] == b, "con A hecha, B ya sale", f"salió {y['id'] if y else None}")

        # dependencia muerta -> la hija se marca bloqueada (no se queda colgada para siempre)
        c2 = c
        a2 = c2.añadir_tarea(o, "A2 que fallará", modelo="*")
        b2 = c2.añadir_tarea(o, "B2 depende de A2", modelo="*", depende_de=[a2])
        c2.marcar(b, "hecha", {})
        t = c2.siguiente(None, worker="w")
        c2.marcar(t["id"], "fallida", {"error": "reventó"})
        ok(c2.siguiente(None) is None, "no devuelve la hija de una tarea fallida")
        ok(c2.tarea(b2)["estado"] == "bloqueada", "y la marca 'bloqueada' con el motivo",
           c2.tarea(b2)["error"])


# -------------------------------------------------------------------- 5 idempotencia

def prueba_huellas(tmp: str) -> None:
    titulo("5 · ya_hecho(): no repetir un paso ya dado")
    with Cola(os.path.join(tmp, "i.db")) as c:
        o = c.nuevo_objetivo("idempotencia")
        t = c.añadir_tarea(o, "escribir fichero", modelo="*")
        h = Cola.huella(t, "escribir", {"ruta": "/tmp/x", "contenido": "hola"})
        ok(len(h) == 64, "la huella es un sha256", h[:16] + "...")
        ok(not c.ya_hecho(h), "antes de hacerlo: ya_hecho = False")
        ok(c.anotar_huella(h, tarea_id=t, accion="escribir"), "anotar_huella devuelve True (nueva)")
        ok(c.ya_hecho(h), "después: ya_hecho = True")
        ok(not c.anotar_huella(h, tarea_id=t, accion="escribir"),
           "repetir la misma huella devuelve False (no la duplica)")

        h2 = Cola.huella(t, "escribir", {"contenido": "hola", "ruta": "/tmp/x"})
        ok(h == h2, "el orden de las claves NO cambia la huella (json canónico)")
        h3 = Cola.huella(t, "escribir", {"ruta": "/tmp/x", "contenido": "adios"})
        ok(h != h3, "cambiar un argumento SÍ cambia la huella")

        # y por el otro camino: registrar() con huella también la indexa
        h4 = Cola.huella(t, "bash", {"cmd": "ls"})
        c.registrar(t, "accion", {"huella": h4, "accion": "bash"})
        ok(c.ya_hecho(h4), "registrar() con 'huella' en los datos también la indexa")


# ----------------------------------------------------------------- 6 journal duro

def prueba_journal(tmp: str) -> None:
    titulo("6 · El journal es append-only de verdad")
    with Cola(os.path.join(tmp, "j.db")) as c:
        o = c.nuevo_objetivo("journal")
        t = c.añadir_tarea(o, "algo", modelo="*")
        c.registrar(t, "prueba", {"a": 1})
        n = c.db.execute("SELECT COUNT(*) FROM eventos").fetchone()[0]
        ok(n >= 3, f"hay eventos escritos ({n})")
        try:
            c.db.execute("UPDATE eventos SET evento='mentira' WHERE id=1")
            ok(False, "UPDATE sobre eventos debe fallar", "¡se dejó editar!")
        except Exception as e:
            ok("append-only" in str(e), "UPDATE sobre eventos ABORTA", str(e))
        try:
            c.db.execute("DELETE FROM eventos WHERE id=1")
            ok(False, "DELETE sobre eventos debe fallar", "¡se dejó borrar!")
        except Exception as e:
            ok("append-only" in str(e), "DELETE sobre eventos ABORTA", str(e))


# --------------------------------------------------------------------- 7 podar

def prueba_podar(tmp: str) -> None:
    titulo("7 · podar(): archiva a .ndjson.gz antes de borrar")
    r = Reloj()
    arch = os.path.join(tmp, "archivo")
    with Cola(os.path.join(tmp, "pod.db"), reloj=r, dir_archivo=arch) as c:
        o = c.nuevo_objetivo("viejo")
        t = c.añadir_tarea(o, "vieja tarea", modelo="*")
        x = c.siguiente(None, worker="w")
        h = Cola.huella(t, "paso", {"n": 1})
        c.registrar(t, "paso", {"n": 1, "huella": h})
        c.marcar(t, "hecha", {"salida": "ok"})
        antes = c.db.execute("SELECT COUNT(*) FROM eventos").fetchone()[0]

        r.avanza_min(60 * 24 * 200)  # 200 días después
        t2 = c.añadir_tarea(o, "tarea reciente", modelo="*")
        c.registrar(t2, "reciente", {"x": 1})

        res = c.podar(dias=90)
        despues = c.db.execute("SELECT COUNT(*) FROM eventos").fetchone()[0]
        ok(res["archivados"] > 0, f"archivó {res['archivados']} eventos", res["fichero"] or "")
        ok(os.path.exists(res["fichero"]), "el .ndjson.gz existe en disco")
        with gzip.open(res["fichero"], "rt", encoding="utf-8") as fh:
            lineas = [json.loads(l) for l in fh]
        ok(len(lineas) == res["archivados"], "el fichero tiene todas las líneas",
           f"{len(lineas)} líneas")
        ok(all("evento" in l for l in lineas), "cada línea es un evento entero (no un resumen)",
           str(lineas[0])[:110] + "...")
        ok(despues < antes + 2, f"la tabla encoge: {antes + 2} -> {despues} eventos")
        ok(c.ya_hecho(h), "LA HUELLA SOBREVIVE a la poda (si no, repetiría pasos ya hechos)")
        vivos = c.db.execute("SELECT COUNT(*) FROM eventos WHERE tarea_id=?",
                             (t2,)).fetchone()[0]
        ok(vivos == 2, "los eventos de la tarea VIVA no se archivan",
           f"{vivos} eventos siguen en la tabla (alta + 'reciente')")
        ids_gz = {l["tarea_id"] for l in lineas}
        ok(t2 not in ids_gz, "y no aparecen en el .gz", f"tareas archivadas: {sorted(ids_gz)}")


# ------------------------------------------------------------ 8 EL APAGÓN (kill -9)

def prueba_apagon(tmp: str) -> None:
    titulo("8 · APAGÓN: kill -9 en mitad de la escritura")
    db = os.path.join(tmp, "corte.db")
    hijo = os.path.join(RAIZ, "pruebas", "_hijo_cola.py")

    p = subprocess.Popen([sys.executable, hijo, "bucle", db],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, bufsize=1)
    en_curso = None
    ultimo_n = 0
    ultimo_id = 0
    objetivo = None
    t_ini = time.time()
    while time.time() - t_ini < 30:
        linea = p.stdout.readline()
        if not linea:
            break
        linea = linea.strip()
        if linea.startswith("EN_CURSO"):
            en_curso = int(linea.split()[1])
        elif linea.startswith("OBJETIVO"):
            objetivo = int(linea.split()[1])
        elif linea.startswith("COMMIT"):
            _, n, tid = linea.split()
            ultimo_n, ultimo_id = int(n), int(tid)
            if ultimo_n >= 60:
                break
    print(f"  ... el hijo (pid {p.pid}) anunció {ultimo_n} commits; último id={ultimo_id}")
    os.kill(p.pid, signal.SIGKILL)          # kill -9 A PELO, sin darle tiempo a cerrar nada
    rc = p.wait(timeout=10)
    print(f"  ... SIGKILL enviado. El proceso murió con returncode={rc} (-9 = SIGKILL)")
    ok(rc == -9, "el hijo murió por SIGKILL, no por un cierre limpio", f"rc={rc}")
    print(f"  ... ficheros que quedaron: {sorted(os.path.basename(f) for f in os.listdir(tmp) if f.startswith('corte'))}")

    # --- se reabre la base, como haría `colmena-reanudar` tras el apagón ---
    c = Cola(db)
    integ = c.integridad()
    ok(integ == "ok", "PRAGMA integrity_check tras el kill -9", integ)
    n_tareas = c.db.execute("SELECT COUNT(*) FROM tareas").fetchone()[0]
    hay_ultima = c.tarea(ultimo_id) is not None
    ok(hay_ultima, "LA ÚLTIMA TAREA QUE EL HIJO DIJO HABER CONFIRMADO ESTÁ EN LA BASE",
       f"id={ultimo_id} -> {c.tarea(ultimo_id)['descripcion'] if hay_ultima else 'NO ESTÁ'}")
    faltan = [i for i in range(1, ultimo_id + 1) if c.tarea(i) is None]
    ok(not faltan, f"no falta ninguna de las {ultimo_id} filas confirmadas",
       f"tareas en la base: {n_tareas}")

    ev = c.db.execute("SELECT COUNT(*) FROM eventos").fetchone()[0]
    print(f"  ... la base traía {n_tareas} tareas y {ev} eventos del hijo muerto")

    reanudadas = c.reanudar()
    ids = [t["id"] for t in reanudadas]
    ok(ids == [en_curso], "reanudar() devuelve EXACTAMENTE la tarea que quedó a medias",
       f"reanudadas={ids} (esperada={en_curso})")
    ok(reanudadas and reanudadas[0]["estado"] == "pendiente",
       "y la deja lista para retomarla (pendiente)",
       f"estado={reanudadas[0]['estado'] if reanudadas else '?'}, "
       f"intentos_n={reanudadas[0]['intentos_n'] if reanudadas else '?'}")
    intento = c.db.execute(
        "SELECT estado,error FROM intentos WHERE tarea_id=? ORDER BY id DESC LIMIT 1",
        (en_curso,)).fetchone()
    ok(intento["estado"] == "cortado", "el intento abierto queda como 'cortado' (la cicatriz)",
       f"{intento['estado']}: {intento['error']}")
    hist = [e["evento"] for e in c.eventos(en_curso)]
    ok("reanudada" in hist and "paso" in hist,
       "el journal conserva lo que había pasado antes del corte", str(hist))
    ok(c.ya_hecho(Cola.huella(en_curso, "paso", {"n": 1})),
       "y la huella de lo ya hecho sobrevivió: al retomar NO repetirá ese paso")

    otra = c.siguiente("qwen05-libre", worker="tras-el-apagon")
    ok(otra is not None and otra["id"] == en_curso,
       "y la cola la vuelve a servir la primera al reanudar el trabajo",
       f"siguiente() -> {otra['id'] if otra else None}")
    c.cerrar()

    # --- variante: muerte con una transacción ABIERTA (atomicidad) ---
    print()
    db2 = os.path.join(tmp, "corte2.db")
    p2 = subprocess.run([sys.executable, hijo, "transaccion", db2],
                        capture_output=True, text=True, timeout=60)
    print(f"  ... hijo 2 (os._exit(1) con la transacción abierta) rc={p2.returncode}")
    print("      " + p2.stdout.strip().replace("\n", " | "))
    confirmada = int([l for l in p2.stdout.splitlines() if l.startswith("CONFIRMADA")][0].split()[1])
    c2 = Cola(db2)
    ok(c2.integridad() == "ok", "integrity_check de la 2ª base", c2.integridad())
    ok(c2.tarea(confirmada) is not None, "lo confirmado ANTES de la transacción sigue ahí",
       f"id={confirmada}")
    fantasmas = c2.db.execute(
        "SELECT COUNT(*) FROM tareas WHERE descripcion LIKE 'fantasma-%'").fetchone()[0]
    ok(fantasmas == 0, "las 300 filas de la transacción sin commit NO existen (atomicidad)",
       f"fantasmas encontrados: {fantasmas}")
    c2.cerrar()


# ------------------------------------------------------------------------- main

def main() -> int:
    solo = sys.argv[1] if len(sys.argv) > 1 else None
    tmp = tempfile.mkdtemp(prefix="colmena-cola-")
    print(f"Base de pruebas en {tmp}")
    print(f"Python {sys.version.split()[0]} · sqlite3 "
          f"{__import__('sqlite3').sqlite_version}")
    try:
        if solo in (None, "todo"):
            prueba_pragmas(tmp)
            prueba_ascensor(tmp)
            prueba_inanicion(tmp)
            prueba_dependencias(tmp)
            prueba_huellas(tmp)
            prueba_journal(tmp)
            prueba_podar(tmp)
            prueba_apagon(tmp)
        elif solo == "apagon":
            prueba_apagon(tmp)
        else:
            raise SystemExit(f"no sé qué es {solo!r}")
    finally:
        print(f"\n(dejo {tmp} por si quieres mirarlo)")

    print("\n" + "=" * 68)
    if FALLOS:
        print(f"RESULTADO: {PASOS - len(FALLOS)}/{PASOS} — FALLOS: {FALLOS}")
        return 1
    print(f"RESULTADO: {PASOS}/{PASOS} comprobaciones OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
