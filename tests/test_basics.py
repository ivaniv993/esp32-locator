"""Протокол, вікно вимірів, розв’язувач і статистика."""
import math
import unittest

from esp32_locator.metrics import bias, percentile, spread, summarize
from esp32_locator.protocol import norm_mac, parse_line
from esp32_locator.readings import Readings
from esp32_locator.solver import distance_from_loss, solve

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
