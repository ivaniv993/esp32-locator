"""Формат файлу сесії: запис, читання, пошкодження."""
import json
import os
import tempfile
import unittest

from esp32_locator.config import load_config
from esp32_locator.session import (FORMAT_VERSION, SessionWriter, default_recordings_dir, describe,
                                   load_session, make_header, new_session_path, radio_label, session_config)

from .synthetic import default_cfg

TX = [{"name": "A", "mac": "AAAAAAAAAAAA", "x": 0, "y": 0}]


def cfg_with_tx():
    cfg = default_cfg()
    cfg["transmitters"] = [dict(t) for t in TX]
    return cfg


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "s.jsonl")

    def put(self, content):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(content)

    def write(self, *lines):
        w = SessionWriter(self.path, make_header(cfg_with_tx(), (3.5, 2.0), "нотатка"))
        for line in lines:
            w.write_rx(line)
        w.close()
        return w

    def test_roundtrip(self):
        w = self.write("AAAAAAAAAAAA,-67.5,9.2,14\n", "AAAAAAAAAAAA,-68.0,9.0,14")
        s = load_session(self.path)
        self.assertEqual(w.count, 2)
        self.assertEqual([l for _, l in s.rx], ["AAAAAAAAAAAA,-67.5,9.2,14", "AAAAAAAAAAAA,-68.0,9.0,14"])
        self.assertEqual(s.truth, (3.5, 2.0))
        self.assertEqual(s.header["note"], "нотатка")
        self.assertEqual(s.header["transmitters"][0]["mac"], "AAAAAAAAAAAA")
        self.assertLessEqual(s.rx[0][0], s.rx[1][0])

    def test_truncated_last_line_is_ignored(self):
        self.write("AAAAAAAAAAAA,-67.5,9.2,14")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"type":"rx","t":9.9,"li')
        self.assertEqual(len(load_session(self.path).rx), 1)

    def test_broken_line_in_the_middle_is_an_error(self):
        self.write("AAAAAAAAAAAA,-67.5,9.2,14", "AAAAAAAAAAAA,-68.0,9.0,14")
        with open(self.path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        lines[1] = lines[1][:10]
        self.put("\n".join(lines) + "\n")
        with self.assertRaises(ValueError):
            load_session(self.path)

    def test_header_problems(self):
        for content in ("", '{"type":"rx","t":0,"line":"x"}\n',
                        json.dumps({"type": "header", "version": FORMAT_VERSION + 1, "area": {}, "transmitters": [],
                                    "receiver_truth": {"x": 0, "y": 0}}) + "\n",
                        json.dumps({"type": "header", "version": 1}) + "\n"):
            self.put(content)
            with self.assertRaises(ValueError, msg=content):
                load_session(self.path)

    def test_unknown_record_types_are_skipped(self):
        self.write("AAAAAAAAAAAA,-67.5,9.2,14")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"type":"future","t":1}\n')
        self.assertEqual(len(load_session(self.path).rx), 1)

    def test_session_config_takes_area_and_beacons_from_recording(self):
        self.write("AAAAAAAAAAAA,-67.5,9.2,14")
        s = load_session(self.path)
        cfg = default_cfg()
        cfg["area"] = {"width": 99, "height": 99}
        cfg["smoothing"] = 0.9
        out = session_config(cfg, s)
        self.assertEqual(out["area"], {"width": 10, "height": 8})
        self.assertEqual([t["mac"] for t in out["transmitters"]], ["AAAAAAAAAAAA"])
        self.assertEqual(out["smoothing"], 0.9)            # параметри алгоритму — з поточного конфігу
        self.assertEqual(cfg["area"], {"width": 99, "height": 99})   # оригінал не змінено

    def test_new_session_path_does_not_overwrite(self):
        first = new_session_path(self.tmp.name)
        open(first, "w").close()
        self.assertNotEqual(first, new_session_path(self.tmp.name))

    def test_recordings_dir_from_config(self):
        self.assertEqual(default_recordings_dir({"recordings_dir": "/x/y"}), "/x/y")


class RadioAndDistancesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "s.jsonl")

    def record(self, truth, lines, radio=None):
        cfg = cfg_with_tx()
        cfg["transmitters"].append({"name": "B", "mac": "BBBBBBBBBBBB", "x": 10, "y": 0})
        if radio:
            cfg["radio"] = radio
        w = SessionWriter(self.path, make_header(cfg, truth))
        for line in lines:
            w.write_rx(line)
        w.close()
        return load_session(self.path)

    def test_header_stores_radio_and_distances(self):
        s = self.record((3.0, 4.0), [], radio={"sf": 7, "bw_khz": 125})
        self.assertEqual(s.radio, {"sf": 7, "bw_khz": 125})
        self.assertEqual(s.sf, 7)
        self.assertEqual([t["distance"] for t in s.header["transmitters"]], [5.0, 8.1])
        self.assertEqual(s.distances(), {"AAAAAAAAAAAA": 5.0, "BBBBBBBBBBBB": 8.1})

    def test_default_radio_comes_from_config(self):
        self.assertEqual(self.record((1.0, 1.0), []).radio, {"sf": 12, "bw_khz": 125})

    def test_old_recording_without_radio_and_distances(self):
        header = {"type": "header", "version": 1, "area": {"width": 10, "height": 8},
                  "transmitters": [{"name": "A", "mac": "AAAAAAAAAAAA", "x": 0, "y": 0}],
                  "receiver_truth": {"x": 3, "y": 4}}
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
        s = load_session(self.path)
        self.assertIsNone(s.radio)
        self.assertIsNone(s.sf)
        self.assertEqual(s.distances(), {"AAAAAAAAAAAA": 5.0})
        self.assertIn("не вказано (старий запис)", describe(s))

    def test_radio_label(self):
        self.assertEqual(radio_label({"sf": 7, "bw_khz": 125}), "SF7, 125 кГц")
        self.assertEqual(radio_label({"sf": 9, "bw_khz": 62.5}), "SF9, 62.5 кГц")
        self.assertEqual(radio_label({"sf": 7}), "SF7")
        self.assertEqual(radio_label(None), "—")

    def test_describe_counts_packets_per_beacon(self):
        a, c = "AAAAAAAAAAAA,-60.0,8.0,14", "CCCCCCCCCCCC,-80.0,2.0,14"
        s = self.record((3.0, 4.0), [a, a, "AAAAAAAAAAAA,-64.0,8.0,14", c], radio={"sf": 7, "bw_khz": 125})
        text = describe(s)
        self.assertIn("Радіо: SF7, 125 кГц", text)
        lines = {l.split()[0] + " " + l.split()[1]: l for l in text.splitlines() if "…" in l}
        self.assertRegex(lines["A …AAAAAA"], r"5\.0 м\s+3\s+[\d.]+\s+-60 дБм$")
        self.assertRegex(lines["B …BBBBBB"], r"8\.1 м\s+0\s+0\.0\s+—$")             # маяк є, пакетів немає
        self.assertTrue(lines["? …CCCCCC"].endswith("(не зареєстровано)"))


class ConfigRadioTest(unittest.TestCase):
    def test_partial_and_missing_radio_get_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"radio": {"sf": 7}}, f)
            self.assertEqual(load_config(path)["radio"], {"sf": 7, "bw_khz": 125})
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"area": {"width": 5, "height": 5}}, f)       # старий config.json без radio
            self.assertEqual(load_config(path)["radio"], {"sf": 12, "bw_khz": 125})


if __name__ == "__main__":
    unittest.main()
