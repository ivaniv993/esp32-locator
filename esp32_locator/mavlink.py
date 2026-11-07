"""Передача положення приймача в ArduPilot як GPS: повідомлення MAVLink GPS_INPUT.

На контролері: GPS1_TYPE = 14 (MAVLink; до 4.6 — GPS_TYPE), SERIALn_PROTOCOL = 2 для порту,
до якого підключено Raspberry Pi. Працює лише в бойовому режимі — потрібні широта й довгота.

Що надсилається (5 разів на секунду, на кожен перерахунок положення):
  * фікс 3D, коли чути щонайменше 3 маяки; інакше «немає фіксу» — ArduPilot знає, що GPS є, але не довіряє;
  * висота — стала (mavlink.alt_m): маяки висоти не вимірюють;
  * швидкість не надсилається (прапорці ignore): похідна від положення з вікном 3 с надто шумна й запізніла;
  * точність — не менше mavlink.min_accuracy_m і не менше узгодженості вимірів;
  * кількість супутників — умовна (SYNTHETIC_SATS): її перевіряють передстартові перевірки ArduPilot.
Формування полів — чисті функції (тестуються без pymavlink); pymavlink потрібна лише MavlinkSender.
"""
import os
import threading
import time

GPS_EPOCH_UNIX = 315964800        # 1980-01-06 00:00:00 UTC
LEAP_SECONDS = 18                 # GPS − UTC, незмінне з 2017 року
WEEK_S = 604800

# GPS_INPUT_IGNORE_FLAGS з MAVLink common.xml
IGNORE_VEL_HORIZ = 8
IGNORE_VEL_VERT = 16
IGNORE_SPEED_ACCURACY = 32

FIX_NONE = 1                      # GPS_FIX_TYPE_NO_FIX
FIX_3D = 3                        # GPS_FIX_TYPE_3D_FIX
MIN_BEACONS = 3
SYNTHETIC_SATS = 10

SOURCE_SYSTEM = 1                 # як у контролера за замовчуванням (SYSID_THISMAV)
SOURCE_COMPONENT = 191            # MAV_COMP_ID_ONBOARD_COMPUTER
MAV_TYPE_ONBOARD_CONTROLLER = 18
MAV_AUTOPILOT_INVALID = 8
HEARTBEAT_TIMEOUT_S = 3.0


def gps_time(unix_time):
    """(тиждень GPS, мілісекунди від початку тижня) для часу UNIX."""
    ms_total = int(round((unix_time - GPS_EPOCH_UNIX + LEAP_SECONDS) * 1000))
    return ms_total // (WEEK_S * 1000), ms_total % (WEEK_S * 1000)


def gps_input(latlon, heard, rms, settings, unix_time):
    """Поля GPS_INPUT. latlon — (широта, довгота) або None; heard — скільки маяків чути;
    rms — узгодженість вимірів, м (або None); settings — розділ mavlink з config.json."""
    week, week_ms = gps_time(unix_time)
    fix = latlon is not None and heard >= MIN_BEACONS
    lat, lon = latlon if fix else (0.0, 0.0)
    accuracy = max(float(settings["min_accuracy_m"]), float(rms or 0.0))
    return {
        "time_usec": int(unix_time * 1e6),
        "gps_id": 0,
        "ignore_flags": IGNORE_VEL_HORIZ | IGNORE_VEL_VERT | IGNORE_SPEED_ACCURACY,
        "time_week_ms": week_ms,
        "time_week": week,
        "fix_type": FIX_3D if fix else FIX_NONE,
        "lat": int(round(lat * 1e7)),             # градуси × 10^7
        "lon": int(round(lon * 1e7)),
        "alt": float(settings["alt_m"]),          # м над рівнем моря
        "hdop": 1.0 if fix else 99.0,
        "vdop": 1.0 if fix else 99.0,
        "vn": 0.0, "ve": 0.0, "vd": 0.0,
        "speed_accuracy": 0.0,
        "horiz_accuracy": accuracy if fix else 999.0,
        "vert_accuracy": 10.0 if fix else 999.0,
        "satellites_visible": SYNTHETIC_SATS if fix else 0,
    }


def make_publisher(frame, sender, settings, clock=time.time):
    """Функція для виклику після кожного Locator.update(): перетворює положення на GPS_INPUT і віддає sender."""
    def publish(loc):
        latlon = frame.to_latlon(*loc.pos) if loc.pos else None
        sender.submit(gps_input(latlon, loc.heard, loc.rms, settings, clock()))
    return publish


def pymavlink_available():
    try:
        import pymavlink  # noqa: F401
    except ImportError:
        return False
    return True


def _connect(port, baud):
    os.environ.setdefault("MAVLINK20", "1")       # MAVLink 2 — до імпорту mavutil
    from pymavlink import mavutil
    return mavutil.mavlink_connection(port, baud=baud, source_system=SOURCE_SYSTEM,
                                      source_component=SOURCE_COMPONENT, autoreconnect=False)


class MavlinkSender(threading.Thread):
    """Тримає з’єднання з контролером: надсилає останній GPS_INPUT, свій HEARTBEAT раз на секунду,
    слухає HEARTBEAT контролера (для рядка стану) і перепідключається після помилок.
    З’єднання використовується лише в цьому потоці; submit() можна викликати з будь-якого."""

    def __init__(self, port, baud, connect=_connect, clock=time.monotonic):
        super().__init__(daemon=True)
        self.port, self.baud = port, baud
        self._connect, self._clock = connect, clock
        self._lock = threading.Lock()
        self._pending = None
        self._wake = threading.Event()
        self._halt = threading.Event()
        self.sent = 0
        self.last_fix = None
        self.last_heartbeat = None
        self.error = None
        self.status = f"ArduPilot: підключення до {port}…"

    def submit(self, fields):
        with self._lock:
            self._pending = fields
        self._wake.set()

    def stop(self):
        self._halt.set()
        self._wake.set()

    def run(self):
        while not self._halt.is_set():
            try:
                conn = self._connect(self.port, self.baud)
            except Exception as e:                    # pymavlink кидає різні винятки на різні помилки порту
                self.error = f"помилка порту {self.port}: {e}"
                self._update_status()
                self._halt.wait(2.0)
                continue
            self.error = None
            try:
                self._serve(conn)
            except Exception as e:
                self.error = f"зв’язок обірвався: {e}"
                self._update_status()
                self._halt.wait(1.0)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def _serve(self, conn):
        next_heartbeat = 0.0
        while not self._halt.is_set():
            self._wake.wait(0.1)
            self._wake.clear()
            with self._lock:
                fields, self._pending = self._pending, None
            if fields:
                conn.mav.gps_input_send(**fields)
                self.sent += 1
                self.last_fix = fields["fix_type"] == FIX_3D
            now = self._clock()
            if now >= next_heartbeat:
                conn.mav.heartbeat_send(MAV_TYPE_ONBOARD_CONTROLLER, MAV_AUTOPILOT_INVALID, 0, 0, 0)
                next_heartbeat = now + 1.0
            while True:
                msg = conn.recv_match(type="HEARTBEAT", blocking=False)
                if msg is None:
                    break
                if getattr(msg, "autopilot", MAV_AUTOPILOT_INVALID) != MAV_AUTOPILOT_INVALID:
                    self.last_heartbeat = now             # це контролер, а не інший супутній комп’ютер
            self._update_status()

    def _update_status(self):
        if self.error:
            self.status = f"ArduPilot: {self.error}"
            return
        alive = self.last_heartbeat is not None and self._clock() - self.last_heartbeat < HEARTBEAT_TIMEOUT_S
        link = "зв’язок є" if alive else f"немає відповіді від контролера на {self.port}"
        fix = "" if self.last_fix is None else ("  фікс" if self.last_fix else "  немає фіксу (< 3 маяків)")
        self.status = f"ArduPilot: {link}, надіслано {self.sent}{fix}"
