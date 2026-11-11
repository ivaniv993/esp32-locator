"""Файл сесії запису: один файл JSON Lines на сесію.

Перший рядок — заголовок (зона, маяки з координатами і відстанню до приймача, справжнє положення
приймача, налаштування радіо), далі по рядку на кожен пакет від приймача з часом від початку запису:
    {"type":"header","version":2,"started_at":"2026-11-01T14:30:05","receiver_truth":{"x":3.5,"y":2.0},
     "radio":{"sf":7,"bw_khz":125},...}
    {"type":"rx","t":0.512,"line":"A1B2C3D4E5F6,-67.5,9.2,14"}
Запис з веб-сторінки (приймач носять): "receiver_truth": null, а фактичне положення — трек GPS
телефона; що визначила програма — раз на секунду:
    {"type":"gps","t":1.020,"lat":50.4501234,"lon":30.5234567,"acc":4.0}
    {"type":"est","t":1.200,"heard":3,"x":12.31,"y":8.02,"lat":50.4501301,"lon":30.5234401,"rms":1.2}
Невідомі типи рядків і поля при читанні пропускаються — формат можна розширювати.
"radio" і "distance" у маяків з’явились пізніше: у старих записах їх немає. Так само "rejected" в "est" —
список маяків, відкинутих як неузгоджені з рештою (є лише тоді, коли такі були).
Версія 2 — receiver_truth може бути null (версія 1 так не вміла).
"""
import bisect
import json
import math
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property

from .config import HERE
from .geo import format_latlon, frame_from_config
from .protocol import parse_line

FORMAT_VERSION = 2
EXT = ".jsonl"
MAX_GPS_ACC = 25.0     # м: гірші точки GPS телефона (ще без супутників) не беремо за фактичне положення
MAX_GPS_GAP = 5.0      # с: між точками GPS, віддаленими більше, положення не інтерполюється
EST_EVERY = 1.0        # с: як часто Recorder пише визначене програмою положення


def default_recordings_dir(cfg):
    """recordings_dir з config.json; «auto» — recordings/ у репозиторії, а якщо туди не можна
    писати (встановлено в /opt) — ~/.config/esp32-locator/recordings."""
    path = cfg.get("recordings_dir")
    if path and path != "auto":
        return os.path.expanduser(path)
    if os.access(HERE, os.W_OK):
        return os.path.join(HERE, "recordings")
    return os.path.join(os.path.expanduser("~"), ".config", "esp32-locator", "recordings")


def new_session_path(directory):
    """directory/2026-11-01_14-30-05.jsonl (з суфіксом, якщо такий файл уже є)."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(directory, stamp + EXT)
    n = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"{stamp}_{n}{EXT}")
        n += 1
    return path


def distance_to(truth, t):
    """Відстань від приймача до маяка за координатами, м (округлено до 0,1 — координати теж приблизні)."""
    return round(math.hypot(float(t["x"]) - truth[0], float(t["y"]) - truth[1]), 1)


def make_header(cfg, truth, note=""):
    """Заголовок сесії: усе, що потрібно, щоб відтворити запис без config.json.
    truth=None — приймач рухається (запис з веб-сторінки): фактичне положення пишеться рядками "gps"."""
    frame = frame_from_config(cfg.get("geo"))
    receiver = None
    if truth is not None:
        truth = float(truth[0]), float(truth[1])
        receiver = {"x": truth[0], "y": truth[1]}
        if frame:
            receiver["lat"], receiver["lon"] = (round(v, 8) for v in frame.to_latlon(*truth))
    return {
        "type": "header",
        "version": FORMAT_VERSION,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "mode": cfg.get("mode", "local"),    # "local" — тестовий, "field" — бойовий
        "area": dict(cfg["area"]),
        "transmitters": [{"name": t.get("name", ""), "mac": t["mac"], "x": t["x"], "y": t["y"],
                          **({"lat": t["lat"], "lon": t["lon"]} if "lat" in t else {}),
                          **({"pl_offset": t["pl_offset"]} if "pl_offset" in t else {}),
                          **({"distance": distance_to(truth, t)} if truth else {})}
                         for t in cfg["transmitters"]],
        "receiver_truth": receiver,
        "radio": dict(cfg["radio"]),         # SF і ширина каналу, з якими прошиті маяки (опис запису)
        "geo": cfg.get("geo"),               # сітка мапи відносно широти/довготи (None — тестовий режим)
        "signal": dict(cfg["signal"]),       # калібрування на момент запису (довідково)
        "baud": cfg["baud"],
        "default_tx_power_dbm": cfg["default_tx_power_dbm"],
    }


class SessionWriter:
    """Дописує пакети у файл сесії. Безпечний для виклику з потоку читання порту."""

    def __init__(self, path, header):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.counts = {}                       # {"rx": 812, "gps": 190, ...} — скільки рядків кожного типу
        self._lock = threading.Lock()
        self._f = open(path, "w", encoding="utf-8")
        self._write(header)
        self._t0 = time.monotonic()
        self._synced = self._t0

    @property
    def count(self):
        """Скільки пакетів від приймача записано."""
        return self.counts.get("rx", 0)

    def _write(self, obj):
        self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._f.flush()

    def write_event(self, kind, ago=0.0, **fields):
        """Записує рядок типу kind з часом від початку запису; ago — на скільки секунд раніше він стався."""
        now = time.monotonic()
        with self._lock:
            if self._f.closed:
                return
            self._write({"type": kind, "t": round(max(0.0, now - ago - self._t0), 3), **fields})
            self.counts[kind] = self.counts.get(kind, 0) + 1
            if now - self._synced >= 1.0:      # раз на секунду скидаємо на диск: переживе вимкнення живлення
                os.fsync(self._f.fileno())
                self._synced = now

    def write_rx(self, line):
        """Записує один рядок приймача з часом від початку запису."""
        self.write_event("rx", line=line.strip())

    @property
    def duration(self):
        return time.monotonic() - self._t0

    def close(self):
        with self._lock:
            if not self._f.closed:
                self._f.flush()
                os.fsync(self._f.fileno())
                self._f.close()


class Recorder:
    """Запис, який вмикають і вимикають під час роботи (кнопкою на веб-сторінці). Приймач можна
    носити: фактичне положення — точки GPS телефона (write_gps), а визначене програмою пишеться
    раз на секунду з on_update(loc). Маяки під час запису не змінюються (їх бере заголовок)."""

    def __init__(self, cfg, directory):
        self.cfg, self.directory = cfg, directory
        self.writer = None
        self._lock = threading.Lock()
        self._frame = None
        self._est_at = 0.0

    @property
    def active(self):
        return self.writer is not None

    def start(self, note=""):
        """Починає новий файл; повертає його шлях. ValueError — якщо почати не можна."""
        with self._lock:
            if self.writer:
                raise ValueError("Запис уже йде.")
            if not self.cfg["transmitters"]:
                raise ValueError("Немає зареєстрованих маяків — записувати нічого.")
            header = make_header(self.cfg, None, note)
            self._frame = frame_from_config(self.cfg.get("geo"))
            self._est_at = 0.0
            self.writer = SessionWriter(new_session_path(self.directory), header)
            return self.writer.path

    def stop(self):
        """Закриває файл. Повертає {"path", "packets", "gps", "duration"} або None, якщо запису не було.
        Файл без жодного пакета видаляється (path=None)."""
        with self._lock:
            w, self.writer = self.writer, None
        if w is None:
            return None
        w.close()
        out = {"path": w.path, "packets": w.count, "gps": w.counts.get("gps", 0), "duration": w.duration}
        if w.count == 0:
            os.remove(w.path)
            out["path"] = None
        return out

    def write_rx(self, line):
        w = self.writer
        if w:
            w.write_rx(line)

    def write_gps(self, lat, lon, acc, ago=0.0):
        """Точка GPS телефона; ago — скільки секунд тому телефон її отримав. False — запис не йде."""
        w = self.writer
        if not w:
            return False
        w.write_event("gps", ago, lat=round(lat, 7), lon=round(lon, 7),
                      acc=None if acc is None else round(acc, 1))
        return True

    def on_update(self, loc):
        """Після кожного перерахунку положення: раз на EST_EVERY с пише, що визначила програма."""
        w, now = self.writer, time.monotonic()
        if not w or now - self._est_at < EST_EVERY:
            return
        self._est_at = now
        est = {"heard": loc.heard}
        if loc.pos and loc.heard >= 3:
            est.update(x=round(loc.pos[0], 2), y=round(loc.pos[1], 2),
                       rms=None if loc.rms is None else round(loc.rms, 2))
            if loc.rejected:
                est["rejected"] = sorted(loc.rejected)
            if self._frame:
                est["lat"], est["lon"] = (round(v, 7) for v in self._frame.to_latlon(*loc.pos))
        w.write_event("est", **est)

    def status(self):
        w = self.writer
        if not w:
            return {"active": False}
        return {"active": True, "id": os.path.basename(w.path), "duration": round(w.duration, 1),
                "packets": w.count, "gps": w.counts.get("gps", 0)}


@dataclass
class Session:
    path: str
    header: dict
    rx: list = field(default_factory=list)      # [(t, line)] за зростанням t
    gps: list = field(default_factory=list)     # [(t, lat, lon, acc)] — GPS телефона (приймач носили)
    est: list = field(default_factory=list)     # рядки "est" як є — що програма показувала під час запису

    @property
    def truth(self):
        """Де стояв приймач (x, y), або None — його носили (тоді див. truth_at)."""
        t = self.header["receiver_truth"]
        return (float(t["x"]), float(t["y"])) if t else None

    @property
    def moving(self):
        return self.header["receiver_truth"] is None

    @cached_property
    def track(self):
        """Точки GPS телефона в метрах мапи: [(t, x, y, acc)], без неточних (acc > MAX_GPS_ACC)."""
        try:
            frame = frame_from_config(self.header.get("geo"))
        except ValueError:
            frame = None
        if frame is None:
            return []
        return [(t, *frame.to_local(lat, lon), acc) for t, lat, lon, acc in self.gps
                if acc is None or acc <= MAX_GPS_ACC]

    @cached_property
    def _track_t(self):
        return [p[0] for p in self.track]

    def truth_at(self, t):
        """Справжнє положення (x, y) у момент t: стале або інтерпольоване між точками GPS.
        None — поблизу немає точок GPS."""
        if not self.moving:
            return self.truth
        tr, ts = self.track, self._track_t
        if not tr:
            return None
        i = bisect.bisect_left(ts, t)
        if i == len(tr) or i == 0:                  # до першої чи після останньої точки — лише впритул
            p = tr[-1] if i else tr[0]
            return (p[1], p[2]) if abs(p[0] - t) <= 1.0 else None
        a, b = tr[i - 1], tr[i]
        if b[0] - a[0] > MAX_GPS_GAP:
            return None
        k = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
        return a[1] + k * (b[1] - a[1]), a[2] + k * (b[2] - a[2])

    def gps_accuracy(self):
        """Медіанна заявлена точність GPS телефона, м (None — немає даних)."""
        acc = [p[3] for p in self.track if p[3] is not None]
        return statistics.median(acc) if acc else None

    @property
    def duration(self):
        return self.rx[-1][0] if self.rx else 0.0

    @property
    def radio(self):
        """{"sf": 7, "bw_khz": 125} або None для записів, зроблених до появи цього поля."""
        return self.header.get("radio")

    @property
    def sf(self):
        return (self.radio or {}).get("sf")

    def distances(self):
        """{id маяка: відстань до приймача, м}; у старих записах рахується з координат.
        Якщо приймач носили — відстані немає (None)."""
        return {t["mac"]: t["distance"] if "distance" in t else (distance_to(self.truth, t) if self.truth else None)
                for t in self.header["transmitters"]}


def radio_label(radio):
    """'SF7, 125 кГц' або '—', якщо не вказано."""
    if not radio or radio.get("sf") is None:
        return "—"
    bw = radio.get("bw_khz")
    return f"SF{radio['sf']}" + (f", {bw:g} кГц" if bw is not None else "")


def _latlon_suffix(geo, xy):
    """' (50.450123, 30.523456)', якщо запис прив’язано до місцевості; інакше ''."""
    try:
        frame = frame_from_config(geo)
    except ValueError:
        return " (прив’язка geo у записі пошкоджена)"
    return f" ({format_latlon(*frame.to_latlon(*xy))})" if frame else ""


def describe(session, default_txp=14):
    """Текстовий опис запису: умови (SF, відстані) і скільки пакетів реально прийнято від кожного маяка."""
    h = session.header
    per = {}
    for _, line in session.rx:
        got = parse_line(line, default_txp)
        if got:
            per.setdefault(got[0], []).append(got[1])
    dur = session.duration
    tx = h["transmitters"]
    dist = session.distances()
    if session.truth:
        receiver = f"({session.truth[0]:g}; {session.truth[1]:g}) м{_latlon_suffix(h.get('geo'), session.truth)}"
    elif session.gps:
        acc = session.gps_accuracy()
        skipped = len(session.gps) - len(session.track)
        receiver = (f"рухався; фактичне положення — GPS телефона, точок: {len(session.track)}"
                    + (f", точність мед. ±{acc:.0f} м" if acc is not None else "")
                    + (f" ({skipped} неточних пропущено)" if skipped else ""))
    else:
        receiver = "рухався; фактичного положення немає (без GPS телефона)"
    lines = [
        session.path,
        f"  Записано: {h.get('started_at', '—')}   тривалість {dur:.0f} с, пакетів {len(session.rx)}",
        f"  Опис: {h.get('note') or '—'}",
        f"  Режим: {'бойовий (широта, довгота)' if h.get('mode') == 'field' else 'тестовий (метри)'}",
        f"  Радіо: {radio_label(session.radio) if session.radio else 'не вказано (старий запис)'}",
        f"  Приймач: {receiver}, зона {h['area']['width']:g} × {h['area']['height']:g} м",
        f"  {'Маяк':16} {'Відстань':>9} {'Пакетів':>8} {'Пак./с':>7} {'RSSI мед.':>10}",
    ]

    def row(label, d, rssi):
        rate = len(rssi) / dur if dur else 0.0
        med = f"{statistics.median(rssi):.0f} дБм" if rssi else "—"
        d_s = f"{d:.1f} м" if d is not None else "—"
        return f"  {label:16} {d_s:>9} {len(rssi):>8} {rate:>7.1f} {med:>10}"

    for t in tx:
        lines.append(row(f"{t.get('name') or '—'}  …{t['mac'][-6:]}", dist[t["mac"]], per.get(t["mac"], [])))
    known = {t["mac"] for t in tx}
    for mac in sorted(m for m in per if m not in known):
        lines.append(row(f"?  …{mac[-6:]}", None, per[mac]) + "  (не зареєстровано)")
    return "\n".join(lines)


def load_session(path):
    """Читає файл сесії. Недописаний останній рядок (обрив живлення) ігнорується."""
    with open(path, encoding="utf-8") as f:
        lines = [(n, s) for n, s in enumerate(f, 1) if s.strip()]
    if not lines:
        raise ValueError(f"{path}: порожній файл")
    records = []
    for i, (n, s) in enumerate(lines):
        try:
            records.append(json.loads(s))
        except json.JSONDecodeError as e:
            if i == len(lines) - 1 and i > 0:
                break
            raise ValueError(f"{path}, рядок {n}: {e}") from None
    header = records[0]
    if header.get("type") != "header":
        raise ValueError(f"{path}: перший рядок має бути заголовком")
    if header.get("version", 0) > FORMAT_VERSION:
        raise ValueError(f"{path}: формат версії {header['version']} новіший за підтримуваний ({FORMAT_VERSION})")
    for key in ("area", "transmitters", "receiver_truth"):
        if key not in header:
            raise ValueError(f"{path}: у заголовку немає «{key}»")
    rx = sorted(((float(r["t"]), r["line"]) for r in records[1:] if r.get("type") == "rx"),
                key=lambda e: e[0])
    gps = sorted((float(r["t"]), float(r["lat"]), float(r["lon"]), None if r.get("acc") is None else float(r["acc"]))
                 for r in records[1:] if r.get("type") == "gps")
    est = [r for r in records[1:] if r.get("type") == "est"]
    return Session(path=path, header=header, rx=rx, gps=gps, est=est)


def session_config(cfg, session):
    """Копія cfg для відтворення: зона і маяки беруться із запису, а параметри алгоритму
    (калібрування, згладжування, вікно) — з поточного config.json, щоб їх можна було змінювати.
    Зсув маяка (pl_offset) — теж поточний, якщо маяк є в config.json; інакше — із запису."""
    out = json.loads(json.dumps(cfg))
    out["area"] = dict(session.header["area"])
    out["mode"] = session.header.get("mode", "local")
    out["geo"] = session.header.get("geo")     # координати маяків у записі відносно його прив’язки
    current = {t["mac"]: t.get("pl_offset") for t in cfg["transmitters"]}
    out["transmitters"] = []
    for t in session.header["transmitters"]:
        t = dict(t, x=float(t["x"]), y=float(t["y"]))
        if t["mac"] in current:
            t.pop("pl_offset", None)
            if current[t["mac"]]:
                t["pl_offset"] = current[t["mac"]]
        out["transmitters"].append(t)
    return out
