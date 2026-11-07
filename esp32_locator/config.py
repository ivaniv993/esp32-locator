"""Конфігурація: config.json з маяками, калібруванням і налаштуваннями екрана.

Два режими ("mode"):
  "local" — тестовий (за замовчуванням): маяки задаються в метрах (x, y) від обраної вами точки (0, 0),
            розмір мапи — area;
  "field" — бойовий: маяки задаються широтою й довготою (lat, lon). Сітка мапи будується сама:
            X — на схід, Y — на північ, (0, 0) — у південно-західному куті прямокутника навколо маяків
            з відступом field_margin_m; area теж рахується сама. Азимут не потрібен.
Усередині програми обидва режими працюють у метрах (x, y); у бойовому x, y, area і geo — похідні
й у config.json не зберігаються.
"""
import json
import os

from .geo import check_latlon, field_frame, frame_from_config
from .protocol import norm_mac

MODES = {"local": "тестовий", "field": "бойовий"}

# Папка з locator.py (на рівень вище за пакет).
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Інсталятор задає ESP32_LOCATOR_CONFIG (~/.config/esp32-locator/config.json);
# без нього config.json лежить поруч із програмою.
CONFIG_PATH = os.environ.get("ESP32_LOCATOR_CONFIG") or os.path.join(HERE, "config.json")

DEFAULT_CONFIG = {
    "mode": "local",                               # "local" — тестовий, "field" — бойовий (див. вище)
    "field_margin_m": 10.0,                        # бойовий режим: відступ мапи навколо маяків, м
    "serial_port": "auto",
    "baud": 115200,
    "area": {"width": 10, "height": 8},
    "signal": {"pl_1m": 40.0, "pl_10m": 67.0},   # втрати сигналу (дБ) на 1 м і на 10 м
    "default_tx_power_dbm": 14,                    # якщо приймач не передає потужність
    "radio": {"sf": 12, "bw_khz": 125},            # як прошиті маяки й приймач; приймач цього не повідомляє,
                                                   # тому значення лише записується в сесії для опису
    "window_sec": 3.0,                             # маяк шле 2 пакети/с — беремо медіану за 3 с
    "smoothing": 0.35,
    "fullscreen": False,
    "recordings_dir": "auto",                      # куди --record пише сесії (auto — recordings/ у репо)
    # передача положення в ArduPilot (--mavlink ПОРТ, лише бойовий режим): швидкість порту, стала висота
    # над рівнем моря (маяки висоти не міряють), найменша заявлена похибка положення
    "mavlink": {"baud": 115200, "alt_m": 0.0, "min_accuracy_m": 2.0},
    "transmitters": [],
}
FIELD_SPREAD_WARN_M = 1000      # маяки розкидані ширше — імовірно, помилка в координатах


def load_config(path):
    """Читає config.json і доповнює значеннями за замовчуванням. ValueError — з поясненням для людини."""
    if not os.path.exists(path):
        save_config(path, DEFAULT_CONFIG)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update(cfg)
    merged["radio"] = {**DEFAULT_CONFIG["radio"], **cfg.get("radio", {})}
    merged["mavlink"] = {**DEFAULT_CONFIG["mavlink"], **cfg.get("mavlink", {})}
    if "pl_1m" not in merged["signal"]:
        # старий формат (сила 0–1000) — переходимо на модель в децибелах
        merged["signal"] = dict(DEFAULT_CONFIG["signal"])
        merged["window_sec"] = max(float(merged.get("window_sec", 3.0)), 3.0)
        save_config(path, merged)
        print("config.json оновлено під LoRa-маяки. Відкалібруйте pl_1m і pl_10m (--calibrate).")
    for t in merged["transmitters"]:
        t["mac"] = norm_mac(t["mac"]) or t["mac"]
    if merged["mode"] not in MODES:
        raise ValueError(f'mode має бути "local" (тестовий) або "field" (бойовий), а не {merged["mode"]!r}')
    if merged["mode"] == "field":
        apply_field(merged)
    else:
        for t in merged["transmitters"]:
            if "x" not in t or "y" not in t:
                raise ValueError(f"маяк {t.get('name') or t['mac']}: у тестовому режимі потрібні x і y "
                                 f"(--add ID X Y) — або перейдіть у бойовий режим (\"mode\": \"field\")")
            t["x"], t["y"] = float(t["x"]), float(t["y"])
        merged["geo"] = None
    return merged


def apply_field(cfg):
    """Бойовий режим: рахує сітку (geo), розмір мапи (area) і x, y маяків з їхніх lat, lon."""
    txs = cfg["transmitters"]
    for t in txs:
        if "lat" not in t or "lon" not in t:
            raise ValueError(f"маяк {t.get('name') or t['mac']}: у бойовому режимі потрібні lat і lon "
                             f"(--add-geo ID ШИРОТА ДОВГОТА)")
        try:
            t["lat"], t["lon"] = float(t["lat"]), float(t["lon"])
        except (TypeError, ValueError):
            raise ValueError(f"маяк {t.get('name') or t['mac']}: lat і lon мають бути числами") from None
        check_latlon(t["lat"], t["lon"])
    margin = float(cfg["field_margin_m"])
    if margin < 0:
        raise ValueError("field_margin_m не може бути від’ємним")
    if not txs:
        cfg["geo"] = None
        return
    cfg["geo"], width, height = field_frame([(t["lat"], t["lon"]) for t in txs], margin)
    cfg["area"] = {"width": width, "height": height}
    frame = frame_from_config(cfg["geo"])
    for t in txs:
        x, y = frame.to_local(t["lat"], t["lon"])
        t["x"], t["y"] = round(x, 2), round(y, 2)


def field_spread_warning(cfg):
    """Попередження, якщо маяки бойового режиму розкидані підозріло широко (переплутано широту й довготу?)."""
    a = cfg["area"]
    if cfg["mode"] == "field" and max(a["width"], a["height"]) > FIELD_SPREAD_WARN_M:
        return (f"Увага: маяки займають {a['width']:g} × {a['height']:g} м — перевірте координати "
                f"(можливо, переплутано широту й довготу).")
    return None


def save_config(path, cfg):
    """Зберігає config.json. Похідні поля бойового режиму (geo, area, x, y маяків) не зберігаються."""
    out = json.loads(json.dumps(cfg))
    out.pop("geo", None)
    if out.get("mode") == "field":
        out.pop("area", None)
        out["transmitters"] = [{k: t[k] for k in ("name", "mac", "lat", "lon") if k in t}
                               for t in out["transmitters"]]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
