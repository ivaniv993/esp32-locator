"""Конфігурація: config.json з маяками, калібруванням і налаштуваннями екрана."""
import json
import os

from .protocol import norm_mac

# Папка з locator.py (на рівень вище за пакет).
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Інсталятор задає ESP32_LOCATOR_CONFIG (~/.config/esp32-locator/config.json);
# без нього config.json лежить поруч із програмою.
CONFIG_PATH = os.environ.get("ESP32_LOCATOR_CONFIG") or os.path.join(HERE, "config.json")

DEFAULT_CONFIG = {
    "serial_port": "auto",
    "baud": 115200,
    "area": {"width": 10, "height": 8},
    "signal": {"pl_1m": 40.0, "pl_10m": 67.0},   # втрати сигналу (дБ) на 1 м і на 10 м
    "default_tx_power_dbm": 14,                    # якщо приймач не передає потужність
    "window_sec": 3.0,                             # маяк шле 2 пакети/с — беремо медіану за 3 с
    "smoothing": 0.35,
    "fullscreen": False,
    "recordings_dir": "auto",                      # куди --record пише сесії (auto — recordings/ у репо)
    "transmitters": [],
}


def load_config(path):
    if not os.path.exists(path):
        save_config(path, DEFAULT_CONFIG)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update(cfg)
    if "pl_1m" not in merged["signal"]:
        # старий формат (сила 0–1000) — переходимо на модель в децибелах
        merged["signal"] = dict(DEFAULT_CONFIG["signal"])
        merged["window_sec"] = max(float(merged.get("window_sec", 3.0)), 3.0)
        save_config(path, merged)
        print("config.json оновлено під LoRa-маяки. Відкалібруйте pl_1m і pl_10m (--calibrate).")
    for t in merged["transmitters"]:
        t["mac"] = norm_mac(t["mac"]) or t["mac"]
        t["x"], t["y"] = float(t["x"]), float(t["y"])
    return merged


def save_config(path, cfg):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
