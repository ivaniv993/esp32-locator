"""Накопичення пакетів кожного маяка у ковзному вікні."""
import statistics
import threading
import time
from collections import deque


class Readings:
    """Зберігає пакети кожного маяка за останні window секунд.
    Час можна передавати явно (ts / now) — так алгоритм можна прогнати по запису;
    без нього береться time.monotonic()."""

    def __init__(self, window, keep_recent=0):
        """keep_recent > 0 — додатково пам’ятати останні пакети поштучно (для журналу на екрані)."""
        self.window = window
        self.lock = threading.Lock()
        self.data = {}
        self.total = 0
        self.recent = deque(maxlen=keep_recent) if keep_recent else None

    def add(self, mac, rssi, snr, txp, ts=None):
        if ts is None:
            ts = time.monotonic()
        with self.lock:
            self.data.setdefault(mac, deque()).append((ts, rssi, snr, txp))
            self.total += 1
            if self.recent is not None:
                self.recent.append((self.total, time.time(), mac, rssi, snr, txp))

    def packets_after(self, seq):
        """Пакети з номером більше seq у порядку надходження: (номер, час, id, rssi, snr, txp)."""
        with self.lock:
            return [p for p in self.recent if p[0] > seq] if self.recent is not None else []

    def snapshot(self, now=None):
        """{id: {rssi, snr, txp, pl, n, age}} — медіани за вікно; pl = потужність − RSSI (втрати, дБ)."""
        if now is None:
            now = time.monotonic()
        out = {}
        with self.lock:
            for mac, dq in self.data.items():
                while dq and now - dq[0][0] > self.window:
                    dq.popleft()
                if dq:
                    out[mac] = {
                        "rssi": statistics.median(v[1] for v in dq),
                        "snr": statistics.median(v[2] for v in dq if v[2] is not None) if any(v[2] is not None for v in dq) else None,
                        "txp": dq[-1][3],
                        "pl": statistics.median(v[3] - v[1] for v in dq),
                        "n": len(dq),
                        "age": now - dq[-1][0],
                    }
        return out
