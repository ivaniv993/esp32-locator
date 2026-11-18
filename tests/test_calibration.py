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
from esp32_locator.geo import GeoFrame
from esp32_locator.readings import Readings
from esp32_locator.session import FORMAT_VERSION, Session, session_config
from esp32_locator.solver import distance_from_loss
from esp32_locator.web import EditError, WebState

from .synthetic import default_cfg, make_session
from .test_cli import run


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


FRAME = GeoFrame(50.4501, 30.5234, 90)
WALK_BEACONS = [("A", "A" * 12, 0.0, 0.0), ("B", "B" * 12, 200.0, 0.0)]


def walk(x0=20.0, x1=180.0, y=5.0, speed=1.0, p1=38.0, n10=28.0, offsets=None, stale=None):
    """Приймач іде вздовж осі x від x0 до x1; GPS — раз на секунду, точно в його положенні; 2 пакети/с від
    кожного маяка, втрати — точно за моделлю (p1, n10) плюс власний зсув маяка з offsets.
    stale — (x, y) першої точки GPS, якщо телефон на початку віддає старе положення."""
    duration = (x1 - x0) / speed
    where = lambda t: (x0 + speed * t, y)
    header = {"type": "header", "version": FORMAT_VERSION, "mode": "field", "area": {"width": 200, "height": 10},
              "geo": {"origin": [50.4501, 30.5234], "x_bearing_deg": 90.0}, "receiver_truth": None,
              "transmitters": [{"name": n, "mac": m, "x": bx, "y": by} for n, m, bx, by in WALK_BEACONS]}
    rx = []
    for i in range(int(duration * 2) + 1):
        t = i / 2
        for k, (name, mac, bx, by) in enumerate(WALK_BEACONS):
            d = math.hypot(where(t)[0] - bx, where(t)[1] - by)
            rx.append((round(t + 0.01 * k, 3), f"{mac},{14 - loss(d, p1, n10, (offsets or {}).get(name, 0.0)):.2f},8.0,14"))
    gps = [(float(t), *FRAME.to_latlon(*(stale if stale and t == 0 else where(t))), 3.0) for t in range(int(duration) + 1)]
    return Session(path="walk.jsonl", header=header, rx=rx, gps=gps)


class WalkCalibrationTest(unittest.TestCase):
    def test_walk_recovers_model(self):
        samples = calibration.walk_samples(walk())
        r = calibration.fit_walk(samples, 20.0)
        self.assertEqual((r["pl_1m"], r["pl_10m"], r["rms"], r["notes"]), (38.0, 66.0, 0.0, []))
        self.assertTrue(r["slope_measured"])
        self.assertEqual([(b["name"], b["bias"]) for b in r["beacons"]], [("A", 0.0), ("B", 0.0)])
        self.assertTrue(all(b["loss"] == b["model"] or abs(b["loss"] - b["model"]) < 0.5 for b in r["bins"]))
        text = calibration.format_walk(r, {"pl_1m": 43.8, "pl_10m": 64.4})
        self.assertIn("pl_1m = 38 дБ, pl_10m = 66 дБ (28.0 дБ", text)
        self.assertIn("Зараз:  pl_1m = 43.8 дБ, pl_10m = 64.4 дБ (20.6 дБ)", text)

    def test_packets_near_beacon_and_stale_first_gps_point_are_skipped(self):
        s = walk(x0=1.0, x1=60.0, stale=(150.0, 5.0))          # починає за 5 м від A; перша точка GPS — стара
        samples = calibration.walk_samples(s)
        self.assertGreaterEqual(min(x["d"] for x in samples), calibration.MIN_GPS_DISTANCE)
        r = calibration.fit_walk(samples, 20.0)                # стара точка дала б відстані з помилкою 150 м
        self.assertEqual((r["pl_1m"], r["pl_10m"], r["rms"]), (38.0, 66.0, 0.0))

    def test_similar_distances_keep_previous_slope(self):
        s = walk(x0=95.0, x1=105.0, speed=0.2)                 # до обох маяків увесь час близько 100 м
        r = calibration.fit_walk(calibration.walk_samples(s), 20.0)
        self.assertFalse(r["slope_measured"])
        self.assertAlmostEqual(r["pl_10m"] - r["pl_1m"], 20.0)
        self.assertIn("надто схожі", r["notes"][0])
        # нахил чужий, але на відстані, де ходили, модель правильна
        self.assertAlmostEqual(distance_from_loss(loss(100, 38.0, 28.0), {"signal": r}), 100, delta=2)

    def test_unrealistic_slope_and_too_few_packets(self):
        r = calibration.fit_walk(calibration.walk_samples(walk(n10=5.0)), 20.0)
        self.assertFalse(r["slope_measured"])
        self.assertIn("так не буває", r["notes"][0])
        self.assertIsNone(calibration.fit_walk(calibration.walk_samples(walk())[:calibration.MIN_WALK_PACKETS - 1], 20.0))

    def test_beacon_that_deviates_is_reported(self):
        r = calibration.fit_walk(calibration.walk_samples(walk(offsets={"B": 14.0})), 20.0)
        self.assertEqual([b["bias"] for b in r["beacons"]], [-7.0, 7.0])
        self.assertTrue(any("Маяк B" in n and "більші" in n for n in r["notes"]))

    def test_walk_without_gps_gives_nothing_and_static_recording_counts(self):
        s = walk()
        s.gps = []
        self.assertEqual(calibration.walk_samples(s), [])
        static = make_session(truth=(3.5, 2.0), duration=10.0)
        samples = calibration.walk_samples(static)             # положення відоме точно — близькі відстані годяться
        self.assertEqual(len(samples), len(static.rx))
        self.assertAlmostEqual(min(x["d"] for x in samples), math.hypot(3.5, 2.0))

    def test_apply_replaces_offsets_and_page_measurements(self):
        cfg = default_cfg()
        cfg["transmitters"] = [{"mac": "A", "x": 0, "y": 0, "pl_offset": 2.0}, {"mac": "B", "x": 1, "y": 0}]
        cfg["calibration"] = [{"mac": "A", "d": 1.0, "loss": 45.0, "n": 20}]
        self.assertEqual(calibration.apply_walk(cfg, {"pl_1m": 38.0, "pl_10m": 66.0}), (1, 1))
        self.assertEqual((cfg["signal"], cfg["calibration"]), ({"pl_1m": 38.0, "pl_10m": 66.0}, []))
        self.assertNotIn("pl_offset", cfg["transmitters"][0])

    def test_command_line(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg_path, rec = os.path.join(tmp.name, "config.json"), os.path.join(tmp.name, "walk.jsonl")
        s = walk()
        with open(rec, "w", encoding="utf-8") as f:
            f.write(json.dumps(s.header) + "\n")
            for t, line in s.rx:
                f.write(json.dumps({"type": "rx", "t": t, "line": line}) + "\n")
            for t, lat, lon, acc in s.gps:
                f.write(json.dumps({"type": "gps", "t": t, "lat": lat, "lon": lon, "acc": acc}) + "\n")
        before = {"transmitters": [{"name": "A", "mac": "A" * 12, "x": 0, "y": 0, "pl_offset": 2.0}],
                  "calibration": [{"mac": "A" * 12, "d": 1.0, "loss": 45.0, "n": 20}]}
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(before, f)
        code, out = run("--config", cfg_path, "--calibrate-from", rec)
        self.assertEqual(code, 0)
        self.assertIn("pl_1m = 38 дБ, pl_10m = 66 дБ", out)
        self.assertIn("config.json не змінено", out)
        with open(cfg_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), before)                 # без --apply файл не чіпаємо
        code, out = run("--config", cfg_path, "--calibrate-from", tmp.name, "--apply")
        self.assertEqual(code, 0)
        self.assertIn("Прибрано зсуви маяків (pl_offset): 1. Прибрано виміри з веб-сторінки: 1.", out)
        with open(cfg_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual((saved["signal"], saved["calibration"]), ({"pl_1m": 38.0, "pl_10m": 66.0}, []))
        self.assertNotIn("pl_offset", saved["transmitters"][0])
        self.assertEqual(run("--config", cfg_path, "--apply"), (1, "--apply працює лише з --calibrate-from"))
        s.gps = []
        with open(rec, "w", encoding="utf-8") as f:
            f.write(json.dumps(s.header) + "\n" + json.dumps({"type": "rx", "t": 1.0, "line": s.rx[0][1]}) + "\n")
        code, out = run("--config", cfg_path, "--calibrate-from", rec)
        self.assertEqual(code, 1)
        self.assertIn("немає GPS телефона", out)
        self.assertIn("Замало даних", out)


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
                                     unknown=[], rejected=set())

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
        for action in (viewer.calibration_cancel, viewer.calibration_delete, viewer.calibration_reset):
            with self.assertRaises(EditError, msg=action.__name__):
                action({"index": 0})


if __name__ == "__main__":
    unittest.main()
