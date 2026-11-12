"""Перевірка параметрів алгоритму в config.json."""
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

from esp32_locator.config import load_config


class AlgorithmConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")

    def load(self, cfg):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return load_config(self.path)

    def test_partial_signal_is_filled_with_defaults(self):
        self.assertEqual(self.load({"signal": {"pl_1m": 45}})["signal"], {"pl_1m": 45.0, "pl_10m": 67.0})
        self.assertEqual(self.load({"signal": {"pl_10m": 70}})["signal"], {"pl_1m": 40.0, "pl_10m": 70.0})
        self.assertEqual(self.load({})["signal"], {"pl_1m": 40.0, "pl_10m": 67.0})

    def test_old_strength_format_is_still_migrated(self):
        with redirect_stdout(StringIO()) as out:
            cfg = self.load({"signal": {"near": 900, "far": 100}, "window_sec": 1.0})
        self.assertEqual(cfg["signal"], {"pl_1m": 40.0, "pl_10m": 67.0})
        self.assertEqual(cfg["window_sec"], 3.0)
        self.assertIn("оновлено", out.getvalue())

    def test_bad_values_are_explained(self):
        cases = [
            ({"signal": {"pl_1m": 60, "pl_10m": 50}}, "більшим за pl_1m"),
            ({"signal": {"pl_1m": 40, "pl_10m": 40}}, "більшим за pl_1m"),
            ({"signal": {"pl_1m": "40", "pl_10m": 67}}, "signal.pl_1m"),
            ({"signal": {"pl_1m": 40, "pl_10m": None}}, "signal.pl_10m"),
            ({"signal": [40, 67]}, "signal"),
            ({"window_sec": 0}, "window_sec"),
            ({"smoothing": 0}, "smoothing"),
            ({"smoothing": 1.5}, "smoothing"),
            ({"smoothing": True}, "smoothing"),
        ]
        for cfg, hint in cases:
            with self.assertRaises(ValueError, msg=cfg) as ctx:
                self.load(cfg)
            self.assertIn(hint, str(ctx.exception), cfg)

    def test_beacon_without_id_is_explained(self):
        for txs in ([{"name": "A", "x": 1, "y": 1}], [{"name": "A", "mac": "", "x": 1, "y": 1}], ["A1B2C3D4E5F6"],
                    {"mac": "A1B2C3D4E5F6"}):
            with self.assertRaises(ValueError, msg=txs) as ctx:
                self.load({"transmitters": txs})
            self.assertIn("transmitters", str(ctx.exception))

    def test_not_an_object(self):
        with self.assertRaises(ValueError):
            self.load([1, 2])


if __name__ == "__main__":
    unittest.main()
