"""Два режими config.json: тестовий (метри) і бойовий (широта, довгота)."""
import json
import os
import tempfile
import unittest

from esp32_locator.config import field_spread_warning, load_config, save_config
from esp32_locator.geo import GeoFrame, frame_from_config

KYIV = (50.4501, 30.5234)
NORTH_UP = GeoFrame(*KYIV, 90)


def beacon(name, mac, x, y):
    lat, lon = NORTH_UP.to_latlon(x, y)
    return {"name": name, "mac": mac, "lat": lat, "lon": lon}


class ModesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")

    def write(self, **cfg):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def read_raw(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_default_is_local(self):
        cfg = load_config(self.path)
        self.assertEqual(cfg["mode"], "local")
        self.assertIsNone(cfg["geo"])
        self.assertNotIn("geo", self.read_raw())

    def test_local_mode_keeps_metres_and_area(self):
        self.write(area={"width": 7, "height": 5}, transmitters=[{"name": "A", "mac": "aa:aa:aa:aa:aa:aa", "x": "1", "y": 2}])
        cfg = load_config(self.path)
        self.assertEqual(cfg["transmitters"][0], {"name": "A", "mac": "AAAAAAAAAAAA", "x": 1.0, "y": 2.0})
        self.assertEqual(cfg["area"], {"width": 7, "height": 5})

    def test_field_mode_derives_grid_area_and_metres(self):
        self.write(mode="field", field_margin_m=5,
                   transmitters=[beacon("A", "A" * 12, 0, 0), beacon("B", "B" * 12, 30, 0), beacon("C", "C" * 12, 10, 20)])
        cfg = load_config(self.path)
        self.assertEqual(cfg["area"], {"width": 40, "height": 30})
        xy = [(t["x"], t["y"]) for t in cfg["transmitters"]]
        for (x, y), (wx, wy) in zip(xy, ((5, 5), (35, 5), (15, 25))):
            self.assertAlmostEqual(x, wx, delta=0.01)
            self.assertAlmostEqual(y, wy, delta=0.01)
        f = frame_from_config(cfg["geo"])
        lat, lon = f.to_latlon(*xy[1])                     # точка мапи -> назад у широту/довготу маяка B
        self.assertAlmostEqual(lat, cfg["transmitters"][1]["lat"], places=6)
        self.assertAlmostEqual(lon, cfg["transmitters"][1]["lon"], places=6)
        self.assertIsNone(field_spread_warning(cfg))

    def test_field_mode_saves_only_lat_lon(self):
        self.write(mode="field", area={"width": 1, "height": 1}, transmitters=[beacon("A", "A" * 12, 0, 0)])
        cfg = load_config(self.path)
        save_config(self.path, cfg)
        raw = self.read_raw()
        self.assertNotIn("geo", raw)
        self.assertNotIn("area", raw)                      # у бойовому режимі area рахується сама
        self.assertEqual(set(raw["transmitters"][0]), {"name", "mac", "lat", "lon"})

    def test_field_mode_without_beacons(self):
        self.write(mode="field")
        cfg = load_config(self.path)
        self.assertIsNone(cfg["geo"])

    def test_errors_explain_what_to_do(self):
        cases = [
            ({"mode": "combat"}, "mode"),
            ({"mode": "field", "transmitters": [{"name": "A", "mac": "A" * 12, "x": 1, "y": 1}]}, "--add-geo"),
            ({"transmitters": [{"name": "A", "mac": "A" * 12, "lat": 50.0, "lon": 30.0}]}, "field"),
            ({"mode": "field", "transmitters": [{"name": "A", "mac": "A" * 12, "lat": "пн", "lon": 30.0}]}, "числами"),
            ({"mode": "field", "transmitters": [{"name": "A", "mac": "A" * 12, "lat": 95.0, "lon": 30.0}]}, "широта"),
            ({"mode": "field", "field_margin_m": -1}, "field_margin_m"),
        ]
        for cfg, hint in cases:
            self.write(**cfg)
            with self.assertRaises(ValueError, msg=cfg) as ctx:
                load_config(self.path)
            self.assertIn(hint, str(ctx.exception))

    def test_swapped_lat_lon_is_flagged(self):
        a = beacon("A", "A" * 12, 0, 0)
        b = {"name": "B", "mac": "B" * 12, "lat": a["lon"], "lon": a["lat"]}     # широту й довготу переплутано
        self.write(mode="field", transmitters=[a, b])
        self.assertIn("переплутано", field_spread_warning(load_config(self.path)))


if __name__ == "__main__":
    unittest.main()
