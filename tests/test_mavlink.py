"""Передача положення в ArduPilot (GPS_INPUT). Без pymavlink: з’єднання підміняється."""
import calendar
import threading
import time
import types
import unittest
from unittest import mock

from esp32_locator import mavlink
from esp32_locator.geo import GeoFrame
from esp32_locator.mavlink import (FIX_3D, FIX_NONE, IGNORE_SPEED_ACCURACY, IGNORE_VEL_HORIZ, IGNORE_VEL_VERT,
                                   MavlinkSender, gps_input, gps_time, make_publisher)
from esp32_locator.sources import SerialReader

SETTINGS = {"baud": 115200, "alt_m": 152.5, "min_accuracy_m": 2.0}


class GpsTimeTest(unittest.TestCase):
    def test_reference_dates(self):
        # тиждень 2295 почався 31.12.2023 (неділя); UTC відстає від GPS на 18 с
        t = calendar.timegm((2024, 1, 1, 0, 0, 0))
        self.assertEqual(gps_time(t), (2295, (86400 + 18) * 1000))
        self.assertEqual(gps_time(mavlink.GPS_EPOCH_UNIX - 18), (0, 0))

    def test_week_rollover(self):
        start = mavlink.GPS_EPOCH_UNIX - 18 + 2295 * mavlink.WEEK_S
        self.assertEqual(gps_time(start - 0.001), (2294, mavlink.WEEK_S * 1000 - 1))
        self.assertEqual(gps_time(start), (2295, 0))


class GpsInputTest(unittest.TestCase):
    def test_fix(self):
        f = gps_input((50.4501812, 30.5235691), heard=3, rms=0.4, settings=SETTINGS, unix_time=1_800_000_000.25)
        self.assertEqual(f["fix_type"], FIX_3D)
        self.assertEqual((f["lat"], f["lon"]), (504501812, 305235691))     # градуси × 10^7
        self.assertEqual(f["alt"], 152.5)
        self.assertEqual(f["horiz_accuracy"], 2.0)                         # не менше min_accuracy_m
        self.assertEqual(f["satellites_visible"], mavlink.SYNTHETIC_SATS)
        self.assertEqual(f["ignore_flags"], IGNORE_VEL_HORIZ | IGNORE_VEL_VERT | IGNORE_SPEED_ACCURACY)
        self.assertEqual(f["time_usec"], 1_800_000_000_250_000)
        self.assertEqual((f["time_week"], f["time_week_ms"]), gps_time(1_800_000_000.25))
        self.assertLessEqual(f["hdop"], 1.4)                               # проходить GPS_HDOP_GOOD за замовчуванням

    def test_inconsistent_measurements_raise_reported_error(self):
        f = gps_input((50.45, 30.52), heard=4, rms=5.5, settings=SETTINGS, unix_time=0)
        self.assertEqual(f["horiz_accuracy"], 5.5)

    def test_no_fix_when_fewer_than_three_beacons_or_no_position(self):
        for latlon, heard in (((50.45, 30.52), 2), (None, 5), (None, 0)):
            f = gps_input(latlon, heard, rms=None, settings=SETTINGS, unix_time=0)
            self.assertEqual(f["fix_type"], FIX_NONE)
            self.assertEqual((f["lat"], f["lon"], f["satellites_visible"]), (0, 0, 0))
            self.assertGreater(f["horiz_accuracy"], 100)

    def test_fields_match_gps_input_message(self):
        # усі поля MAVLink GPS_INPUT, крім необов’язкового розширення yaw
        expected = {"time_usec", "gps_id", "ignore_flags", "time_week_ms", "time_week", "fix_type", "lat", "lon",
                    "alt", "hdop", "vdop", "vn", "ve", "vd", "speed_accuracy", "horiz_accuracy", "vert_accuracy",
                    "satellites_visible"}
        self.assertEqual(set(gps_input(None, 0, None, SETTINGS, 0)), expected)


class PublisherTest(unittest.TestCase):
    def test_converts_map_position_to_lat_lon(self):
        frame = GeoFrame(50.4501, 30.5234, 90)
        sent = []
        sender = types.SimpleNamespace(submit=sent.append)
        publish = make_publisher(frame, sender, SETTINGS, clock=lambda: 0.0)
        publish(types.SimpleNamespace(pos=(12.0, 34.0), heard=3, rms=0.1))
        publish(types.SimpleNamespace(pos=None, heard=0, rms=None))
        lat, lon = frame.to_latlon(12.0, 34.0)
        self.assertEqual((sent[0]["lat"], sent[0]["lon"]), (round(lat * 1e7), round(lon * 1e7)))
        self.assertEqual([s["fix_type"] for s in sent], [FIX_3D, FIX_NONE])


class FakeConn:
    """Імітація з’єднання pymavlink: записує надіслане, віддає заплановані HEARTBEAT."""

    def __init__(self, heartbeats=()):
        self.sent, self.heartbeats_sent, self.closed = [], [], False
        self.incoming = list(heartbeats)
        self.mav = types.SimpleNamespace(gps_input_send=lambda **f: self.sent.append(f),
                                         heartbeat_send=lambda *a: self.heartbeats_sent.append(a))

    def recv_match(self, type=None, blocking=False):
        return self.incoming.pop(0) if self.incoming else None

    def close(self):
        self.closed = True


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class SenderTest(unittest.TestCase):
    def test_sends_latest_fields_and_reports_link(self):
        conn = FakeConn(heartbeats=[types.SimpleNamespace(autopilot=3)])            # MAV_AUTOPILOT_ARDUPILOTMEGA
        s = MavlinkSender("/dev/ttyUSB0", 115200, connect=lambda p, b: conn)
        s.start()
        self.addCleanup(s.stop)
        s.submit(gps_input((50.45, 30.52), 3, 0.1, SETTINGS, 0))
        self.assertTrue(wait_for(lambda: conn.sent and "зв’язок є" in s.status), s.status)
        self.assertEqual(conn.sent[0]["fix_type"], FIX_3D)
        self.assertIn("фікс", s.status)
        self.assertEqual(conn.heartbeats_sent[0][:2], (mavlink.MAV_TYPE_ONBOARD_CONTROLLER, mavlink.MAV_AUTOPILOT_INVALID))

    def test_only_newest_position_is_sent_when_several_queued(self):
        conn = FakeConn()
        gate = threading.Event()

        def connect(port, baud):
            gate.wait(2)
            return conn
        s = MavlinkSender("p", 1, connect=connect)
        s.start()
        self.addCleanup(s.stop)
        for heard in (0, 1, 3):
            s.submit(gps_input((50.45, 30.52), heard, 0.1, SETTINGS, 0))
        gate.set()
        self.assertTrue(wait_for(lambda: conn.sent))
        time.sleep(0.2)
        self.assertEqual([f["fix_type"] for f in conn.sent], [FIX_3D])          # старі положення не надсилаються

    def test_no_heartbeat_from_autopilot(self):
        conn = FakeConn(heartbeats=[types.SimpleNamespace(autopilot=mavlink.MAV_AUTOPILOT_INVALID)])  # інший комп’ютер
        s = MavlinkSender("/dev/ttyUSB0", 115200, connect=lambda p, b: conn)
        s.start()
        self.addCleanup(s.stop)
        s.submit(gps_input(None, 0, None, SETTINGS, 0))
        self.assertTrue(wait_for(lambda: conn.sent))
        self.assertTrue(wait_for(lambda: "немає відповіді від контролера на /dev/ttyUSB0" in s.status), s.status)
        self.assertIn("немає фіксу", s.status)

    def test_port_error_is_reported_and_retried(self):
        attempts = []

        def connect(port, baud):
            attempts.append(port)
            if len(attempts) == 1:
                raise OSError("No such file or directory")
            return FakeConn()
        s = MavlinkSender("/dev/ttyUSB9", 115200, connect=connect)
        with mock.patch.object(s._halt, "wait", lambda t=None: s._halt.is_set()):   # без 2 с очікування
            s.start()
            self.addCleanup(s.stop)
            self.assertTrue(wait_for(lambda: len(attempts) >= 2))
        self.assertTrue(wait_for(lambda: "помилка" not in s.status), s.status)

    def test_broken_link_reconnects(self):
        conns = []

        def connect(port, baud):
            c = FakeConn()
            if not conns:
                def boom(**f):
                    raise OSError("device disconnected")
                c.mav.gps_input_send = boom
            conns.append(c)
            return c
        s = MavlinkSender("p", 1, connect=connect)
        with mock.patch.object(s._halt, "wait", lambda t=None: s._halt.is_set()):
            s.start()
            self.addCleanup(s.stop)
            s.submit(gps_input(None, 0, None, SETTINGS, 0))
            self.assertTrue(wait_for(lambda: len(conns) >= 2))
        self.assertTrue(conns[0].closed)


class ExcludeTest(unittest.TestCase):
    def test_autodetect_skips_flight_controller_port(self):
        ports = {"/dev/ttyUSB*": ["/dev/ttyUSB0"], "/dev/ttyACM*": []}
        links = {"/dev/serial/by-id/usb-FTDI-if00": "/dev/ttyUSB0", "/dev/serial0": "/dev/ttyAMA0"}
        with mock.patch("esp32_locator.sources.glob.glob", lambda p: ports[p]), \
                mock.patch("esp32_locator.sources.os.path.exists", lambda p: p == "/dev/serial0"), \
                mock.patch("esp32_locator.sources.os.path.realpath", lambda p: links.get(p, p)):
            r = SerialReader("auto", 115200, None, exclude=["/dev/serial/by-id/usb-FTDI-if00"])
            self.assertEqual(r.candidates(), ["/dev/serial0"])
            self.assertEqual(SerialReader("auto", 115200, None).candidates(), ["/dev/ttyUSB0", "/dev/serial0"])
            self.assertEqual(SerialReader("/dev/serial0", 1, None, exclude=["/dev/serial0"]).candidates(),
                             ["/dev/serial0"])        # явно вказаний порт не відкидається (CLI перевіряє окремо)


if __name__ == "__main__":
    unittest.main()
