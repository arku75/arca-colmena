"""Cola durable de tareas con SQLite WAL y synchronous=FULL.

Gestiona objetivos, dependencias, afinidad de modelo, intentos, eventos y
huellas de idempotencia. siguiente() reclama tareas en una transacción
BEGIN IMMEDIATE. El journal usa triggers contra modificaciones ordinarias;
no constituye un registro inviolable frente a quien controla el archivo.
Las pruebas de recuperación interrumpen únicamente procesos hijos de prueba.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

__all__ = ["Cola", "ESTADOS_TAREA", "ESTADOS_ABIERTOS", "ESTADOS_CERRADOS", "VERSION_ESQUEMA"]

VERSION_ESQUEMA = 1

# Estados posibles de una tarea. 'en_curso' es el testigo de corte: si al arrancar hay
# tareas ahí, es que se fue la luz a mitad (nadie deja una tarea en_curso a propósito).
ESTADOS_TAREA = (
    "pendiente",   # esperando turno
    "en_curso",    # reclamada por un trabajador
    "hecha",       # terminada bien
    "fallida",     # terminada mal (y sin más reintentos)
    "bloqueada",   # una dependencia murió; no puede ejecutarse
    "cancelada",   # la retiró operador o el planificador
    "escalada",    # se cerró porque encoló otra tarea en otro modelo (§A.4 arquitectura)
)
ESTADOS_ABIERTOS = ("pendiente", "en_curso")
ESTADOS_CERRADOS = ("hecha", "fallida", "cancelada", "escalada")

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS meta(
  clave TEXT PRIMARY KEY,
  valor TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS objetivos(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  texto      TEXT    NOT NULL,
  prioridad  INTEGER NOT NULL DEFAULT 5,   -- 1 = relleno ... 9 = urgente (mayor = antes)
  estado     TEXT    NOT NULL DEFAULT 'abierto',  -- abierto|pausado|cerrado
  meta       TEXT    NOT NULL DEFAULT '{}',
  creado     TEXT    NOT NULL,
  creado_ts  REAL    NOT NULL,
  cerrado    TEXT
);

CREATE TABLE IF NOT EXISTS tareas(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  objetivo_id   INTEGER NOT NULL REFERENCES objetivos(id),
  descripcion   TEXT    NOT NULL,
  modelo        TEXT    NOT NULL DEFAULT '*',   -- '*' = le sirve cualquiera (no fuerza swap)
  conversacion  TEXT,                            -- clave de ráfaga: pasos que comparten prefijo
  prioridad     INTEGER NOT NULL DEFAULT 5,
  estado        TEXT    NOT NULL DEFAULT 'pendiente',
  intentos_n    INTEGER NOT NULL DEFAULT 0,
  worker        TEXT,
  claim_ts      REAL,
  no_antes_de   REAL    NOT NULL DEFAULT 0,      -- backoff tras un fallo
  espera_desde  REAL    NOT NULL,                -- para el envejecimiento anti-inanición
  meta          TEXT    NOT NULL DEFAULT '{}',
  resultado     TEXT,
  error         TEXT,
  creado        TEXT    NOT NULL,
  creado_ts     REAL    NOT NULL,
  terminado     TEXT,
  terminado_ts  REAL
);
CREATE INDEX IF NOT EXISTS ix_cola      ON tareas(estado, modelo, prioridad, espera_desde);
CREATE INDEX IF NOT EXISTS ix_tarea_obj ON tareas(objetivo_id);

CREATE TABLE IF NOT EXISTS dependencias(
  tarea_id     INTEGER NOT NULL REFERENCES tareas(id),
  depende_de   INTEGER NOT NULL REFERENCES tareas(id),
  PRIMARY KEY (tarea_id, depende_de)
);
CREATE INDEX IF NOT EXISTS ix_dep_de ON dependencias(depende_de);

CREATE TABLE IF NOT EXISTS intentos(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  tarea_id   INTEGER NOT NULL REFERENCES tareas(id),
  n          INTEGER NOT NULL,
  estado     TEXT    NOT NULL,          -- ejecutando|ok|fallo|cortado
  worker     TEXT,
  modelo     TEXT,
  inicio     TEXT    NOT NULL,
  inicio_ts  REAL    NOT NULL,
  fin        TEXT,
  fin_ts     REAL,
  tok_in     INTEGER,
  tok_out    INTEGER,
  error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_intento_tarea ON intentos(tarea_id, n);

-- EL JOURNAL. Append-only por trigger. Es la verdad de lo que pasó.
CREATE TABLE IF NOT EXISTS eventos(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT    NOT NULL,
  ts_epoch  REAL    NOT NULL,
  tarea_id  INTEGER,
  evento    TEXT    NOT NULL,
  datos     TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_ev_tarea ON eventos(tarea_id, id);
CREATE INDEX IF NOT EXISTS ix_ev_ts    ON eventos(ts_epoch);

-- IDEMPOTENCIA. La clave primaria impide almacenar dos veces una misma huella.
-- El llamador debe consultar la huella antes de repetir una acción externa.
CREATE TABLE IF NOT EXISTS huellas(
  huella     TEXT PRIMARY KEY,
  tarea_id   INTEGER,
  accion     TEXT,
  resultado  TEXT,
  ts         TEXT NOT NULL,
  ts_epoch   REAL NOT NULL
);

-- El journal no se edita jamás.
CREATE TRIGGER IF NOT EXISTS eventos_sin_update
BEFORE UPDATE ON eventos
BEGIN
  SELECT RAISE(ABORT, 'eventos es append-only: no se edita');
END;

-- Y solo se puede borrar desde podar(), que antes lo ha archivado a .ndjson.gz.
CREATE TRIGGER IF NOT EXISTS eventos_sin_delete
BEFORE DELETE ON eventos
WHEN COALESCE((SELECT valor FROM meta WHERE clave='podando'), '0') <> '1'
BEGIN
  SELECT RAISE(ABORT, 'eventos es append-only: solo podar() archiva');
END;
"""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def _json(obj: Any) -> str:
    return json.dumps(obj if obj is not None else {}, ensure_ascii=False, default=str)


def _carga(txt: str | None) -> Any:
    if not txt:
        return {}
    try:
        return json.loads(txt)
    except (ValueError, TypeError):
        return {"_crudo": txt}


class Cola:
    """La cola durable de COLMENA.

    Uso mínimo::

        c = Cola("estado/estado.db")
        o = c.nuevo_objetivo("arreglar el parser", prioridad=7)
        t = c.añadir_tarea(o, "leer el fichero", modelo="qwen36")
        tarea = c.siguiente(modelo_cargado="qwen36")
        c.marcar(tarea["id"], "hecha", {"salida": "..."})
    """

    def __init__(
        self,
        ruta_db: str,
        *,
        espera_max_min: float = 20.0,
        reintentos_max: int = 3,
        reloj: Callable[[], float] | None = None,
        dir_archivo: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        """SQLite con journal_mode=WAL y synchronous=FULL (con WAL, NORMAL pierde la última
        transacción en un corte de luz — y aquí los cortes son reales).

        Parámetros extra (todos con valor por defecto: no cambian la firma del contrato):
          espera_max_min  minutos de "descuento del ascensor"; ver `siguiente()`.
          reintentos_max  cuántas veces se reintenta una tarea antes de darla por fallida.
          reloj           función que devuelve epoch en segundos (inyectable para los tests).
          dir_archivo     dónde deja `podar()` los .ndjson.gz (por defecto <db>/../archivo).
        """
        self.ruta_db = os.path.abspath(ruta_db)
        self.espera_max_min = float(espera_max_min)
        self.reintentos_max = int(reintentos_max)
        self._reloj = reloj or time.time
        self.dir_archivo = os.path.abspath(
            dir_archivo or os.path.join(os.path.dirname(self.ruta_db), "archivo")
        )
        self._lock = threading.RLock()

        os.makedirs(os.path.dirname(self.ruta_db) or ".", exist_ok=True)
        self.db = sqlite3.connect(self.ruta_db, timeout=timeout_s, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self._pragmas()
        self._crear()

    # ------------------------------------------------------------------ interno

    def _pragmas(self) -> None:
        c = self.db
        # journal_mode es persistente en el fichero; synchronous y busy_timeout son
        # POR CONEXIÓN: hay que ponerlos cada vez que se abre. Ese es el fallo clásico.
        modo = c.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(modo).lower() != "wal":
            raise RuntimeError(f"no se pudo poner WAL (quedó en {modo!r})")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA wal_autocheckpoint=512")

    def _crear(self) -> None:
        with self._lock:
            # OJO: executescript() hace COMMIT implícito antes de empezar, así que NO se
            # puede envolver en un BEGIN IMMEDIATE (se lo come). El esquema es idempotente
            # (todo es IF NOT EXISTS), así que se ejecuta suelto.
            self.db.executescript(_ESQUEMA)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "INSERT OR IGNORE INTO meta(clave,valor) VALUES('version_esquema',?)",
                    (str(VERSION_ESQUEMA),),
                )
                self.db.execute("INSERT OR IGNORE INTO meta(clave,valor) VALUES('podando','0')")
                self.db.execute("COMMIT")
            except Exception:
                self._deshacer()
                raise

    def _deshacer(self) -> None:
        """ROLLBACK que no tapa el error de verdad si ya no hay transacción abierta."""
        try:
            self.db.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _ahora(self) -> float:
        return float(self._reloj())

    def _ev(self, tarea_id: int | None, evento: str, datos: Any = None, ts: float | None = None) -> None:
        """Inserta en el journal. NO abre transacción: se llama dentro de una."""
        t = self._ahora() if ts is None else ts
        self.db.execute(
            "INSERT INTO eventos(ts,ts_epoch,tarea_id,evento,datos) VALUES(?,?,?,?,?)",
            (_iso(t), t, tarea_id, evento, _json(datos)),
        )

    def _fila(self, fila: sqlite3.Row | None) -> dict | None:
        if fila is None:
            return None
        d = dict(fila)
        for k in ("meta", "resultado", "datos"):
            if k in d and isinstance(d[k], str):
                d[k] = _carga(d[k])
        return d

    # --------------------------------------------------------------- objetivos

    def nuevo_objetivo(self, texto: str, *, prioridad: int = 5, meta: dict | None = None) -> int:
        """Crea un objetivo. prioridad 1..9 — MAYOR = más urgente (5 = normal)."""
        t = self._ahora()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = self.db.execute(
                    "INSERT INTO objetivos(texto,prioridad,estado,meta,creado,creado_ts)"
                    " VALUES(?,?,'abierto',?,?,?)",
                    (texto, int(prioridad), _json(meta), _iso(t), t),
                )
                oid = int(cur.lastrowid)
                self._ev(None, "objetivo_nuevo", {"objetivo_id": oid, "texto": texto,
                                                  "prioridad": int(prioridad)}, ts=t)
                self.db.execute("COMMIT")
            except Exception:
                self._deshacer()
                raise
        return oid

    def cerrar_objetivo(self, objetivo_id: int, estado: str = "cerrado") -> None:
        t = self._ahora()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "UPDATE objetivos SET estado=?, cerrado=? WHERE id=?",
                    (estado, _iso(t) if estado == "cerrado" else None, int(objetivo_id)),
                )
                self._ev(None, "objetivo_estado", {"objetivo_id": int(objetivo_id),
                                                   "estado": estado}, ts=t)
                self.db.execute("COMMIT")
            except Exception:
                self._deshacer()
                raise

    # ------------------------------------------------------------------ tareas

    def añadir_tarea(
        self,
        objetivo_id: int,
        descripcion: str,
        *,
        modelo: str,
        depende_de: Sequence[int] = (),
        prioridad: int | None = None,
        conversacion: str | None = None,
        meta: dict | None = None,
    ) -> int:
        """Encola una tarea. `modelo` es la afinidad ('*' = cualquiera, no fuerza swap).

        Si no se da `prioridad`, hereda la del objetivo.
        """
        t = self._ahora()
        modelo = (modelo or "*").strip() or "*"
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                obj = self.db.execute(
                    "SELECT prioridad FROM objetivos WHERE id=?", (int(objetivo_id),)
                ).fetchone()
                if obj is None:
                    raise ValueError(f"objetivo {objetivo_id} no existe")
                prio = int(obj["prioridad"]) if prioridad is None else int(prioridad)
                cur = self.db.execute(
                    "INSERT INTO tareas(objetivo_id,descripcion,modelo,conversacion,prioridad,"
                    "estado,espera_desde,meta,creado,creado_ts)"
                    " VALUES(?,?,?,?,?,'pendiente',?,?,?,?)",
                    (int(objetivo_id), descripcion, modelo, conversacion, prio,
                     t, _json(meta), _iso(t), t),
                )
                tid = int(cur.lastrowid)
                for dep in depende_de or ():
                    self.db.execute(
                        "INSERT OR IGNORE INTO dependencias(tarea_id,depende_de) VALUES(?,?)",
                        (tid, int(dep)),
                    )
                self._ev(tid, "tarea_nueva", {"objetivo_id": int(objetivo_id),
                                              "descripcion": descripcion, "modelo": modelo,
                                              "prioridad": prio,
                                              "depende_de": [int(d) for d in (depende_de or ())]},
                         ts=t)
                self.db.execute("COMMIT")
            except Exception:
                self._deshacer()
                raise
        return tid

    # alias por si a alguien le estorba la ñ al teclear
    anadir_tarea = añadir_tarea

    def tarea(self, tarea_id: int) -> dict | None:
        f = self.db.execute("SELECT * FROM tareas WHERE id=?", (int(tarea_id),)).fetchone()
        d = self._fila(f)
        if d is not None:
            d["depende_de"] = [
                int(r[0]) for r in self.db.execute(
                    "SELECT depende_de FROM dependencias WHERE tarea_id=? ORDER BY depende_de",
                    (int(tarea_id),)).fetchall()
            ]
        return d

    # ------------------------------------------------------- EL ASCENSOR

    def siguiente(self, modelo_cargado: str | None, *, worker: str | None = None,
                  reclamar: bool = True) -> dict | None:
        """LA PIEZA CLAVE. Devuelve (y reclama) la siguiente tarea a ejecutar.

        REGLA EXACTA IMPLEMENTADA — "el ascensor con envejecimiento":

          1. Son CANDIDATAS las tareas con estado='pendiente', de un objetivo 'abierto',
             con `no_antes_de <= ahora` (backoff cumplido) y con TODAS sus dependencias
             en estado 'hecha'.
             · Si alguna dependencia acabó en 'fallida'/'cancelada'/'bloqueada', la tarea
               NO es candidata: se marca 'bloqueada' y se registra en el journal.

          2. Cada candidata recibe una PUNTUACIÓN en "minutos equivalentes":

                 puntos = 3 * prioridad_efectiva
                        + minutos_esperando                       (envejecimiento)
                        + espera_max_min   SI ya puede usar el modelo cargado

             donde  prioridad_efectiva = max(prioridad_tarea, prioridad_objetivo)  (1..9)
             y      "puede usar el modelo cargado" = tarea.modelo in ('*', modelo_cargado)
                    (si no hay modelo cargado, nadie cobra esa bonificación).

          3. Gana la puntuación más alta; a igualdad, la tarea más antigua (id menor).

        Por qué así — y por qué NO se muere de hambre nadie:
          · La bonificación por evitar el swap es una CONSTANTE (`espera_max_min`, 20 min
            por defecto), no una barrera. Es un "adelanto" equivalente a 20 minutos de
            espera: agrupa trabajo del modelo residente y ahorra swaps de 15-20 GB...
          · ...pero el término de envejecimiento crece SIN TECHO con el tiempo. Cualquier
            tarea que necesite otro modelo adelanta a las del modelo cargado en cuanto
            lleva `espera_max_min` minutos más esperando que ellas. Cota dura y demostrable:
            una tarea nunca espera más que (la más nueva de su rival) + espera_max_min.
          · La prioridad pesa 3 minutos por punto: separa lo urgente de lo normal al
            principio, pero el tiempo acaba mandando (no hay prioridad que congele a otra).

        Al reclamar (por defecto): estado -> 'en_curso', se apunta worker y claim_ts, sube
        intentos_n, se abre una fila en `intentos` con estado='ejecutando' (ese es el
        testigo de corte que lee `reanudar()`) y se escribe el evento 'reclamada'.
        Todo dentro de una única transacción IMMEDIATE: dos trabajadores no pueden
        llevarse la misma tarea.
        """
        ahora = self._ahora()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # Ordenamos por espera_desde ASC para que las más hambrientas entren SIEMPRE
                # en la ventana, aunque haya miles de tareas pendientes.
                filas = self.db.execute(
                    "SELECT t.*, o.prioridad AS prio_obj FROM tareas t"
                    " JOIN objetivos o ON o.id = t.objetivo_id"
                    " WHERE t.estado='pendiente' AND o.estado='abierto' AND t.no_antes_de <= ?"
                    " ORDER BY t.espera_desde ASC, t.id ASC LIMIT 2000",
                    (ahora,),
                ).fetchall()

                mejor: sqlite3.Row | None = None
                mejor_pts = float("-inf")
                bloqueadas: list[tuple[int, list[int]]] = []

                for f in filas:
                    deps = self.db.execute(
                        "SELECT d.depende_de AS did, t2.estado AS est"
                        " FROM dependencias d JOIN tareas t2 ON t2.id = d.depende_de"
                        " WHERE d.tarea_id=?", (f["id"],)
                    ).fetchall()
                    muertas = [int(d["did"]) for d in deps
                               if d["est"] in ("fallida", "cancelada", "bloqueada")]
                    if muertas:
                        bloqueadas.append((int(f["id"]), muertas))
                        continue
                    if any(d["est"] != "hecha" for d in deps):
                        continue  # todavía hay dependencias vivas sin terminar

                    prio = max(int(f["prioridad"]), int(f["prio_obj"]))
                    espera_min = max(0.0, (ahora - float(f["espera_desde"])) / 60.0)
                    puede = bool(modelo_cargado) and f["modelo"] in ("*", modelo_cargado)
                    pts = 3.0 * prio + espera_min + (self.espera_max_min if puede else 0.0)
                    if pts > mejor_pts or (pts == mejor_pts and mejor is not None
                                           and f["id"] < mejor["id"]):
                        mejor, mejor_pts = f, pts

                for tid, muertas in bloqueadas:
                    self.db.execute(
                        "UPDATE tareas SET estado='bloqueada', error=? WHERE id=?",
                        (f"dependencias muertas: {muertas}", tid),
                    )
                    self._ev(tid, "bloqueada", {"dependencias_muertas": muertas}, ts=ahora)

                if mejor is None:
                    self.db.execute("COMMIT")
                    return None

                tid = int(mejor["id"])
                if not reclamar:
                    self.db.execute("COMMIT")
                    d = dict(mejor)
                    d["puntos"] = mejor_pts
                    d["meta"] = _carga(d.get("meta"))
                    return d

                n = int(mejor["intentos_n"]) + 1
                self.db.execute(
                    "UPDATE tareas SET estado='en_curso', worker=?, claim_ts=?, intentos_n=?"
                    " WHERE id=?", (worker, ahora, n, tid),
                )
                cur = self.db.execute(
                    "INSERT INTO intentos(tarea_id,n,estado,worker,modelo,inicio,inicio_ts)"
                    " VALUES(?,?,'ejecutando',?,?,?,?)",
                    (tid, n, worker, mejor["modelo"], _iso(ahora), ahora),
                )
                intento_id = int(cur.lastrowid)
                self._ev(tid, "reclamada", {
                    "intento": n, "intento_id": intento_id, "worker": worker,
                    "modelo_tarea": mejor["modelo"], "modelo_cargado": modelo_cargado,
                    "puntos": round(mejor_pts, 3),
                    "swap_evitado": bool(modelo_cargado) and mejor["modelo"] in ("*", modelo_cargado),
                    "espera_min": round((ahora - float(mejor["espera_desde"])) / 60.0, 3),
                }, ts=ahora)
                self.db.execute("COMMIT")
            except Exception:
                self._deshacer()
                raise

        d = dict(mejor)
        d["meta"] = _carga(d.get("meta"))
        d["estado"] = "en_curso"
        d["intentos_n"] = n
        d["intento_id"] = intento_id
        d["intento"] = n
        d["worker"] = worker
        d["puntos"] = mejor_pts
        d["espera_min"] = (ahora - float(mejor["espera_desde"])) / 60.0
        return d

    # ----------------------------------------------------------------- journal

    def registrar(self, tarea_id: int | None, evento: str, datos: dict | None = None) -> None:
        """Journal APPEND-ONLY. Es la verdad de lo que pasó. Nunca se edita, nunca se borra.

        Si `datos` trae la clave 'huella', además se indexa en la tabla `huellas` para que
        `ya_hecho()` la vea (la tabla es el índice del journal, no una verdad aparte).
        """
        t = self._ahora()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._ev(None if tarea_id is None else int(tarea_id), evento, datos, ts=t)
                h = (datos or {}).get("huella")
                if h:
                    self.db.execute(
                        "INSERT OR IGNORE INTO huellas(huella,tarea_id,accion,resultado,ts,ts_epoch)"
                        " VALUES(?,?,?,?,?,?)",
                        (str(h), None if tarea_id is None else int(tarea_id),
                         (datos or {}).get("accion") or evento,
                         _json((datos or {}).get("resultado")), _iso(t), t),
                    )
                self.db.execute("COMMIT")
            except Exception:
                self._deshacer()
                raise

    # ------------------------------------------------------------ idempotencia

    @staticmethod
    def huella(tarea_id: int | None, accion: str, argumentos: Any = None) -> str:
        """sha256 canónico de (tarea, acción, argumentos). Determinista entre procesos:
        el JSON va con sort_keys y sin espacios, así que el orden de un dict no cambia nada."""
        payload = json.dumps(
            {"t": None if tarea_id is None else int(tarea_id),
             "a": accion,
             "g": argumentos},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def ya_hecho(self, huella: str) -> bool:
        """Indica si la huella fue registrada; el llamador decide cómo usar el resultado."""
        f = self.db.execute("SELECT 1 FROM huellas WHERE huella=?", (str(huella),)).fetchone()
        return f is not None

    def anotar_huella(self, huella: str, *, tarea_id: int | None = None,
                      accion: str | None = None, resultado: Any = None) -> bool:
        """Marca la acción como hecha. Devuelve True si es NUEVA, False si ya estaba
        (la carrera entre dos trabajadores la resuelve el PRIMARY KEY, no un if)."""
        t = self._ahora()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO huellas(huella,tarea_id,accion,resultado,ts,ts_epoch)"
                    " VALUES(?,?,?,?,?,?)",
                    (str(huella), None if tarea_id is None else int(tarea_id), accion,
                     _json(resultado), _iso(t), t),
                )
                nueva = cur.rowcount == 1
                if nueva:
                    self._ev(tarea_id, "huella", {"huella": str(huella), "accion": accion}, ts=t)
                self.db.execute("COMMIT")
            except Exception:
                self._deshacer()
                raise
        return nueva

    # ----------------------------------------------------------------- cerrar

    def marcar(self, tarea_id: int, estado: str, resultado: dict | None = None) -> None:
        """Cierra (o devuelve a la cola) una tarea y cierra su intento abierto.

        Convenciones de `resultado` (opcionales):
          error          texto del fallo -> se guarda en tareas.error e intentos.error
          reintentar_en_s  si estado='pendiente', retrasa `no_antes_de` esa cantidad
          tok_in/tok_out  se guardan en el intento
        """
        if estado not in ESTADOS_TAREA:
            raise ValueError(f"estado desconocido: {estado!r} (válidos: {ESTADOS_TAREA})")
        t = self._ahora()
        res = resultado or {}
        err = res.get("error")
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                fila = self.db.execute("SELECT * FROM tareas WHERE id=?", (int(tarea_id),)).fetchone()
                if fila is None:
                    raise ValueError(f"tarea {tarea_id} no existe")

                cerrada = estado in ESTADOS_CERRADOS or estado == "bloqueada"
                if estado == "pendiente":
                    espera = float(res.get("reintentar_en_s") or 0.0)
                    self.db.execute(
                        "UPDATE tareas SET estado='pendiente', worker=NULL, claim_ts=NULL,"
                        " no_antes_de=?, resultado=?, error=? WHERE id=?",
                        (t + espera, _json(res), err, int(tarea_id)),
                    )
                else:
                    self.db.execute(
                        "UPDATE tareas SET estado=?, resultado=?, error=?, terminado=?,"
                        " terminado_ts=?, worker=NULL, claim_ts=NULL WHERE id=?",
                        (estado, _json(res), err, _iso(t) if cerrada else None,
                         t if cerrada else None, int(tarea_id)),
                    )

                self.db.execute(
                    "UPDATE intentos SET estado=?, fin=?, fin_ts=?, tok_in=?, tok_out=?, error=?"
                    " WHERE tarea_id=? AND estado='ejecutando'",
                    ("ok" if estado == "hecha" else "fallo", _iso(t), t,
                     res.get("tokens_in"), res.get("tokens_out"), err, int(tarea_id)),
                )
                self._ev(int(tarea_id), f"tarea_{estado}", res, ts=t)
                h = res.get("huella")
                if h:
                    self.db.execute(
                        "INSERT OR IGNORE INTO huellas(huella,tarea_id,accion,resultado,ts,ts_epoch)"
                        " VALUES(?,?,?,?,?,?)",
                        (str(h), int(tarea_id), res.get("accion") or estado, _json(res), _iso(t), t),
                    )
                self.db.execute("COMMIT")
            except Exception:
                self._deshacer()
                raise

    def reanudar(self) -> list[dict]:
        """Al arrancar: devuelve las tareas que quedaron 'en curso' para retomarlas.

        Una tarea 'en_curso' al abrir la base solo puede significar una cosa: se cortó a
        mitad (apagón, kill -9, OOM). Se devuelven a 'pendiente' conservando el histórico:
        el intento abierto se cierra como 'cortado' (queda la cicatriz en `intentos`), se
        escribe el evento 'reanudada' en el journal y `intentos_n` NO se toca — ya se
        contó al reclamar, así que el presupuesto de reintentos no se puede burlar
        apagando el portátil.
        """
        t = self._ahora()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                filas = self.db.execute(
                    "SELECT * FROM tareas WHERE estado='en_curso' ORDER BY id"
                ).fetchall()
                salida = []
                for f in filas:
                    tid = int(f["id"])
                    self.db.execute(
                        "UPDATE intentos SET estado='cortado', fin=?, fin_ts=?,"
                        " error='corte: proceso muerto con la tarea en curso'"
                        " WHERE tarea_id=? AND estado='ejecutando'",
                        (_iso(t), t, tid),
                    )
                    agotada = int(f["intentos_n"]) >= self.reintentos_max
                    nuevo = "fallida" if agotada else "pendiente"
                    if agotada:
                        self.db.execute(
                            "UPDATE tareas SET estado='fallida', error=?, terminado=?,"
                            " terminado_ts=?, worker=NULL, claim_ts=NULL WHERE id=?",
                            (f"cortada {f['intentos_n']} veces (max {self.reintentos_max})",
                             _iso(t), t, tid),
                        )
                    else:
                        self.db.execute(
                            "UPDATE tareas SET estado='pendiente', worker=NULL, claim_ts=NULL"
                            " WHERE id=?", (tid,),
                        )
                    self._ev(tid, "reanudada", {
                        "intentos_n": int(f["intentos_n"]), "estado_nuevo": nuevo,
                        "worker_muerto": f["worker"],
                    }, ts=t)
                    d = self._fila(f) or {}
                    d["estado"] = nuevo
                    salida.append(d)
                self._ev(None, "reanudar", {"tareas": [d["id"] for d in salida]}, ts=t)
                self.db.execute("COMMIT")
            except Exception:
                self._deshacer()
                raise
        return salida

    # ------------------------------------------------------------------ podar

    def podar(self, dias: int = 90) -> dict:
        """Archiva (NO borra) los eventos viejos de tareas ya cerradas.

        Los eventos se escriben a `<dir_archivo>/eventos-<fecha>.ndjson.gz` con escritura
        atómica (tmp -> flush -> fsync -> os.replace -> fsync del directorio) y SOLO
        entonces se borran de la tabla. Las `huellas` NO se tocan nunca: si se borrasen,
        el sistema volvería a repetir pasos ya hechos, que es justo el fallo que evitan.
        """
        corte = self._ahora() - float(dias) * 86400.0
        with self._lock:
            filas = self.db.execute(
                "SELECT e.* FROM eventos e JOIN tareas t ON t.id = e.tarea_id"
                " WHERE t.estado IN ('hecha','fallida','cancelada','escalada')"
                "   AND t.terminado_ts IS NOT NULL AND t.terminado_ts < ?"
                "   AND e.ts_epoch < ? ORDER BY e.id",
                (corte, corte),
            ).fetchall()
            if not filas:
                return {"archivados": 0, "fichero": None}

            os.makedirs(self.dir_archivo, exist_ok=True)
            marca = datetime.fromtimestamp(self._ahora(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            destino = os.path.join(self.dir_archivo, f"eventos-{marca}.ndjson.gz")
            tmp = destino + ".tmp"
            with open(tmp, "wb") as fh:
                with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
                    for f in filas:
                        gz.write((json.dumps(dict(f), ensure_ascii=False, default=str) + "\n")
                                 .encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, destino)
            dfd = os.open(self.dir_archivo, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)

            ids = [int(f["id"]) for f in filas]
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("UPDATE meta SET valor='1' WHERE clave='podando'")
                for i in range(0, len(ids), 500):
                    trozo = ids[i:i + 500]
                    self.db.execute(
                        f"DELETE FROM eventos WHERE id IN ({','.join('?' * len(trozo))})", trozo
                    )
                self.db.execute("UPDATE meta SET valor='0' WHERE clave='podando'")
                self._ev(None, "podado", {"archivados": len(ids), "fichero": destino,
                                          "dias": dias})
                self.db.execute("COMMIT")
            except Exception:
                self._deshacer()
                self.db.execute("UPDATE meta SET valor='0' WHERE clave='podando'")
                raise
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"archivados": len(ids), "fichero": destino}

    # ------------------------------------------------------------------ varios

    def eventos(self, tarea_id: int | None = None, limite: int = 50) -> list[dict]:
        if tarea_id is None:
            filas = self.db.execute(
                "SELECT * FROM eventos ORDER BY id DESC LIMIT ?", (int(limite),)).fetchall()
        else:
            filas = self.db.execute(
                "SELECT * FROM eventos WHERE tarea_id=? ORDER BY id DESC LIMIT ?",
                (int(tarea_id), int(limite))).fetchall()
        out = []
        for f in filas:
            d = dict(f)
            d["datos"] = _carga(d["datos"])
            out.append(d)
        return out

    def resumen(self) -> dict:
        filas = self.db.execute(
            "SELECT estado, COUNT(*) n FROM tareas GROUP BY estado").fetchall()
        r = {"tareas": {f["estado"]: f["n"] for f in filas}}
        r["objetivos"] = {f["estado"]: f["n"] for f in self.db.execute(
            "SELECT estado, COUNT(*) n FROM objetivos GROUP BY estado").fetchall()}
        r["eventos"] = self.db.execute("SELECT COUNT(*) FROM eventos").fetchone()[0]
        r["huellas"] = self.db.execute("SELECT COUNT(*) FROM huellas").fetchone()[0]
        r["pragmas"] = {
            "journal_mode": self.db.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": self.db.execute("PRAGMA synchronous").fetchone()[0],
            "foreign_keys": self.db.execute("PRAGMA foreign_keys").fetchone()[0],
        }
        return r

    def integridad(self) -> str:
        return self.db.execute("PRAGMA integrity_check").fetchone()[0]

    def cerrar(self) -> None:
        with self._lock:
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.db.close()

    def __enter__(self) -> "Cola":
        return self

    def __exit__(self, *_exc) -> None:
        self.cerrar()
