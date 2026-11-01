"""Файл сесії запису: один файл JSON Lines на сесію.

Перший рядок — заголовок (зона, маяки з координатами, справжнє положення приймача),
далі по рядку на кожен пакет від приймача з часом від початку запису:
    {"type":"header","version":1,"started_at":"2026-11-01T14:30:05","receiver_truth":{"x":3.5,"y":2.0},...}
    {"type":"rx","t":0.512,"line":"A1B2C3D4E5F6,-67.5,9.2,14"}
Невідомі типи рядків при читанні пропускаються — формат можна розширювати.
"""
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from .config import HERE

FORMAT_VERSION = 1
EXT = ".jsonl"


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


def make_header(cfg, truth, note=""):
    """Заголовок сесії: усе, що потрібно, щоб відтворити запис без config.json."""
    return {
        "type": "header",
        "version": FORMAT_VERSION,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "area": dict(cfg["area"]),
        "transmitters": [{"name": t.get("name", ""), "mac": t["mac"], "x": t["x"], "y": t["y"]}
                         for t in cfg["transmitters"]],
        "receiver_truth": {"x": float(truth[0]), "y": float(truth[1])},
        "signal": dict(cfg["signal"]),       # калібрування на момент запису (довідково)
        "baud": cfg["baud"],
        "default_tx_power_dbm": cfg["default_tx_power_dbm"],
    }


class SessionWriter:
    """Дописує пакети у файл сесії. Безпечний для виклику з потоку читання порту."""

    def __init__(self, path, header):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.count = 0
        self._lock = threading.Lock()
        self._f = open(path, "w", encoding="utf-8")
        self._write(header)
        self._t0 = time.monotonic()
        self._synced = self._t0

    def _write(self, obj):
        self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._f.flush()

    def write_rx(self, line):
        """Записує один рядок приймача з часом від початку запису."""
        now = time.monotonic()
        with self._lock:
            if self._f.closed:
                return
            self._write({"type": "rx", "t": round(now - self._t0, 3), "line": line.strip()})
            self.count += 1
            if now - self._synced >= 1.0:      # раз на секунду скидаємо на диск: переживе вимкнення живлення
                os.fsync(self._f.fileno())
                self._synced = now

    @property
    def duration(self):
        return time.monotonic() - self._t0

    def close(self):
        with self._lock:
            if not self._f.closed:
                self._f.flush()
                os.fsync(self._f.fileno())
                self._f.close()


@dataclass
class Session:
    path: str
    header: dict
    rx: list = field(default_factory=list)      # [(t, line)] за зростанням t

    @property
    def truth(self):
        t = self.header["receiver_truth"]
        return float(t["x"]), float(t["y"])

    @property
    def duration(self):
        return self.rx[-1][0] if self.rx else 0.0


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
    return Session(path=path, header=header, rx=rx)


def session_config(cfg, session):
    """Копія cfg для відтворення: зона і маяки беруться із запису, а параметри алгоритму
    (калібрування, згладжування, вікно) — з поточного config.json, щоб їх можна було змінювати."""
    out = json.loads(json.dumps(cfg))
    out["area"] = dict(session.header["area"])
    out["transmitters"] = [dict(t, x=float(t["x"]), y=float(t["y"])) for t in session.header["transmitters"]]
    return out
