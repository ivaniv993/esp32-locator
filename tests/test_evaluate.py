"""Оцінка алгоритму на записах і порівняння з базовою лінією."""
import os
import tempfile
import unittest

from esp32_locator.evaluate import (collect_paths, compare, evaluate_session, format_report, load_baseline,
                                    save_baseline, to_baseline)

from .synthetic import BEACONS, default_cfg, make_session


class EvaluateTest(unittest.TestCase):
    def test_noiseless_recording_is_located_precisely(self):
        r = evaluate_session(make_session(), default_cfg())
        m = r.metrics()
        self.assertEqual(m["coverage"], 1.0)
        self.assertLess(m["max"], 0.15)           # лишається лише округлення RSSI до 0,1 дБ
        self.assertLess(m["spread"], 0.05)

    def test_noisy_recording_has_bounded_error(self):
        m = evaluate_session(make_session(noise_db=2.0, duration=60.0), default_cfg()).metrics()
        self.assertEqual(m["coverage"], 1.0)
        self.assertLess(m["mean"], 1.5)
        self.assertLess(m["p95"], 3.0)

    def test_warmup_is_skipped(self):
        s = make_session(duration=10.0)
        r = evaluate_session(s, default_cfg())
        # останній пакет на t = 9,52 с; перші 3 с (вікно) не рахуються: кроки 3,0 … 9,4 з кроком 0,2
        self.assertAlmostEqual(s.duration, 9.52)
        self.assertEqual(r.ticks, 33)

    def test_fewer_than_three_beacons_gives_no_fix(self):
        m = evaluate_session(make_session(beacons=BEACONS[:2]), default_cfg()).metrics()
        self.assertEqual((m["fixes"], m["coverage"], m["mean"]), (0, 0.0, None))
        self.assertIn("—", format_report([evaluate_session(make_session(beacons=BEACONS[:2]), default_cfg())], default_cfg()))

    def test_deterministic(self):
        a = evaluate_session(make_session(noise_db=2.0), default_cfg()).fixes
        b = evaluate_session(make_session(noise_db=2.0), default_cfg()).fixes
        self.assertEqual(a, b)

    def test_wrong_calibration_is_visible_in_metrics(self):
        good, bad = default_cfg(), default_cfg()
        bad["signal"]["pl_10m"] = 100.0
        s = make_session(noise_db=1.0)
        self.assertGreater(evaluate_session(s, bad).metrics()["mean"], 3 * evaluate_session(s, good).metrics()["mean"])


class BaselineTest(unittest.TestCase):
    def setUp(self):
        self.sessions = [make_session(noise_db=1.5, seed=1, path="one.jsonl"),
                         make_session(truth=(7.0, 5.0), noise_db=1.5, seed=2, path="two.jsonl")]
        self.good, self.bad = default_cfg(), default_cfg()
        self.bad["signal"]["pl_10m"] = 80.0

    def results(self, cfg):
        return [evaluate_session(s, cfg) for s in self.sessions]

    def verdict(self, cfg, baseline_cfg):
        base = to_baseline(self.results(baseline_cfg), baseline_cfg)
        return compare(self.results(cfg), cfg, base)

    def test_same_algorithm_is_unchanged(self):
        text, verdict = self.verdict(self.good, self.good)
        self.assertEqual(verdict, "без змін")
        self.assertNotIn("Увага", text)

    def test_better_and_worse(self):
        self.assertEqual(self.verdict(self.good, self.bad)[1], "краще")
        text, verdict = self.verdict(self.bad, self.good)
        self.assertEqual(verdict, "гірше")
        self.assertIn("pl_10m: було 67.0, зараз 80.0", text)      # зміну параметрів видно

    def test_lost_coverage_is_worse_even_if_mean_error_is_lower(self):
        full = self.results(self.good)
        base = to_baseline(full, self.good)
        for f in base["files"].values():           # у базовій лінії покриття було повне, а похибка вища
            f["mean"] += 1.0
        broken = [evaluate_session(make_session(beacons=BEACONS[:2], path=s.path, truth=s.truth), self.good)
                  for s in self.sessions]
        self.assertEqual(compare(broken, self.good, base)[1], "гірше")

    def test_only_common_files_are_compared(self):
        base = to_baseline(self.results(self.good)[:1], self.good)
        text, verdict = compare(self.results(self.good), self.good, base)
        self.assertIn("немає в базовій лінії", text)
        self.assertEqual(verdict, "без змін")

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "base.json")
            save_baseline(path, self.results(self.good), self.good)
            base = load_baseline(path)
        self.assertEqual(set(base["files"]), {"one.jsonl", "two.jsonl"})
        self.assertEqual(compare(self.results(self.good), self.good, base)[1], "без змін")

    def test_collect_paths_expands_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            for n in ("b.jsonl", "a.jsonl", "notes.txt"):
                open(os.path.join(tmp, n), "w").close()
            self.assertEqual([os.path.basename(p) for p in collect_paths([tmp])], ["a.jsonl", "b.jsonl"])
            self.assertEqual(collect_paths(["x.jsonl"]), ["x.jsonl"])


if __name__ == "__main__":
    unittest.main()
