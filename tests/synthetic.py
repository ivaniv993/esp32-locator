"""Синтетичні записи для тестів: приймач стоїть на місці, RSSI — за моделлю log-distance + шум."""
import json
import math
import random

from esp32_locator.config import DEFAULT_CONFIG
from esp32_locator.session import FORMAT_VERSION, Session

BEACONS = [("A", "AAAAAAAAAAAA", 0.0, 0.0), ("B", "BBBBBBBBBBBB", 10.0, 0.0), ("C", "CCCCCCCCCCCC", 5.0, 8.0)]


def default_cfg():
    return json.loads(json.dumps(DEFAULT_CONFIG))


def make_session(truth=(3.5, 2.0), duration=30.0, noise_db=0.0, seed=1, beacons=BEACONS,
                 pl_1m=40.0, pl_10m=67.0, rate=2.0, path="synthetic.jsonl"):
    """Запис у пам’яті. noise_db — стандартне відхилення шуму RSSI, дБ; seed робить його відтворюваним."""
    rng = random.Random(seed)
    header = {
        "type": "header", "version": FORMAT_VERSION, "started_at": "2026-01-01T00:00:00", "note": "synthetic",
        "area": {"width": 10, "height": 8},
        "transmitters": [{"name": n, "mac": m, "x": x, "y": y} for n, m, x, y in beacons],
        "receiver_truth": {"x": truth[0], "y": truth[1]},
        "signal": {"pl_1m": pl_1m, "pl_10m": pl_10m}, "baud": 115200, "default_tx_power_dbm": 14,
    }
    rx = []
    for i in range(int(duration * rate)):
        for k, (_, mac, bx, by) in enumerate(beacons):
            d = max(math.hypot(truth[0] - bx, truth[1] - by), 0.1)
            loss = pl_1m + (pl_10m - pl_1m) * math.log10(d) + rng.gauss(0, noise_db)
            rx.append((round(i / rate + 0.01 * k, 3), f"{mac},{14 - loss:.1f},8.0,14"))
    return Session(path=path, header=header, rx=rx)
