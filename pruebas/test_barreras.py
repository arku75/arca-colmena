"""Pruebas aisladas de publicación; no sensores ni ejecución de comandos ajenos."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nucleo import barreras
from nucleo.verificar import verificar
from colmena.bucle import Colmena, cargar_roles


class PublicationGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="colmena-guards-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.allowlist = self.root / "allowlist.json"
        self.allowlist.write_text(json.dumps({"version": 1, "politica": {"permitir_eval": False}, "entradas": []}))
        self.patches = [
            patch.object(barreras, "RUTA_ALLOWLIST", self.allowlist),
            patch.object(barreras, "RUTA_ESTADO", self.root / "state.json"),
            patch.object(barreras, "_cfg_cache", {"limites": {"pasos_max_tarea": 2,
                "segundos_max_tarea": 3600, "repeticiones_max": 3}}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_empty_allowlist_denies(self):
        for command in ([sys.executable, "--version"], ["git", "status"], "echo sample", []):
            with self.subTest(command=command):
                self.assertFalse(barreras.puede_ejecutar(command)[0])

    def test_corrupt_allowlist_denies(self):
        self.allowlist.write_text("{bad json")
        self.assertFalse(barreras.puede_ejecutar([sys.executable, "--version"])[0])

    def test_budget_limits_steps(self):
        barreras.iniciar_tarea("synthetic")
        self.assertTrue(barreras.registrar_paso("synthetic", "one")[0])
        self.assertTrue(barreras.registrar_paso("synthetic", "two")[0])
        self.assertFalse(barreras.registrar_paso("synthetic", "three")[0])
        barreras.reiniciar_tarea("synthetic")
        self.assertTrue(barreras.presupuesto("synthetic")[0])

    def test_repetition_stops_budget(self):
        with patch.object(barreras, "_cfg_cache", {"limites": {"pasos_max_tarea": 20,
                               "segundos_max_tarea": 3600, "repeticiones_max": 3}}):
            self.assertTrue(barreras.registrar_paso("repeat", "same")[0])
            self.assertTrue(barreras.registrar_paso("repeat", "same")[0])
            self.assertFalse(barreras.registrar_paso("repeat", "same")[0])

    def test_verifier_is_blocked_before_any_action(self):
        with patch("nucleo.verificar._rutas_reales", side_effect=AssertionError("must not resolve")), \
             patch("nucleo.verificar.subprocess.run", side_effect=AssertionError("must not execute")):
            result = verificar({"rutas": [str(self.root / "synthetic.py")], "humo": "echo sample"})
        self.assertFalse(result["ok"])
        self.assertFalse(result["independiente"])
        self.assertEqual(result["confianza"], "nula")
        self.assertEqual(result["señales"], [])

    def test_orchestrator_is_blocked_before_configuration(self):
        with patch("colmena.bucle._motor.Motor", side_effect=AssertionError("must not create")):
            with self.assertRaisesRegex(RuntimeError, "histórico"):
                Colmena(str(self.root / "nonexistent.toml"))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["allowlist.json"])

    def test_six_role_templates_load_without_model_calls(self):
        roles = cargar_roles()
        self.assertEqual(set(roles), {"obrero", "planificador", "critico", "investigador", "juez", "reparador"})
        self.assertTrue(all(r.prefijo for r in roles.values()))
        self.assertEqual(sum(r.esquema() is not None for r in roles.values()), 4)


if __name__ == "__main__":
    unittest.main()
