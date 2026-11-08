"""Прив’язка мапи до широти й довготи."""
import math
import unittest

from esp32_locator.geo import GeoFrame, field_frame, format_latlon, frame_from_config, parse_latlon

KYIV = (50.4501, 30.5234)
# довжина 1° на широті 50° за еліпсоїдом WGS84 (довідкові таблиці), м
DEG_LAT_50, DEG_LON_50 = 111229.0, 71696.5


class GeoFrameTest(unittest.TestCase):
    def test_metres_to_degrees_match_reference_lengths(self):
        f = GeoFrame(50.0, 30.0, 90)           # X — схід, Y — північ
        lat, lon = f.to_latlon(0.0, 1000.0)
        self.assertAlmostEqual((lat - 50.0) * DEG_LAT_50, 1000.0, delta=1.0)
        self.assertAlmostEqual(lon, 30.0)
        lat, lon = f.to_latlon(1000.0, 0.0)
        self.assertAlmostEqual((lon - 30.0) * DEG_LON_50, 1000.0, delta=1.0)
        self.assertAlmostEqual(lat, 50.0)

    def test_origin_and_roundtrip(self):
        for bearing in (0, 37.5, 90, 200, 359):
            f = GeoFrame(*KYIV, bearing)
            self.assertEqual(f.to_latlon(0, 0), KYIV)
            for x, y in ((12.3, 45.6), (-30, 7), (0, -50)):
                bx, by = f.to_local(*f.to_latlon(x, y))
                self.assertAlmostEqual(bx, x, places=6)
                self.assertAlmostEqual(by, y, places=6)

    def test_axes_follow_bearing(self):
        north_up = GeoFrame(*KYIV, 90)
        def en(frame, x, y):                  # куди потрапила точка: (схід, північ), м
            return north_up.to_local(*frame.to_latlon(x, y))
        f0 = GeoFrame(*KYIV, 0)               # X — на північ, тоді Y — на захід
        for got, want in ((en(f0, 10, 0), (0, 10)), (en(f0, 0, 10), (-10, 0))):
            self.assertAlmostEqual(got[0], want[0], places=6)
            self.assertAlmostEqual(got[1], want[1], places=6)
        e, n = en(GeoFrame(*KYIV, 45), 10, 0)
        self.assertAlmostEqual(e, 10 / math.sqrt(2), places=6)
        self.assertAlmostEqual(n, 10 / math.sqrt(2), places=6)

    def test_distances_are_preserved(self):
        f = GeoFrame(*KYIV, 123)
        north_up = GeoFrame(*KYIV, 90)
        a, b = north_up.to_local(*f.to_latlon(0, 0)), north_up.to_local(*f.to_latlon(30, 40))
        self.assertAlmostEqual(math.hypot(b[0] - a[0], b[1] - a[1]), 50.0, places=4)


class ConfigTest(unittest.TestCase):
    def test_absent(self):
        self.assertIsNone(frame_from_config(None))
        self.assertIsNone(frame_from_config({}))

    def test_default_bearing_is_east(self):
        self.assertEqual(frame_from_config({"origin": list(KYIV)}).bearing, 90.0)

    def test_errors(self):
        for geo in ({"x_bearing_deg": 90}, {"origin": [50.0]}, {"origin": ["50", "30"]}, {"origin": [95.0, 30.0]},
                    {"origin": list(KYIV), "x_bearing_deg": "east"}, "origin"):
            with self.assertRaises(ValueError, msg=geo):
                frame_from_config(geo)


class FieldFrameTest(unittest.TestCase):
    def test_grid_is_north_up_with_margin_around_beacons(self):
        north_up = GeoFrame(*KYIV, 90)
        pts = [north_up.to_latlon(x, y) for x, y in ((0, 0), (40, 5), (15, 30))]
        geo, width, height = field_frame(pts, margin=10)
        f = frame_from_config(geo)
        self.assertEqual(f.bearing, 90.0)
        xy = [f.to_local(*p) for p in pts]
        for (x, y), (wx, wy) in zip(xy, ((10, 10), (50, 15), (25, 40))):   # зсув на відступ 10 м
            self.assertAlmostEqual(x, wx, places=2)
            self.assertAlmostEqual(y, wy, places=2)
        self.assertEqual((width, height), (60, 50))

    def test_single_beacon_and_zero_margin(self):
        geo, width, height = field_frame([KYIV], margin=0)
        self.assertEqual((width, height), (0, 0))
        self.assertAlmostEqual(geo["origin"][0], KYIV[0], places=7)


class FormatParseTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_latlon(50.4501, 30.5234), "50.450100, 30.523400")

    def test_parse(self):
        self.assertEqual(parse_latlon("50.4501", "30.5234"), (50.4501, 30.5234))
        self.assertEqual(parse_latlon("50.4501,", "30.5234"), (50.4501, 30.5234))   # скопійовано з карти
        self.assertEqual(parse_latlon("50,4501", "30,5234"), (50.4501, 30.5234))    # десяткова кома
        for bad in (("abc", "30"), ("91", "30"), ("50", "181")):
            with self.assertRaises(ValueError):
                parse_latlon(*bad)


if __name__ == "__main__":
    unittest.main()
