"""Калібрування: підгонка моделі за вимірами і вимір через веб-сторінку."""
import json
import math
import os
import tempfile
import types
import unittest
from unittest import mock

from esp32_locator import calibration
from esp32_locator.config import load_config
from esp32_locator.readings import Readings
from esp32_locator.session import Session, session_config
from esp32_locator.solver import distance_from_loss
from esp32_locator.web import EditError, WebState

from .synthetic import default_cfg


def loss(d, p1=45.0, n10=30.0, offset=0.0):
    return p1 + offset + n10 * math.log10(d)


class FitTest(unittest.TestCase):
    def test_two_distances_per_beacon_recover_model_and_offsets(self):
        samples = [{"mac": m, "d": d, "loss": loss(d, offset=o)}
                   for m, o in (("A", 2.0), ("B", -2.0)) for d in (1.0, 10.0)]
        r = calibration.fit(samples, 27.0)
        self.assertEqual((r["pl_1m"], r["pl_10m"]), (45.0, 75.0))
        self.assertEqual(r["offsets"], {"A": 2.0, "B": -2.0})
        self.assertTrue(r["slope_measured"])
        self.assertEqual((r["rms"], r["notes"]), (0.0, []))

    def test_all_at_one_metre_keeps_previous_slope(self):
        samples = [{"mac": "A", "d": 1.0, "loss": 43.0}, {"mac": "B", "d": 1.0, "loss": 47.0}]
        r = calibration.fit(samples, 27.0)
        self.assertEqual((r["pl_1m"], r["pl_10m"]), (45.0, 72.0))
        self.assertEqual(r["offsets"], {"A": -2.0, "B": 2.0})
        self.assertFalse(r["slope_measured"])
        self.assertIn("іншій відстані", r["notes"][0])

    def test_each_beacon_once_at_different_distance(self):
        samples = [{"mac": "A", "d": 1.0, "loss": loss(1)}, {"mac": "B", "d": 10.0, "loss": loss(10)}]
        r = calibration.fit(samples, 27.0)
        self.assertEqual((r["pl_1m"], r["pl_10m"], r["offsets"]), (45.0, 75.0, {}))
        self.assertIn("не враховано", r["notes"][0])

    def test_unrealistic_slope_is_rejected(self):
        samples = [{"mac": "A", "d": 1.0, "loss": 45.0}, {"mac": "A", "d": 10.0, "loss": 47.0}]   # 2 дБ на декаду
        r = calibration.fit(samples, 27.0)
        self.assertFalse(r["slope_measured"])
        self.assertEqual(r["pl_10m"] - r["pl_1m"], 27.0)
        self.assertIn("так не буває", r["notes"][0])
        self.assertIsNone(calibration.fit([], 27.0))

    def test_apply_and_distance_with_offset(self):
        cfg = default_cfg()
        cfg["transmitters"] = [{"mac": "A", "x": 0, "y": 0, "pl_offset": 9.0}, {"mac": "B", "x": 1, "y": 0}]
        calibration.apply(cfg, {"pl_1m": 45.0, "pl_10m": 75.0, "offsets": {"B": 3.0}})
        self.assertEqual(cfg["signal"], {"pl_1m": 45.0, "pl_10m": 75.0})
        self.assertNotIn("pl_offset", cfg["transmitters"][0])        # без виміру — без зсуву
        self.assertEqual(cfg["transmitters"][1]["pl_offset"], 3.0)
        self.assertAlmostEqual(distance_from_loss(loss(5, offset=3.0), cfg, 3.0), 5.0)

    def test_replay_uses_current_offsets(self):
        cfg = default_cfg()
        cfg["transmitters"] = [{"mac": "A", "x": 0, "y": 0, "pl_offset": 1.5}, {"mac": "B", "x": 1, "y": 0}]
        header = {"area": {"width": 5, "height": 5}, "receiver_truth": {"x": 1, "y": 1},
                  "transmitters": [{"mac": "A", "x": 0, "y": 0}, {"mac": "B", "x": 1, "y": 0, "pl_offset": 4.0},
                                   {"mac": "C", "x": 2, "y": 0, "pl_offset": 2.0}]}
        txs = session_config(cfg, Session(path="x.jsonl", header=header))["transmitters"]
        self.assertEqual([t.get("pl_offset") for t in txs], [1.5, None, 2.0])


class WebCalibrationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "config.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"mode": "field", "web": {"pin": "1"}, "transmitters": [
                {"name": "A", "mac": "A" * 12, "lat": 50.4501, "lon": 30.5234},
                {"name": "B", "mac": "B" * 12, "lat": 50.4502, "lon": 30.5236}]}, f)
        self.cfg = load_config(self.path)
        self.readings = Readings(3.0, keep_recent=200)
        self.ws = WebState(self.cfg, self.path, self.readings, types.SimpleNamespace(status="ok"), pin="1")
        self.clock = [1000.0]
        patcher = mock.patch("time.monotonic", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def loc(self):
        return types.SimpleNamespace(pos=None, rms=None, heard=0, rows=[(t, None, None) for t in self.cfg["transmitters"]],
                                     unknown=[])

    def measure(self, mac, d, packets, offset=0.0):
        self.ws.calibration_measure({"pin": "1", "mac": mac, "distance": d})
        for i in range(packets):
            self.readings.add(mac, 14 - loss(d, offset=offset) + (0.5 if i % 2 else -0.5), 8.0, 14.0)
            self.readings.add("C" * 12, -80.0, 1.0, 14.0)               # інший маяк у вимір не потрапляє
        self.clock[0] += 5
        self.ws.on_update(self.loc())
        state = json.loads(self.ws.state_json())["calibration"]
        self.assertEqual((state["job"]["packets"], state["job"]["distance"]), (packets, d))
        self.clock[0] += 6
        self.ws.on_update(self.loc())
        return json.loads(self.ws.state_json())["calibration"]

    def test_measurements_update_model_in_current_session(self):
        self.assertIsNone(json.loads(self.ws.state_json() or b"{}").get("calibration"))
        with self.assertRaises(PermissionError):
            self.ws.calibration_measure({"mac": "A" * 12, "distance": 1})
        for bad in ({"pin": "1", "mac": "D" * 12, "distance": 1}, {"pin": "1", "mac": "A" * 12, "distance": "x"},
                    {"pin": "1", "mac": "A" * 12, "distance": 0}):
            with self.assertRaises(EditError):
                self.ws.calibration_measure(bad)

        state = self.measure("A" * 12, 1.0, 12, offset=1.0)
        self.assertIsNone(state["job"])
        self.assertIn("Модель оновлено", state["last"]["ok"])
        self.assertEqual(state["samples"][0]["loss"], 46.0)
        self.measure("A" * 12, 10.0, 12, offset=1.0)
        self.measure("B" * 12, 1.0, 12, offset=-1.0)
        state = self.measure("B" * 12, 10.0, 12, offset=-1.0)
        self.assertEqual(self.cfg["signal"], {"pl_1m": 45.0, "pl_10m": 75.0})   # діє одразу в поточному cfg
        self.assertEqual([t.get("pl_offset") for t in self.cfg["transmitters"]], [1.0, -1.0])
        self.assertTrue(state["fit"]["slope_measured"])
        self.assertAlmostEqual(state["samples"][1]["model_d"], 10.0, places=1)

        saved = load_config(self.path)                                 # і збережено
        self.assertEqual((saved["signal"]["pl_1m"], len(saved["calibration"])), (45.0, 4))
        self.assertEqual(saved["transmitters"][0]["pl_offset"], 1.0)

        self.ws.calibration_delete({"pin": "1", "index": 3})
        self.ws.calibration_delete({"pin": "1", "index": 1})
        self.assertEqual(self.cfg["signal"]["pl_10m"] - self.cfg["signal"]["pl_1m"], 27.0)   # нахил — за замовчуванням
        with self.assertRaises(EditError):
            self.ws.calibration_delete({"pin": "1", "index": 7})
        self.ws.calibration_reset({"pin": "1"})
        self.assertEqual((self.cfg["signal"], self.cfg["calibration"]), ({"pl_1m": 40.0, "pl_10m": 67.0}, []))
        self.assertNotIn("pl_offset", load_config(self.path)["transmitters"][0])

    def test_too_few_packets_and_busy(self):
        self.ws.calibration_measure({"pin": "1", "mac": "A" * 12, "distance": 2})
        with self.assertRaises(EditError):
            self.ws.calibration_measure({"pin": "1", "mac": "B" * 12, "distance": 2})
        self.readings.add("A" * 12, -50.0, 8.0, 14.0)
        self.clock[0] += 11
        self.ws.on_update(self.loc())
        state = json.loads(self.ws.state_json())["calibration"]
        self.assertIn("прийнято пакетів: 1", state["last"]["error"])
        self.assertEqual(state["samples"], [])
        self.ws.calibration_measure({"pin": "1", "mac": "B" * 12, "distance": 2})
        self.ws.calibration_cancel({"pin": "1"})
        self.assertIsNone(self.ws.cal_job)

    def test_viewer_cannot_calibrate(self):
        viewer = WebState(self.cfg, None, self.readings, types.SimpleNamespace(status="ok"))
        with self.assertRaises(EditError):
            viewer.calibration_measure({"mac": "A" * 12, "distance": 1})


if __name__ == "__main__":
    unittest.main()
