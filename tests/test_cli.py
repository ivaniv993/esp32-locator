"""Командний рядок: --info і перевірка аргументів --record."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from esp32_locator import cli
from esp32_locator.geo import GeoFrame

from .synthetic import make_session


def run(*argv):
    out = io.StringIO()
    with mock.patch("sys.argv", ["locator.py", *argv]), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(io.StringIO()):
        try:
            cli.main()
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if isinstance(e.code, str):
                out.write(e.code)
    return code, out.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = os.path.join(self.tmp.name, "config.json")

    def save(self, session, name):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(session.header, ensure_ascii=False) + "\n")
            for t, line in session.rx:
                f.write(json.dumps({"type": "rx", "t": t, "line": line}) + "\n")
        return path

    def test_info_describes_each_file(self):
        self.save(make_session(radio={"sf": 7, "bw_khz": 125}, duration=10.0), "a.jsonl")
        self.save(make_session(duration=10.0), "b.jsonl")
        with open(os.path.join(self.tmp.name, "bad.jsonl"), "w", encoding="utf-8") as f:
            f.write("not json\n")
        code, out = run("--config", self.cfg, "--info", self.tmp.name)
        self.assertIn("Радіо: SF7, 125 кГц", out)
        self.assertIn("не вказано (старий запис)", out)
        self.assertIn("Помилка:", out)                    # зіпсований файл не зупиняє решту…
        self.assertEqual(out.count("Записано:"), 2)
        self.assertEqual(code, 1)                         # …але про нього каже код виходу

    def test_info_ok_and_missing_file(self):
        good = self.save(make_session(duration=5.0), "a.jsonl")
        self.assertEqual(run("--config", self.cfg, "--info", good)[0], 0)
        code, out = run("--config", self.cfg, "--info", os.path.join(self.tmp.name, "no-such.jsonl"))
        self.assertEqual(code, 1)
        self.assertIn("Помилка:", out)
        empty = os.path.join(self.tmp.name, "empty-dir")
        os.mkdir(empty)
        self.assertEqual(run("--config", self.cfg, "--info", empty), (1, "Записів не знайдено."))

    def write_cfg(self, **kw):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(kw, f)

    def raw_cfg(self):
        with open(self.cfg, encoding="utf-8") as f:
            return json.load(f)

    def test_field_mode_add_geo_and_list(self):
        self.write_cfg(mode="field")
        lat, lon = GeoFrame(50.4501, 30.5234, 90).to_latlon(12.0, 34.0)
        code, out = run("--config", self.cfg, "--add-geo", "a1:b2:c3:d4:e5:f6", f"{lat:.7f},", f"{lon:.7f}", "--name", "Пн")
        self.assertEqual(code, 0, out)
        run("--config", self.cfg, "--add-geo", "B2B2B2B2B2B2", "50.4501", "30.5234")
        tx = self.raw_cfg()["transmitters"]
        self.assertEqual([set(t) for t in tx], [{"name", "mac", "lat", "lon"}] * 2)   # без x, y у файлі
        self.assertEqual((tx[0]["name"], tx[0]["mac"], tx[0]["lat"]), ("Пн", "A1B2C3D4E5F6", round(lat, 7)))
        code, out = run("--config", self.cfg, "--list")
        self.assertIn("Режим: бойовий", out)
        self.assertIn(f"{lat:.6f}, {lon:.6f}", out)
        self.assertNotIn("переплутано", out)

    def test_field_mode_warns_about_far_beacon(self):
        self.write_cfg(mode="field")
        run("--config", self.cfg, "--add-geo", "A1B2C3D4E5F6", "50.4501", "30.5234")
        code, out = run("--config", self.cfg, "--add-geo", "B2B2B2B2B2B2", "30.5234", "50.4501")
        self.assertEqual(code, 0)
        self.assertIn("переплутано", out)

    def test_wrong_command_for_mode(self):
        self.write_cfg()
        code, out = run("--config", self.cfg, "--add-geo", "A1B2C3D4E5F6", "50.4501", "30.5234")
        self.assertEqual(code, 1)
        self.assertIn('"mode": "field"', out)
        self.write_cfg(mode="field")
        code, out = run("--config", self.cfg, "--add", "A1B2C3D4E5F6", "1", "2")
        self.assertEqual(code, 1)
        self.assertIn("--add-geo", out)

    def test_record_truth_is_lat_lon_in_field_mode(self):
        self.write_cfg(mode="field", field_margin_m=10)
        run("--config", self.cfg, "--add-geo", "A1B2C3D4E5F6", "50.4501", "30.5234")
        code, out = run("--config", self.cfg, "--record", "--truth", "3.5", "2.0")      # метри замість широти
        self.assertEqual(code, 1)
        self.assertIn("field_margin_m", out)
        code, out = run("--config", self.cfg, "--record", "--truth", "abc", "2.0")
        self.assertEqual(code, 1)
        self.assertIn("широту й довготу", out)

    def test_mavlink_needs_field_mode(self):
        self.write_cfg()
        with mock.patch("esp32_locator.cli.pymavlink_available", return_value=True):
            code, out = run("--config", self.cfg, "--mavlink", "/dev/ttyUSB0", "--headless")
        self.assertEqual(code, 1)
        self.assertIn("бойовому режимі", out)

    def test_mavlink_without_pymavlink(self):
        self.write_cfg(mode="field")
        run("--config", self.cfg, "--add-geo", "A1B2C3D4E5F6", "50.4501", "30.5234")
        with mock.patch("esp32_locator.cli.pymavlink_available", return_value=False), \
                mock.patch("esp32_locator.cli.SerialReader.start"):
            code, out = run("--config", self.cfg, "--mavlink", "/dev/ttyUSB0", "--headless")
        self.assertEqual(code, 1)
        self.assertIn("pip3 install", out)

    def test_mavlink_rejects_same_port_and_other_commands(self):
        self.write_cfg(mode="field", serial_port="/dev/ttyUSB0")
        code, out = run("--config", self.cfg, "--mavlink", "/dev/ttyUSB0")
        self.assertEqual(code, 1)
        self.assertIn("одному порту", out)
        code, out = run("--config", self.cfg, "--mavlink", "/dev/ttyUSB1", "--list")
        self.assertEqual(code, 1)
        self.assertIn("--record або --replay", out)

    def test_record_with_mavlink_error_leaves_no_file(self):
        self.write_cfg()
        run("--config", self.cfg, "--add", "A1B2C3D4E5F6", "1", "1")
        out_dir = os.path.join(self.tmp.name, "rec")
        code, out = run("--config", self.cfg, "--record", "--truth", "1", "1", "--mavlink", "/dev/ttyUSB0",
                        "--out", out_dir)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(out_dir) and os.listdir(out_dir))

    def test_broken_config_is_reported(self):
        self.write_cfg(mode="combat")
        code, out = run("--config", self.cfg, "--list")
        self.assertEqual(code, 1)
        self.assertIn("mode", out)

    def test_record_rejects_impossible_sf(self):
        code, _ = run("--config", self.cfg, "--record", "--truth", "1", "1", "--sf", "13")
        self.assertEqual(code, 2)                         # помилка argparse — ще до відкриття порту


if __name__ == "__main__":
    unittest.main()
