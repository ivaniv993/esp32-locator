"""Журнал пакетів для екрана."""
import re
import time
import unittest
from unittest import mock

from esp32_locator.packetlog import PacketFeed, Row, format_row
from esp32_locator.readings import Readings

A, B, C = "A" * 12, "B" * 12, "C" * 12
TX = [{"name": "Кухня", "mac": A, "x": 0, "y": 0}, {"name": "", "mac": B, "x": 5, "y": 0}]


class RecentTest(unittest.TestCase):
    def test_disabled_by_default(self):
        r = Readings(3.0)
        r.add(A, -70.0, 5.0, 14.0)
        self.assertEqual(r.packets_after(0), [])
        self.assertIsNone(r.recent)

    def test_packets_come_in_arrival_order_with_sequence_numbers(self):
        r = Readings(3.0, keep_recent=100)
        for mac in (A, B, A):
            r.add(mac, -70.0, 5.0, 14.0)
        got = r.packets_after(0)
        self.assertEqual([p[2] for p in got], [A, B, A])
        self.assertEqual([p[0] for p in got], [1, 2, 3])
        self.assertEqual([p[0] for p in r.packets_after(2)], [3])

    def test_only_last_n_are_kept(self):
        r = Readings(3.0, keep_recent=3)
        for _ in range(10):
            r.add(A, -70.0, 5.0, 14.0)
        self.assertEqual([p[0] for p in r.packets_after(0)], [8, 9, 10])


class FeedTest(unittest.TestCase):
    def feed_with(self, packets):
        r = Readings(3.0, keep_recent=100)
        feed = PacketFeed(r)
        for wall, mac, rssi in packets:
            with mock.patch("esp32_locator.readings.time.time", return_value=wall):
                r.add(mac, rssi, 5.0, 14.0)
        feed.update(TX)
        return feed

    def test_rows_gaps_and_names(self):
        feed = self.feed_with([(100.0, A, -60.0), (100.5, B, -70.0), (101.5, A, -61.0), (103.0, C, -90.0)])
        rows = list(feed.rows)
        self.assertEqual([r.mac for r in rows], [A, B, A, C])
        self.assertEqual([r.name for r in rows], ["Кухня", B[-5:], "Кухня", None])   # без імені — кінець ID; невідомий — None
        self.assertEqual([r.gap for r in rows], [None, None, 1.5, None])

    def test_update_only_adds_new_packets(self):
        r = Readings(3.0, keep_recent=100)
        feed = PacketFeed(r)
        r.add(A, -60.0, 5.0, 14.0)
        feed.update(TX)
        feed.update(TX)
        r.add(A, -61.0, 5.0, 14.0)
        feed.update(TX)
        self.assertEqual(len(feed.rows), 2)

    def test_ignores_packets_received_before_the_feed_was_created(self):
        r = Readings(3.0, keep_recent=100)
        r.add(A, -60.0, 5.0, 14.0)
        feed = PacketFeed(r)
        feed.update(TX)
        self.assertEqual(len(feed.rows), 0)

    def test_maxlen(self):
        r = Readings(3.0, keep_recent=100)
        feed = PacketFeed(r, maxlen=5)
        for _ in range(20):
            r.add(A, -60.0, 5.0, 14.0)
        feed.update(TX)
        self.assertEqual(len(feed.rows), 5)

    def test_works_without_keep_recent(self):
        r = Readings(3.0)
        feed = PacketFeed(r)
        r.add(A, -60.0, 5.0, 14.0)
        feed.update(TX)
        self.assertEqual(len(feed.rows), 0)


class FormatTest(unittest.TestCase):
    def test_format_row(self):
        wall = time.mktime((2026, 11, 2, 14, 30, 5, 0, 0, -1)) + 0.123
        line = format_row(Row(wall, "A1B2C3D4E5F6", "A", -67.4, 9.2, 0.52))
        self.assertRegex(line, r"^14:30:05\.12\d  {3}A  …E5F6   -67 дБм  SNR   9  \+0,5 с$")

    def test_unknown_beacon_first_packet_and_missing_snr(self):
        line = format_row(Row(0.0, "A1B2C3D4E5F6", None, -100.0, None, None))
        self.assertIn("  ?  ", line)
        self.assertTrue(line.endswith("SNR   —  перший"))
        self.assertTrue(re.match(r"^\d\d:\d\d:\d\d\.\d{3}", line))


if __name__ == "__main__":
    unittest.main()
