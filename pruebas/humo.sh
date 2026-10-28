#!/usr/bin/env bash
# Datos sintéticos, directorios temporales y servidor HTTP de prueba local.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1
python3 examples/offline_demo.py
python3 -m unittest discover -s pruebas -p 'test_memoria.py' -v
python3 -m unittest discover -s pruebas -p 'test_barreras.py' -v
python3 pruebas/test_cola.py
python3 pruebas/test_motor.py
