"""Протокол, вікно вимірів, розв’язувач і статистика."""
import math
import unittest

from esp32_locator.metrics import bias, percentile, spread, summarize
from esp32_locator.protocol import norm_mac, parse_line
from esp32_locator.readings import Readings
import random

from esp32_locator.solver import (Locator, check_geometry, distance_from_loss, error_estimate, solve,
                                  solve_consistent)

from .synthetic import default_cfg


class ProtocolTest(unittest.TestCase):
    def test_full_line(self):
        self.assertEqual(parse_line("A1B2C3D4E5F6,-67.5,9.2,14", 10), ("A1B2C3D4E5F6", -67.5, 9.2, 14.0))

    def test_optional_fields_and_mac_formats(self):
        self.assertEqual(parse_line("a1:b2:c3:d4:e5:f6, -70", 14), ("A1B2C3D4E5F6", -70.0, None, 14.0))

    def test_service_lines_are_ignored(self):
        for line in ("# listening…", "chipId,rssi_dBm,snr_dB,txPower_dBm", "WARN x", "", "A1B2C3D4E5F6,5,1,1"):
            self.assertIsNone(parse_line(line, 14), line)

    def test_norm_mac(self):
        self.assertEqual(norm_mac("a1-b2-c3-d4-e5-f6"), "A1B2C3D4E5F6")
        self.assertIsNone(norm_mac("A1B2"))
        self.assertIsNone(norm_mac("not a mac"))


class ReadingsTest(unittest.TestCase):
    def test_median_and_window_with_explicit_time(self):
        r = Readings(3.0)
        for ts, rssi in ((0.0, -90.0), (1.0, -70.0), (2.0, -72.0), (2.5, -71.0)):
            r.add("A" * 12, rssi, 5.0, 14.0, ts=ts)
        snap = r.snapshot(now=3.0)["A" * 12]
        self.assertEqual(snap["n"], 4)
        self.assertEqual(snap["rssi"], -71.5)
        self.assertEqual(snap["pl"], 14.0 + 71.5)
        snap = r.snapshot(now=3.4)["A" * 12]          # пакет з ts=0 вийшов за вікно
        self.assertEqual(snap["n"], 3)
        self.assertEqual(r.snapshot(now=10.0), {})


class SolverTest(unittest.TestCase):
    def test_exact_distances_give_exact_position(self):
        truth, beacons = (3.5, 2.0), [(0, 0), (10, 0), (5, 8)]
        pts = [(x, y, math.hypot(truth[0] - x, truth[1] - y)) for x, y in beacons]
        x, y, rms = solve(pts)
        self.assertAlmostEqual(x, truth[0], places=3)
        self.assertAlmostEqual(y, truth[1], places=3)
        self.assertLess(rms, 1e-3)

    def test_no_points(self):
        self.assertIsNone(solve([]))

    def test_distance_model_hits_calibration_points(self):
        cfg = default_cfg()
        self.assertAlmostEqual(distance_from_loss(40.0, cfg), 1.0)
        self.assertAlmostEqual(distance_from_loss(67.0, cfg), 10.0)


SLOPE = 27.0
CORNERS = [(0, 0), (10, 0), (0, 8), (10, 8), (5, 0)]


def exact(truth, beacons):
    return [(x, y, math.hypot(truth[0] - x, truth[1] - y)) for x, y in beacons]


def off_db(pt, db):
    """Маяк, відстань до якого «бреше» на db дБ втрат (мінус — здається ближчим)."""
    x, y, d = pt
    return x, y, d * 10 ** (db / SLOPE)


class ConsistencyTest(unittest.TestCase):
    def test_lying_beacon_is_rejected(self):
        truth = (3.0, 4.0)
        for liar in range(len(CORNERS)):
            for db in (-20.0, 20.0):
                pts = exact(truth, CORNERS)
                pts[liar] = off_db(pts[liar], db)
                x, y, rms, rejected = solve_consistent(pts, SLOPE)
                self.assertEqual(rejected, [liar], (liar, db))
                self.assertAlmostEqual(x, truth[0], places=2)
                self.assertAlmostEqual(y, truth[1], places=2)

    def test_consistent_beacons_are_kept(self):
        pts = [off_db(p, db) for p, db in zip(exact((7.0, 2.5), CORNERS), (3, -3, 2, -2, 1))]
        self.assertEqual(solve_consistent(pts, SLOPE)[3], [])

    def test_three_beacons_are_never_reduced(self):
        pts = exact((3.0, 4.0), CORNERS[:3])
        pts[0] = off_db(pts[0], -20.0)
        self.assertEqual(solve_consistent(pts, SLOPE)[3], [])
        self.assertIsNone(solve_consistent([], SLOPE))

    def test_threshold(self):
        pts = exact((3.0, 4.0), CORNERS)
        pts[1] = off_db(pts[1], 10.0)
        self.assertEqual(solve_consistent(pts, SLOPE, max_db=8.0)[3], [1])
        self.assertEqual(solve_consistent(pts, SLOPE, max_db=12.0)[3], [])

    def test_locator_marks_rejected_beacon(self):
        cfg = default_cfg()
        cfg["transmitters"] = [{"name": str(i), "mac": f"{i:012X}", "x": float(x), "y": float(y)}
                               for i, (x, y) in enumerate(CORNERS[:4])]
        truth = (3.0, 4.0)
        readings = Readings(3.0)
        for t in cfg["transmitters"]:
            loss = 40.0 + SLOPE * math.log10(math.hypot(truth[0] - t["x"], truth[1] - t["y"]))
            txp = 14.0 - 20.0 if t["name"] == "2" else 14.0      # маяк 2 «каже», що він на 20 дБ слабший
            readings.add(t["mac"], 14.0 - loss, 8.0, txp, ts=0.0)
        loc = Locator(cfg, readings)
        loc.update(now=1.0)
        self.assertEqual(loc.rejected, {"000000000002"})
        self.assertAlmostEqual(loc.pos[0], truth[0], places=2)
        self.assertAlmostEqual(loc.pos[1], truth[1], places=2)
        self.assertEqual(loc.heard, 4)

        cfg["outliers"]["enabled"] = False
        loc = Locator(cfg, readings)
        loc.update(now=1.0)
        self.assertEqual(loc.rejected, set())
        self.assertGreater(math.hypot(loc.pos[0] - truth[0], loc.pos[1] - truth[1]), 0.5)

class LocatorMinBeaconsTest(unittest.TestCase):
    """Менше 3 маяків — положення невідоме; після перерви — без згладжування від старої точки."""
    BEACONS = [(0.0, 0.0), (60.0, 0.0), (30.0, 60.0)]

    def setUp(self):
        self.cfg = default_cfg()
        self.cfg["area"] = {"width": 60, "height": 60}
        self.cfg["transmitters"] = [{"name": str(i), "mac": f"{i + 1:012X}", "x": x, "y": y}
                                    for i, (x, y) in enumerate(self.BEACONS)]
        self.readings = Readings(3.0)
        self.loc = Locator(self.cfg, self.readings)

    def feed(self, truth, t, macs=None):
        for b in self.cfg["transmitters"]:
            if macs is None or b["mac"] in macs:
                loss = 40.0 + SLOPE * math.log10(math.hypot(truth[0] - b["x"], truth[1] - b["y"]))
                self.readings.add(b["mac"], 14.0 - loss, 8.0, 14.0, ts=t)

    def test_fewer_than_three_beacons_give_no_position(self):
        for macs in ({"000000000001"}, {"000000000001", "000000000002"}):
            readings = Readings(3.0)
            loc = Locator(self.cfg, readings)
            self.readings = readings
            self.feed((30.0, 25.0), 0.0, macs)
            loc.update(now=0.5)
            self.assertEqual(loc.heard, len(macs))
            self.assertIsNone(loc.pos)
            self.assertIsNone(loc.rms)

    def test_after_dropout_first_position_is_not_smoothed(self):
        a, b = (30.0, 25.0), (45.0, 15.0)
        for k in range(10):                       # стоїть у точці a — положення встановилось
            self.feed(a, k * 0.5)
            self.loc.update(now=k * 0.5)
        self.assertAlmostEqual(self.loc.pos[0], a[0], places=2)
        for k in range(10, 20):                   # 5 с чути лише один маяк; приймач переносять у b
            self.feed(b, k * 0.5, {"000000000001"})
            self.loc.update(now=k * 0.5)
        self.assertEqual(self.loc.heard, 1)
        self.assertIsNone(self.loc.pos)
        self.feed(b, 10.0)                        # знову чути всі три
        self.loc.update(now=10.0)
        self.assertEqual(self.loc.heard, 3)
        self.assertAlmostEqual(self.loc.pos[0], b[0], places=1)   # одразу b, а не точка між a і b
        self.assertAlmostEqual(self.loc.pos[1], b[1], places=1)

    def test_moving_a_beacon_restarts_smoothing(self):
        for k in range(10):                       # положення встановилось
            self.feed((30.0, 25.0), k * 0.5)
            self.loc.update(now=k * 0.5)
            self.assertFalse(self.loc.moved)
        # координати маяка 3 виправили (екран, сторінка чи API) — розв’язок за тими самими пакетами інший
        self.cfg["transmitters"][2]["y"] = 40.0
        self.loc.update(now=4.6)
        self.assertTrue(self.loc.moved)
        fresh = Locator(self.cfg, self.readings)  # що дав би розв’язок без жодної історії
        fresh.update(now=4.6)
        self.assertGreater(math.hypot(fresh.pos[0] - 30.0, fresh.pos[1] - 25.0), 1.0)
        self.assertAlmostEqual(self.loc.pos[0], fresh.pos[0], places=6)   # без «доїжджання» від старої точки
        self.assertAlmostEqual(self.loc.pos[1], fresh.pos[1], places=6)
        self.loc.update(now=4.8)
        self.assertFalse(self.loc.moved)


class GeometryTest(unittest.TestCase):
    """Перевірка розташування маяків: дзеркальне положення й оцінка похибки."""
    TRUTH = (5.0, 4.0)

    def locator(self, beacons, truth=TRUTH, sigma=0.0, rnd=None):
        cfg = default_cfg()
        cfg["area"] = {"width": 60, "height": 60}
        cfg["transmitters"] = [{"name": str(i), "mac": f"{i + 1:012X}", "x": float(x), "y": float(y)}
                               for i, (x, y) in enumerate(beacons)]
        readings = Readings(3.0)
        for t in cfg["transmitters"]:
            d = max(math.hypot(truth[0] - t["x"], truth[1] - t["y"]), 0.5)
            loss = 40.0 + SLOPE * math.log10(d) + (rnd.gauss(0, sigma) if rnd else 0.0)
            readings.add(t["mac"], 14.0 - loss, 8.0, 14.0, ts=0.0)
        loc = Locator(cfg, readings)
        loc.update(now=0.5)
        return loc

    def test_beacons_on_one_line_give_mirror_warning(self):
        # раніше: з (0;0), (5;0), (10;0) розв’язок застрягав на лінії — (7,25; 0), а з (5; 0,5) замість
        # середнього маяка — давав дзеркальну точку (5; −3,2) без жодного попередження
        for beacons, mirror in (([(0, 0), (5, 0), (10, 0)], (5.0, -4.0)), ([(0, 0), (5, 0.5), (10, 0)], (5.0, -3.16))):
            loc = self.locator(beacons)
            found = {tuple(round(v, 1) for v in loc.pos), tuple(round(v, 1) for v in loc.alt)}
            self.assertEqual(found, {self.TRUTH, tuple(round(v, 1) for v in mirror)}, beacons)
            self.assertTrue(loc.warnings)
            self.assertIn("одній лінії", loc.warnings[0])
            self.assertGreaterEqual(loc.err, math.hypot(loc.pos[0] - loc.alt[0], loc.pos[1] - loc.alt[1]) - 1e-6)

    def test_mirror_choice_follows_previous_position(self):
        pts = exact(self.TRUTH, [(0, 0), (5, 0.5), (10, 0)])
        for prefer in ((5.0, 3.0), (5.0, -3.0)):
            x, y, alt = check_geometry(5.0, 4.0, pts, SLOPE, 4.0, prefer=prefer)
            self.assertIsNotNone(alt)
            self.assertEqual(y > 0, prefer[1] > 0, prefer)

    def test_spread_beacons_give_no_warning(self):
        for beacons in ([(0, 0), (10, 0), (0, 8), (10, 8)], [(0, 0), (10, 0), (5, 8)]):
            loc = self.locator(beacons)
            self.assertIsNone(loc.alt)
            self.assertEqual(loc.warnings, [])
            self.assertLess(loc.hdop, 1.5)
            self.assertAlmostEqual(loc.pos[0], self.TRUTH[0], places=3)

    def test_error_estimate(self):
        pts = exact((5.0, 4.0), [(0, 0), (10, 0), (0, 8), (10, 8)])
        err, hdop = error_estimate(5.0, 4.0, pts, SLOPE, 4.0)
        # 4 маяки на 6,4 м: σ_d = 6,4 · ln10 / 27 · 4 ≈ 2,2 м; HᵀH ≈ 2·I → похибка ≈ σ_d
        self.assertAlmostEqual(err, 6.403 * math.log(10) / SLOPE * 4.0, delta=0.1)
        self.assertAlmostEqual(hdop, 1.0, delta=0.05)
        self.assertAlmostEqual(error_estimate(5.0, 4.0, pts, SLOPE, 8.0)[0], 2 * err, places=6)
        # приймач на продовженні лінії маяків: упоперек неї положення не визначене
        err, hdop = error_estimate(20.0, 0.0, exact((20.0, 0.0), [(0, 0), (5, 0), (10, 0)]), SLOPE, 4.0)
        self.assertEqual((err, hdop), (math.inf, math.inf))

    def test_error_estimate_matches_real_errors(self):
        # з шумом, рівним sigma_db, справжня похибка не більша за оцінку приблизно в 2 випадках з 3
        rnd, inside, n = random.Random(7), 0, 300
        for _ in range(n):
            truth = (rnd.uniform(0, 10), rnd.uniform(0, 8))
            loc = self.locator([(0, 0), (10, 0), (0, 8), (10, 8)], truth, sigma=4.0, rnd=rnd)
            inside += math.hypot(loc.pos[0] - truth[0], loc.pos[1] - truth[1]) <= loc.err
        self.assertTrue(0.5 <= inside / n <= 0.85, inside / n)


class MetricsTest(unittest.TestCase):
    def test_percentile(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertAlmostEqual(percentile([0, 10], 95), 9.5)
        self.assertIsNone(percentile([], 50))

    def test_summarize(self):
        m = summarize([1.0, 3.0], ticks=4)
        self.assertEqual((m["fixes"], m["coverage"], m["mean"], m["max"]), (2, 0.5, 2.0, 3.0))
        self.assertAlmostEqual(m["rmse"], math.sqrt(5))
        self.assertIsNone(summarize([], 4)["mean"])

    def test_bias_and_spread(self):
        pts = [(1.0, 2.0), (3.0, 2.0)]
        self.assertEqual(bias(pts, (1.0, 1.0)), (1.0, 1.0))
        self.assertEqual(spread(pts), 1.0)


if __name__ == "__main__":
    unittest.main()
