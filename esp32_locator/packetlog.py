"""Журнал пакетів для екрана: що саме і в якому порядку прийшло від приймача."""
import time
from collections import deque, namedtuple

# gap — секунд від попереднього пакета цього ж маяка (None для першого); name — None, якщо маяк не зареєстровано
Row = namedtuple("Row", "wall mac name rssi snr gap")


class PacketFeed:
    """Забирає нові пакети з Readings (потрібен keep_recent) і зберігає останні maxlen рядків."""

    def __init__(self, readings, maxlen=200):
        self.readings = readings
        self.rows = deque(maxlen=maxlen)
        self._seq = readings.total
        self._last = {}                     # mac -> wall-час попереднього пакета

    def update(self, transmitters):
        names = {t["mac"]: t.get("name") or t["mac"][-5:] for t in transmitters}
        for seq, wall, mac, rssi, snr, _txp in self.readings.packets_after(self._seq):
            prev = self._last.get(mac)
            self.rows.append(Row(wall, mac, names.get(mac), rssi, snr, None if prev is None else wall - prev))
            self._last[mac] = wall
            self._seq = seq


def format_row(r):
    """'14:30:05.123    A  …E5F6   -67 дБм  SNR  9   +0,5 с'"""
    ms = int(r.wall % 1 * 1000)
    clock = f"{time.strftime('%H:%M:%S', time.localtime(r.wall))}.{ms:03d}"
    name = r.name or "?"
    snr = f"{r.snr:3.0f}" if r.snr is not None else "  —"
    gap = f"+{r.gap:.1f} с".replace(".", ",") if r.gap is not None else "перший"
    return f"{clock}  {name:>3}  …{r.mac[-4:]}  {r.rssi:4.0f} дБм  SNR {snr}  {gap}"
