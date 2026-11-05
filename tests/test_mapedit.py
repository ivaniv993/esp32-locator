"""Редагування маяків на мапі: координати екрана, пошук маяка під курсором, видалення."""
import unittest

from esp32_locator.mapedit import beacon_at, remove_beacon, screen_to_world, world_to_screen

# Зона 10 × 8 м, 50 пікселів на метр, точка (0; 0) на екрані в (30, 430).
GEO = {"ox": 30.0, "oy": 430.0, "scale": 50.0, "aw": 10.0, "ah": 8.0}


def beacons():
    return [{"name": "A", "mac": "A1", "x": 0.0, "y": 0.0},
            {"name": "B", "mac": "B2", "x": 10.0, "y": 0.0},
            {"name": "C", "mac": "C3", "x": 5.0, "y": 8.0}]


class ScreenToWorldTest(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(screen_to_world(world_to_screen(3.4, 6.2, GEO), GEO), (3.4, 6.2))

    def test_rounds_to_step(self):
        self.assertEqual(screen_to_world((30 + 3.44 * 50, 430 - 1.06 * 50), GEO), (3.4, 1.1))

    def test_clamped_to_area(self):
        self.assertEqual(screen_to_world((-500, 900), GEO), (0.0, 0.0))
        self.assertEqual(screen_to_world((5000, -900), GEO), (10.0, 8.0))


class BeaconAtTest(unittest.TestCase):
    def test_hit_within_radius(self):
        x, y = world_to_screen(5.0, 8.0, GEO)
        self.assertEqual(beacon_at((x + 10, y - 10), beacons(), GEO)["name"], "C")

    def test_miss_outside_radius(self):
        x, y = world_to_screen(5.0, 8.0, GEO)
        self.assertIsNone(beacon_at((x + 40, y), beacons(), GEO))

    def test_nearest_wins_when_two_are_close(self):
        tx = beacons() + [{"name": "D", "mac": "D4", "x": 5.3, "y": 8.0}]
        x, y = world_to_screen(5.25, 8.0, GEO)
        self.assertEqual(beacon_at((x, y), tx, GEO)["name"], "D")


class RemoveBeaconTest(unittest.TestCase):
    def test_remove(self):
        cfg = {"transmitters": beacons()}
        self.assertEqual(remove_beacon(cfg, "B2")["name"], "B")
        self.assertEqual([t["name"] for t in cfg["transmitters"]], ["A", "C"])
        self.assertIsNone(remove_beacon(cfg, "B2"))


if __name__ == "__main__":
    unittest.main()
