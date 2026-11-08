"""Веб-сторінка на Raspberry Pi: мапа з маяками й приймачем у браузері телефона, налаштування маяків.

Сервер — стандартна бібліотека Python (ThreadingHTTPServer), без залежностей. Сторінка сама малює мапу
й раз на пів секунди забирає стан (GET /api/state). Положення не рахується тут: WebState отримує
кожен перерахунок через on_update(loc) — той самий, що й екран та передача в ArduPilot.

Зміни маяків (POST /api/beacons, /api/beacons/delete) — лише у звичайному режимі (не запис і не
відтворення) і, якщо в config.json задано web.pin, лише з цим PIN. Тестовий режим — метри x, y;
бойовий — широта й довгота (сітка мапи після зміни перераховується, як при запуску).

Запис (POST /api/record/start, /api/record/stop) — теж у звичайному режимі: приймач можна носити,
а телефон, що почав запис, надсилає свої точки GPS (POST /api/gps) — це фактичне положення для
порівняння. Кожна точка приходить із тим, скільки мілісекунд тому телефон її отримав (age): час
точки рахується за годинником Pi, тож годинник телефона на це не впливає.
"""
import hmac
import json
import math
import os
import socket
import ssl
import shutil
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import calibration
from .config import DEFAULT_CONFIG, apply_field, field_spread_warning, save_config
from .geo import check_latlon, frame_from_config
from .packetlog import PacketFeed, format_row
from .protocol import norm_mac
from .solver import distance_from_loss

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 8192
TRAIL = 120
LOG_ROWS = 40
MAX_GPS_POINTS = 60          # точок GPS в одному запиті (телефон надсилає пачками, якщо зв’язок пропадав)
MAX_GPS_AGE = 600.0          # с: старіші точки (телефон довго був без зв’язку) не пишемо
DEFAULT_N10 = DEFAULT_CONFIG["signal"]["pl_10m"] - DEFAULT_CONFIG["signal"]["pl_1m"]


class EditError(ValueError):
    """Помилка зміни маяків, зрозуміла людині (повертається на сторінку)."""


def poweroff_command():
    """Команда вимкнення Pi: від root — напряму, інакше через sudo без пароля (дозвіл на саме цю
    команду додає install-raspberry.sh --service у /etc/sudoers.d/esp32-locator). None — не Linux."""
    systemctl = shutil.which("systemctl")
    if not systemctl or not sys.platform.startswith("linux"):
        return None
    return [systemctl, "poweroff"] if os.geteuid() == 0 else ["sudo", "-n", systemctl, "poweroff"]


def system_poweroff(check=False):
    """check=True — лише перевірити, що вимкнути можна (sudo -l), нічого не вимикаючи. Повертає True/False."""
    cmd = poweroff_command()
    if cmd is None:
        return False
    if check:
        if cmd[0] != "sudo":
            return True
        cmd = ["sudo", "-n", "-l", *cmd[2:]]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def next_name(transmitters):
    used = {t.get("name") for t in transmitters}
    return next((chr(c) for c in range(65, 91) if chr(c) not in used), str(len(transmitters) + 1))


class WebState:
    """Стан для сторінки й зміни маяків. cfg — той самий об’єкт, з яким працюють Locator і екран."""

    def __init__(self, cfg, cfg_path, readings, source, link=None, truth=None, pin=None, recorder=None):
        self.cfg, self.cfg_path = cfg, cfg_path          # cfg_path=None — лише перегляд (запис, відтворення)
        self.readings, self.source, self.link = readings, source, link
        # truth — де стоїть приймач (x, y), або функція без аргументів → (x, y) | None, якщо приймач
        # рухається (відтворення запису з GPS телефона): тоді положення показується як «GPS телефона»
        self.truth = truth
        self.recorder = recorder                          # session.Recorder — запис кнопкою на сторінці
        self.pin = str(pin) if pin else ""
        self.lock = threading.Lock()
        self.feed = PacketFeed(readings, maxlen=LOG_ROWS)
        self.trail = deque(maxlen=TRAIL)
        self.phone = None                                 # (x, y, acc, monotonic-час точки) — останній GPS
        self.phone_trail = deque(maxlen=TRAIL)
        self._phone_trail_at = 0.0
        self.cal_job = None                               # вимір калібрування, що йде зараз
        self.cal_last = None                              # {"ok"|"error": текст, "at": time.time()}
        self.cal_fit = calibration.fit(cfg.get("calibration") or [], DEFAULT_N10)
        self.poweroff = system_poweroff                   # у тестах підміняється
        self._json = b"{}"
        self._geo_src = object()
        self._frame = None
        self._reset_locator = False
        self._rate = (time.monotonic(), readings.total, 0.0)

    @property
    def editable(self):
        return self.cfg_path is not None

    @property
    def recording(self):
        return bool(self.recorder and self.recorder.active)

    @property
    def field(self):
        return self.cfg.get("mode") == "field"

    def frame(self):
        geo = self.cfg.get("geo")
        if geo is not self._geo_src:                      # сітка бойового режиму змінилась (нові маяки)
            self._geo_src, self._frame = geo, frame_from_config(geo)
            self.trail.clear()
            self.phone, self.phone_trail = None, deque(maxlen=TRAIL)
        return self._frame

    def _set_phone(self, x, y, acc, at):
        """Нова точка «GPS телефона» (живий GPS або трек із запису). Викликати під self.lock."""
        if not self.phone_trail or abs(at - self._phone_trail_at) >= 0.5:    # слід — не частіше ніж раз на 0,5 с
            self.phone_trail.append((round(x, 2), round(y, 2)))
            self._phone_trail_at = at
        if self.phone is None or at >= self.phone[3]:
            self.phone = (x, y, acc, at)

    # ------------------------------------------------------------ стан для сторінки
    def on_update(self, loc):
        """Викликається після кожного Locator.update(); готує JSON для GET /api/state."""
        with self.lock:
            if self._reset_locator:                       # координати маяків змінились — старе згладжене
                loc.pos, self._reset_locator = None, False  # положення в іншій сітці
                self.trail.clear()
            self.feed.update(self.cfg["transmitters"])
            self._cal_tick()
            frame = self.frame()
            fix = bool(loc.pos) and loc.heard >= 3
            if fix:
                self.trail.append((round(loc.pos[0], 2), round(loc.pos[1], 2)))
            now = time.monotonic()
            t0, n0, rate = self._rate
            if now - t0 >= 1.0:
                rate = (self.readings.total - n0) / (now - t0)
                self._rate = (now, self.readings.total, rate)

            def latlon(x, y):
                return [round(v, 7) for v in frame.to_latlon(x, y)] if frame else None

            truth = self.truth
            if callable(truth):                           # відтворення запису, де приймач носили
                p = truth()
                if p:
                    self._set_phone(p[0], p[1], None, now)
                truth = None
            phone = None
            if self.phone:
                x, y, acc, at = self.phone
                phone = {"x": round(x, 2), "y": round(y, 2), "latlon": latlon(x, y),
                         "acc": acc, "age": round(now - at, 1), "replay": callable(self.truth)}

            receiver = None
            if loc.pos:
                receiver = {"x": round(loc.pos[0], 2), "y": round(loc.pos[1], 2), "latlon": latlon(*loc.pos),
                            "rms": None if loc.rms is None else round(loc.rms, 2), "fix": fix}
            state = {
                "mode": self.cfg.get("mode", "local"),
                "area": self.cfg["area"],
                "editable": self.editable and not self.recording,
                "can_shutdown": self.editable,            # звичайний режим (служба), не запис і не відтворення
                "pin_required": bool(self.pin),
                "beacons": [{"name": t.get("name") or "", "mac": t["mac"], "x": t["x"], "y": t["y"],
                             "latlon": [t["lat"], t["lon"]] if "lat" in t else latlon(t["x"], t["y"]),
                             "rssi": round(r["rssi"], 1) if r else None,
                             "loss": round(r["pl"], 1) if r else None, "offset": t.get("pl_offset", 0.0),
                             "snr": round(r["snr"], 1) if r and r["snr"] is not None else None,
                             "packets": r["n"] if r else 0, "distance": None if d is None else round(d, 2)}
                            for t, r, d in loc.rows],
                "unknown": [{"mac": m, "rssi": round(r["rssi"], 1), "distance": round(d, 2)} for m, r, d in loc.unknown],
                "receiver": receiver,
                "heard": loc.heard,
                "truth": list(truth) if truth else None,
                "trail": list(self.trail),
                "phone": phone,
                "phone_trail": list(self.phone_trail),
                "recording": self.recorder.status() if self.recorder else None,
                "calibration": self._cal_state() if self.editable else None,
                "log": [{"text": format_row(r), "known": r.name is not None} for r in list(self.feed.rows)[-LOG_ROWS:]],
                "status": self.source.status,
                "link": self.link.status if self.link else None,
                "rate": round(rate, 1),
                "time": time.time(),
            }
            self._json = json.dumps(state, ensure_ascii=False).encode("utf-8")

    def state_json(self):
        with self.lock:
            return self._json

    # ------------------------------------------------------------ зміни маяків
    def _auth(self, pin):
        if self.pin and not hmac.compare_digest(str(pin or ""), self.pin):
            raise PermissionError("Неправильний PIN.")

    def check_pin(self, pin):
        if not self.editable:
            raise EditError("Під час запису й відтворення маяки змінювати не можна.")
        self._auth(pin)
        if self.recording:
            raise EditError("Йде запис — зупиніть його, щоб змінити маяки.")

    def _commit(self, mutate):
        """mutate(новий список маяків) → перевірка → заміна в cfg одним присвоєнням → збереження config.json."""
        with self.lock:
            new = json.loads(json.dumps(self.cfg["transmitters"]))
            message = mutate(new)
            trial = dict(self.cfg, transmitters=new)
            if self.field:
                try:
                    apply_field(trial)
                except ValueError as e:
                    raise EditError(str(e)) from None
            moved = self.field and trial.get("geo") != self.cfg.get("geo")
            self.cfg["transmitters"] = trial["transmitters"]
            if self.field:
                self.cfg["area"], self.cfg["geo"] = trial["area"], trial["geo"]
            save_config(self.cfg_path, self.cfg)
            if moved:
                self._reset_locator = True
            warning = field_spread_warning(self.cfg) if self.field else None
            return {"ok": True, "message": message, "warning": warning}

    def save_beacon(self, data):
        """Додає або оновлює маяк: {"mac", "name"?, "x", "y"} (тестовий) або {"mac", "name"?, "lat", "lon"} (бойовий)."""
        self.check_pin(data.get("pin"))
        mac = norm_mac(data.get("mac", ""))
        if not mac:
            raise EditError("Неправильний ID маяка. Приклад: A1B2C3D4E5F6")
        name = str(data.get("name") or "").strip()[:12]
        keys = ("lat", "lon") if self.field else ("x", "y")
        try:
            a, b = (float(data[k]) for k in keys)
        except (KeyError, TypeError, ValueError):
            raise EditError("Координати мають бути числами: " +
                            ("широта і довгота" if self.field else "x і y у метрах") + ".") from None
        if not all(map(math.isfinite, (a, b))):
            raise EditError("Координати мають бути скінченними числами.")
        if self.field:
            try:
                check_latlon(a, b)
            except ValueError as e:
                raise EditError(str(e).capitalize() + ".") from None
            coords = {"lat": round(a, 7), "lon": round(b, 7)}
        else:
            x, y = round(a, 2), round(b, 2)
            aw, ah = float(self.cfg["area"]["width"]), float(self.cfg["area"]["height"])
            if not (0 <= x <= aw and 0 <= y <= ah):
                raise EditError(f"Маяк поза зоною мапи 0…{aw:g} × 0…{ah:g} м.")
            coords = {"x": x, "y": y}

        def mutate(txs):
            t = next((t for t in txs if t["mac"] == mac), None)
            if t is None:
                txs.append({"name": name or next_name(txs), "mac": mac, **coords})
                return f"Зареєстровано {name or txs[-1]['name']}: {mac}"
            t.update(coords)
            if name:
                t["name"] = name
            return f"Оновлено {t.get('name') or mac}"
        return self._commit(mutate)

    def delete_beacon(self, data):
        self.check_pin(data.get("pin"))
        mac = norm_mac(data.get("mac", ""))

        def mutate(txs):
            before = len(txs)
            txs[:] = [t for t in txs if t["mac"] != mac]
            if len(txs) == before:
                raise EditError("Такого маяка немає.")
            return f"Видалено {mac}"
        return self._commit(mutate)

    # ------------------------------------------------------------ калібрування
    # Приймач стоїть на відомій відстані від маяка; за кілька секунд береться медіана втрат його пакетів.
    # Після кожного виміру модель перераховується з усіх вимірів (calibration.fit) і одразу діє:
    # Locator читає cfg["signal"] і pl_offset маяків на кожному кроці. Виміри й результат — у config.json.
    def _samples(self):
        return self.cfg.setdefault("calibration", [])

    def _cal_apply(self):
        """Перерахунок моделі з усіх вимірів і збереження. Викликати під self.lock."""
        # якщо нахил не виміряно, береться значення за замовчуванням, а не поточне: інакше після
        # видалення виміру на 10 м лишився б нахил, порахований із нього
        self.cal_fit = calibration.fit(self._samples(), DEFAULT_N10)
        if self.cal_fit:
            calibration.apply(self.cfg, self.cal_fit)
        save_config(self.cfg_path, self.cfg)

    def _cal_tick(self):
        """З on_update: збирає пакети маяка, що вимірюється; по завершенні — новий вимір і перерахунок."""
        job = self.cal_job
        if not job:
            return
        for p in self.readings.packets_after(job["seq"]):
            job["seq"] = p[0]
            if p[2] == job["mac"]:
                job["losses"].append(p[5] - p[3])         # потужність − RSSI
        if time.monotonic() < job["end"]:
            return
        self.cal_job = None
        name = job["name"] or job["mac"]
        n = len(job["losses"])
        if n < calibration.MIN_PACKETS:
            self.cal_last = {"error": f"{name}: за {job['seconds']:g} с прийнято пакетів: {n}, а потрібно щонайменше "
                                      f"{calibration.MIN_PACKETS} — вимір не зараховано. Чи увімкнено маяк?",
                             "at": time.time()}
            return
        loss = statistics.median(job["losses"])
        self._samples().append({"mac": job["mac"], "d": job["distance"], "loss": round(loss, 1), "n": n})
        try:
            self._cal_apply()
        except OSError as e:
            self.cal_last = {"error": f"Не вдалося зберегти config.json: {e}", "at": time.time()}
            return
        sig = self.cfg["signal"]
        text = (f"{name} на {job['distance']:g} м: втрати {loss:.1f} дБ, пакетів {n}. Модель оновлено: "
                f"{sig['pl_1m']:.1f} дБ на 1 м, {sig['pl_10m']:.1f} дБ на 10 м.")
        self.cal_last = {"ok": text.replace(".", ",").rstrip(",") + ".", "at": time.time()}

    def _cal_state(self):
        names = {t["mac"]: t.get("name") or t["mac"] for t in self.cfg["transmitters"]}
        offsets = {t["mac"]: t.get("pl_offset", 0.0) for t in self.cfg["transmitters"]}
        job = None
        if self.cal_job:
            j = self.cal_job
            job = {"mac": j["mac"], "name": j["name"], "distance": j["distance"], "packets": len(j["losses"]),
                   "left": round(max(0.0, j["end"] - time.monotonic()), 1),
                   "loss": round(statistics.median(j["losses"]), 1) if j["losses"] else None}
        samples = [{"mac": s["mac"], "name": names.get(s["mac"], s["mac"]), "d": s["d"], "loss": s["loss"],
                    "n": s.get("n"), "model_d": round(distance_from_loss(s["loss"], self.cfg, offsets.get(s["mac"], 0.0)), 2)}
                   for s in self._samples()]
        return {"signal": dict(self.cfg["signal"]), "samples": samples, "job": job, "last": self.cal_last,
                "fit": {k: self.cal_fit[k] for k in ("rms", "slope_measured", "notes")} if self.cal_fit else None}

    def calibration_measure(self, data):
        """Почати вимір: {"mac", "distance" (м), "seconds"?}."""
        if not self.editable:
            raise EditError("Калібрування — лише у звичайному режимі (не під час --record чи --replay).")
        self._auth(data.get("pin"))
        mac = norm_mac(data.get("mac", ""))
        t = next((t for t in self.cfg["transmitters"] if t["mac"] == mac), None)
        if t is None:
            raise EditError("Спершу зареєструйте маяк — калібрують зареєстровані маяки.")
        try:
            d, sec = float(data.get("distance")), float(data.get("seconds") or 10)
        except (TypeError, ValueError):
            raise EditError("Відстань має бути числом, у метрах.") from None
        if not (math.isfinite(d) and calibration.MIN_DISTANCE <= d <= calibration.MAX_DISTANCE):
            raise EditError(f"Відстань має бути від {calibration.MIN_DISTANCE:g} до {calibration.MAX_DISTANCE:g} м.")
        sec = min(max(sec if math.isfinite(sec) else 10.0, 5.0), 60.0)
        with self.lock:
            if self.cal_job:
                raise EditError("Вже йде вимір — дочекайтесь його або скасуйте.")
            self.cal_job = {"mac": mac, "name": t.get("name") or "", "distance": round(d, 2), "seconds": sec,
                            "end": time.monotonic() + sec, "seq": self.readings.total, "losses": []}
            self.cal_last = None
        return {"ok": True, "message": f"Вимірюю {t.get('name') or mac} на {d:g} м, {sec:g} с — не рухайте приймач."}

    def calibration_cancel(self, data):
        self._auth(data.get("pin"))
        with self.lock:
            self.cal_job = None
        return {"ok": True, "message": "Вимір скасовано."}

    def calibration_delete(self, data):
        """Видалити вимір {"index"} (номер у списку на сторінці); модель перераховується без нього."""
        if not self.editable:
            raise EditError("Калібрування — лише у звичайному режимі.")
        self._auth(data.get("pin"))
        with self.lock:
            samples = self._samples()
            try:
                i = int(data.get("index"))
                if not 0 <= i < len(samples):
                    raise ValueError
            except (TypeError, ValueError):
                raise EditError("Такого виміру немає.") from None
            samples.pop(i)
            self._cal_apply()
        return {"ok": True, "message": "Вимір видалено, модель перераховано."}

    def calibration_reset(self, data):
        """Усі виміри геть, модель — значення за замовчуванням, зсуви маяків — нуль."""
        if not self.editable:
            raise EditError("Калібрування — лише у звичайному режимі.")
        self._auth(data.get("pin"))
        with self.lock:
            self.cfg["calibration"] = []
            self.cfg["signal"] = {**self.cfg["signal"], **DEFAULT_CONFIG["signal"]}
            for t in self.cfg["transmitters"]:
                t.pop("pl_offset", None)
            self.cal_fit, self.cal_job, self.cal_last = None, None, None
            save_config(self.cfg_path, self.cfg)
        return {"ok": True, "message": "Калібрування скинуто до значень за замовчуванням."}

    # ------------------------------------------------------------ вимкнення Pi
    def shutdown(self, data):
        """Вимкнути Raspberry Pi з телефона: запис закривається, через 2 с — systemctl poweroff
        (відповідь сторінці встигає піти до того, як служба отримає SIGTERM)."""
        if not self.editable:
            raise EditError("Вимкнення — лише у звичайному режимі (служба на Pi).")
        self._auth(data.get("pin"))
        if not self.poweroff(check=True):
            raise EditError("Немає дозволу вимкнути Pi. Перевстановіть службу: ./install-raspberry.sh --service "
                            "--https (вона додає дозвіл на вимкнення).")
        saved = None
        if self.recording:
            res = self.recorder.stop()
            saved = res and res["path"] and os.path.basename(res["path"])

        def later():
            time.sleep(2)
            self.poweroff()
        threading.Thread(target=later, daemon=True).start()
        return {"ok": True, "message": ("Запис збережено: " + saved + ". " if saved else "")
                + "Pi вимикається. Від’єднуйте живлення, коли зелений світлодіод на Pi перестане блимати (~20–30 с)."}

    # ------------------------------------------------------------ запис і GPS телефона
    def _need_recorder(self, pin):
        if not self.recorder:
            raise EditError("Запис зі сторінки доступний лише у звичайному режимі (не під час --record чи --replay).")
        self._auth(pin)

    def record_start(self, data):
        self._need_recorder(data.get("pin"))
        note = str(data.get("note") or "").strip()[:200]
        try:
            path = self.recorder.start(note)
        except (ValueError, OSError) as e:
            raise EditError(f"Запис не почато: {e}") from None
        with self.lock:
            self.phone, self.phone_trail = None, deque(maxlen=TRAIL)
        warn = "Маяків менше 3 — положення не визначатиметься." if len(self.cfg["transmitters"]) < 3 else None
        return {"ok": True, "id": os.path.basename(path), "message": f"Запис почато: {os.path.basename(path)}",
                "warning": warn}

    def record_stop(self, data):
        self._need_recorder(data.get("pin"))
        res = self.recorder.stop()
        if res is None:
            raise EditError("Запис не йде.")
        if res["path"] is None:
            return {"ok": True, "message": "Пакетів не отримано — порожній запис видалено."}
        return {"ok": True, "message": f"Збережено {os.path.basename(res['path'])}: пакетів {res['packets']}, "
                                       f"точок GPS {res['gps']}, тривалість {res['duration']:.0f} с"}

    def post_gps(self, data):
        """Точки GPS телефона: {"points": [{"lat", "lon", "acc", "age"(мс)}, …]}. Пише їх у запис і показує на мапі."""
        self._need_recorder(data.get("pin"))
        points = data.get("points")
        if not isinstance(points, list) or len(points) > MAX_GPS_POINTS:
            raise EditError("Неправильний запит.")
        now = time.monotonic()
        good = []
        for p in points:
            try:
                lat, lon, age = float(p["lat"]), float(p["lon"]), float(p.get("age") or 0) / 1000
                acc = None if p.get("acc") is None else float(p["acc"])
                check_latlon(lat, lon)
            except (TypeError, KeyError, ValueError, AttributeError):
                continue                                  # одну зіпсовану точку пропускаємо, решту пишемо
            if not all(map(math.isfinite, (age, acc or 0.0))) or not 0 <= age <= MAX_GPS_AGE:
                continue
            good.append((lat, lon, None if acc is None else max(acc, 0.0), age))
        if not self.recording:
            return {"ok": True, "recording": False, "saved": 0}
        saved = sum(self.recorder.write_gps(*p) for p in good)
        with self.lock:
            frame = self.frame()
            if frame:
                for lat, lon, acc, age in sorted(good, key=lambda p: -p[3]):   # від старіших до новіших
                    self._set_phone(*frame.to_local(lat, lon), acc, now - age)
        return {"ok": True, "recording": self.recording, "saved": saved}


# ---------------------------------------------------------------- HTTP
def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        server_version = "esp32-locator"
        timeout = 30                                # завислий клієнт не тримає потік вічно

        def log_message(self, fmt, *args):          # без журналу кожного запиту в консоль
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, state.state_json())
            else:
                self._json(404, {"error": "Не знайдено"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            actions = {"/api/beacons": state.save_beacon, "/api/beacons/delete": state.delete_beacon,
                       "/api/record/start": state.record_start, "/api/record/stop": state.record_stop,
                       "/api/gps": state.post_gps,
                       "/api/calibration/measure": state.calibration_measure,
                       "/api/calibration/cancel": state.calibration_cancel,
                       "/api/calibration/delete": state.calibration_delete,
                       "/api/calibration/reset": state.calibration_reset,
                       "/api/shutdown": state.shutdown}
            if path not in actions:
                return self._json(404, {"error": "Не знайдено"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                return self._json(400, {"error": "Неправильний запит."})
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "Неправильний запит."})
            try:
                self._json(200, actions[path](data))
            except PermissionError as e:
                self._json(403, {"error": str(e)})
            except EditError as e:
                self._json(400, {"error": str(e)})
    return Handler


def ensure_certificate(directory):
    """Самопідписаний сертифікат для HTTPS (потрібен, щоб браузер дав сторінці GPS телефона).
    Створюється один раз через openssl. Повертає (cert, key)."""
    cert, key = os.path.join(directory, "web-cert.pem"), os.path.join(directory, "web-key.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        os.makedirs(directory, exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                        "-subj", "/CN=esp32-locator", "-keyout", key, "-out", cert],
                       check=True, capture_output=True)
        os.chmod(key, 0o600)
    return cert, key


class _Server(ThreadingHTTPServer):
    """HTTP(S)-сервер: кожне з’єднання — у своєму потоці, разом із TLS-рукостисканням.
    Якби рукостискання йшло в потоці, що приймає з’єднання, один клієнт, що підключився й мовчить
    (браузери телефонів відкривають з’єднання наперед), зупинив би сервер для всіх."""
    daemon_threads = True
    tls = None                                       # ssl.SSLContext або None

    def finish_request(self, request, client_address):
        if self.tls is None:
            return super().finish_request(request, client_address)
        request.settimeout(10)
        try:
            conn = self.tls.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):              # не TLS-клієнт, обрив, таймаут — просто закриваємо
            return
        try:
            super().finish_request(conn, client_address)
        finally:
            conn.close()

    def handle_error(self, request, client_address):
        # обірвані з’єднання з телефона — звичайна річ; решту помилок друкуємо як зазвичай
        import sys
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, ssl.SSLError)):
            super().handle_error(request, client_address)


def start_server(state, port, host="0.0.0.0", tls=None):
    """Запускає сервер у фоновому потоці. tls — (cert, key) для HTTPS. Повертає сервер (server_address — порт)."""
    server = _Server((host, port), make_handler(state))
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*tls)
        server.tls = ctx
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def addresses():
    """Адреси, за якими сторінку можна відкрити з телефона: ім’я.local і IP у мережі (без надсилання пакетів)."""
    out = [f"{socket.gethostname().split('.')[0]}.local"]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))              # лише вибір маршруту; пакет не надсилається
            ip = s.getsockname()[0]
        if not ip.startswith("127."):
            out.append(ip)
    except OSError:
        pass
    return out
