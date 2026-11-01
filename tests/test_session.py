"""Формат файлу сесії: запис, читання, пошкодження."""
import json
import os
import tempfile
import unittest

from esp32_locator.session import (FORMAT_VERSION, SessionWriter, default_recordings_dir, load_session,
                                   make_header, new_session_path, session_config)

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


if __name__ == "__main__":
    unittest.main()
