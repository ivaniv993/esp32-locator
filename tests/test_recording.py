"""Запис з веб-сторінки: приймач носять, фактичне положення — трек GPS телефона."""
import json
import math
import os
import tempfile
import types
import unittest
from unittest import mock

from esp32_locator.config import load_config
from esp32_locator.evaluate import evaluate_session, format_report
from esp32_locator.geo import GeoFrame, frame_from_config
from esp32_locator.readings import Readings
from esp32_locator.session import FORMAT_VERSION, Recorder, Session, describe, load_session
from esp32_locator.web import EditError, WebState

from .synthetic import default_cfg

KYIV = GeoFrame(50.4501, 30.5234, 90)
GEO = {"origin": [50.4501, 30.5234], "x_bearing_deg": 90.0}
BEACONS = [("A", "AAAAAAAAAAAA", 0.0, 0.0), ("B", "BBBBBBBBBBBB", 30.0, 0.0), ("C", "CCCCCCCCCCCC", 15.0, 25.0)]


def walk_session(duration=60.0, speed=0.4, gps_noise=0.0, gps_gap=None, path="walk.jsonl"):
    """Приймач іде вздовж осі x з (5; 8) зі швидкістю speed м/с; GPS — раз на секунду, точно в його положенні."""
    header = {
        "type": "header", "version": FORMAT_VERSION, "started_at": "2026-01-01T00:00:00", "note": "walk",
        "mode": "field", "area": {"width": 30, "height": 25}, "geo": GEO,
        "transmitters": [{"name": n, "mac": m, "x": x, "y": y} for n, m, x, y in BEACONS],
        "receiver_truth": None,
    }
    where = lambda t: (5.0 + speed * t, 8.0)
    rx = []
    for i in range(int(duration * 2)):
        t = i / 2
        for k, (_, mac, bx, by) in enumerate(BEACONS):
            x, y = where(t)
            d = max(math.hypot(x - bx, y - by), 0.1)
            rx.append((round(t + 0.01 * k, 3), f"{mac},{14 - (40 + 27 * math.log10(d)):.1f},8.0,14"))
    gps = []
    for t in range(int(duration) + 1):
        if gps_gap and gps_gap[0] <= t <= gps_gap[1]:
            continue
        lat, lon = KYIV.to_latlon(*where(t))
        gps.append((float(t), lat, lon, 3.0))
    return Session(path=path, header=header, rx=rx, gps=gps)


class TrackTest(unittest.TestCase):
    def test_truth_follows_gps_track(self):
        s = walk_session()
        self.assertTrue(s.moving)
        self.assertIsNone(s.truth)
        x, y = s.truth_at(10.5)                                     # між точками — інтерполяція
        self.assertAlmostEqual(x, 5 + 0.4 * 10.5, places=3)
        self.assertAlmostEqual(y, 8.0, places=3)
        self.assertIsNotNone(s.truth_at(60.5))                     # впритул після останньої точки
        self.assertIsNone(s.truth_at(65))
        self.assertEqual(s.gps_accuracy(), 3.0)
        self.assertEqual(s.distances()["AAAAAAAAAAAA"], None)

    def test_gap_and_inaccurate_points_are_skipped(self):
        s = walk_session(gps_gap=(20, 30))
        self.assertIsNone(s.truth_at(25))                           # 11 с без GPS — не вигадуємо положення
        s.gps.append((70.0, 50.0, 30.0, 80.0))                      # «точність» 80 м — не береться
        s.__dict__.pop("track", None), s.__dict__.pop("_track_t", None)
        self.assertNotIn(70.0, [p[0] for p in s.track])

    def test_evaluate_walk_against_gps(self):
        r = evaluate_session(walk_session(), default_cfg())
        m = r.metrics()
        self.assertGreater(m["coverage"], 0.95)
        self.assertIsNone(m["spread"])
        self.assertLess(m["median"], 1.5)                           # без шуму — лише відставання від руху
        self.assertGreater(r.lag[0], 0)                             # визначене положення відстає від руху
        self.assertLess(r.lag[1], m["mean"])
        report = format_report([r], default_cfg())
        self.assertIn("Приймач носили", report)
        self.assertIn("с тому", report)
        self.assertIn("рухався", describe(walk_session()))

    def test_walk_without_gps_cannot_be_evaluated(self):
        s = walk_session()
        s.gps = []
        r = evaluate_session(s, default_cfg())
        self.assertEqual((r.ticks, r.metrics()["mean"]), (0, None))
        self.assertIn("порівнювати нема з чим", format_report([r], default_cfg()))
        self.assertIn("без GPS", describe(s))


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = os.path.join(self.tmp.name, "config.json")
        txs = [{"name": n, "mac": m, "lat": KYIV.to_latlon(x, y)[0], "lon": KYIV.to_latlon(x, y)[1]} for n, m, x, y in BEACONS]
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"mode": "field", "radio": {"sf": 7}, "web": {"pin": "1234"}, "transmitters": txs}, f)
        self.cfg = load_config(self.cfg_path)
        self.dir = os.path.join(self.tmp.name, "rec")
        self.recorder = Recorder(self.cfg, self.dir)
        self.readings = Readings(3.0, keep_recent=20)
        self.ws = WebState(self.cfg, self.cfg_path, self.readings, types.SimpleNamespace(status="ok"),
                           pin="1234", recorder=self.recorder)

    def loc(self, pos=(10.0, 12.0)):
        rows = [(t, None, None) for t in self.cfg["transmitters"]]
        return types.SimpleNamespace(pos=pos, rms=0.5, heard=3, rows=rows, unknown=[])

    def test_record_from_page_with_phone_gps(self):
        with self.assertRaises(PermissionError):
            self.ws.record_start({})
        res = self.ws.record_start({"pin": "1234", "note": "обхід"})
        self.assertTrue(self.recorder.active)
        with self.assertRaises(EditError):                          # маяки під час запису не змінюються
            self.ws.save_beacon({"pin": "1234", "mac": "A" * 12, "lat": 50.45, "lon": 30.52})
        self.ws.on_update(self.loc())
        state = json.loads(self.ws.state_json())
        self.assertFalse(state["editable"])
        self.assertEqual(state["recording"]["id"], res["id"])

        line = "AAAAAAAAAAAA,-60.0,8.0,14"
        self.recorder.write_rx(line)
        self.recorder.on_update(self.loc())
        lat, lon = frame_from_config(self.cfg["geo"]).to_latlon(11.0, 12.0)    # сітка мапи — з відступом 10 м
        out = self.ws.post_gps({"pin": "1234", "points": [
            {"lat": lat, "lon": lon, "acc": 4.2, "age": 1500},
            {"lat": "bad", "lon": 1},                                # зіпсовану точку пропускаємо
            {"lat": lat, "lon": lon, "acc": 4.0, "age": 999999}]})   # надто стара
        self.assertEqual(out, {"ok": True, "recording": True, "saved": 1})
        self.ws.on_update(self.loc())
        phone = json.loads(self.ws.state_json())["phone"]
        self.assertAlmostEqual(phone["x"], 11.0, places=2)
        self.assertEqual(phone["acc"], 4.2)
        self.assertGreaterEqual(phone["age"], 1.4)

        msg = self.ws.record_stop({"pin": "1234"})["message"]
        self.assertIn("пакетів 1, точок GPS 1", msg)
        self.assertFalse(self.recorder.active)
        self.assertEqual(self.ws.post_gps({"pin": "1234", "points": []})["recording"], False)

        files = os.listdir(self.dir)
        self.assertEqual(len(files), 1)
        s = load_session(os.path.join(self.dir, files[0]))
        self.assertTrue(s.moving)
        self.assertEqual(s.header["note"], "обхід")
        self.assertEqual(s.header["radio"]["sf"], 7)
        self.assertNotIn("distance", s.header["transmitters"][0])
        self.assertEqual(s.rx[0][1], line)
        self.assertEqual(len(s.gps), 1)
        self.assertEqual(s.gps[0][0], 0.0)                          # 1,5 с тому — раніше за початок запису
        self.assertEqual(s.est[0]["x"], 10.0)
        self.assertIn("lat", s.est[0])

    def test_empty_recording_is_removed_and_double_start_refused(self):
        self.ws.record_start({"pin": "1234"})
        with self.assertRaises(EditError):
            self.ws.record_start({"pin": "1234"})
        self.assertIn("порожній", self.ws.record_stop({"pin": "1234"})["message"])
        self.assertEqual(os.listdir(self.dir), [])
        with self.assertRaises(EditError):
            self.ws.record_stop({"pin": "1234"})

    def test_shutdown_saves_recording_then_powers_off(self):
        calls = []
        self.ws.poweroff = lambda check=False: calls.append(check) or True
        with self.assertRaises(PermissionError):
            self.ws.shutdown({})
        self.ws.record_start({"pin": "1234"})
        self.recorder.write_rx("AAAAAAAAAAAA,-60.0,8.0,14")
        with mock.patch("time.sleep"), mock.patch("threading.Thread") as thread:
            msg = self.ws.shutdown({"pin": "1234"})["message"]
            thread.call_args.kwargs["target"]()                    # те, що потік зробив би через 2 с
        self.assertIn("Запис збережено", msg)
        self.assertFalse(self.recorder.active)
        self.assertEqual(len(os.listdir(self.dir)), 1)
        self.assertEqual(calls, [True, False])                     # спершу перевірка дозволу, потім вимкнення

        self.ws.poweroff = lambda check=False: False               # служба без дозволу (sudoers)
        with self.assertRaises(EditError) as ctx:
            self.ws.shutdown({"pin": "1234"})
        self.assertIn("install-raspberry.sh", str(ctx.exception))
        viewer = WebState(self.cfg, None, self.readings, types.SimpleNamespace(status="ok"))
        with self.assertRaises(EditError):
            viewer.shutdown({})

    def test_no_recorder_in_replay(self):
        viewer = WebState(self.cfg, None, self.readings, types.SimpleNamespace(status="ok"))
        with self.assertRaises(EditError):
            viewer.record_start({})
        viewer.on_update(self.loc())
        self.assertIsNone(json.loads(viewer.state_json())["recording"])

    def test_replay_of_walk_moves_phone_marker(self):
        where = [(5.0, 8.0)]
        viewer = WebState(self.cfg, None, self.readings, types.SimpleNamespace(status="ok"), truth=lambda: where[0])
        with mock.patch("time.monotonic", side_effect=[100.0, 100.2, 100.4, 100.6]):
            for x in (5.0, 5.2, 5.4, 5.6):                        # перерахунок раз на 0,2 с, слід — раз на 0,5 с
                where[0] = (x, 8.0)
                viewer.on_update(self.loc())
        state = json.loads(viewer.state_json())
        self.assertIsNone(state["truth"])
        self.assertEqual((state["phone"]["x"], state["phone"]["replay"]), (5.6, True))
        self.assertEqual(state["phone_trail"], [[5.0, 8.0], [5.6, 8.0]])


if __name__ == "__main__":
    unittest.main()
