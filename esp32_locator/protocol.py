"""Формат рядків, які приймач друкує в Serial: chipId,rssi_dBm[,snr_dB[,txPower_dBm]]."""
import re

# chipId,rssi_dBm[,snr_dB[,txPower_dBm]]
LINE_RE = re.compile(r"^\s*([0-9A-Fa-f:\-]{12,17})\s*,\s*(-?\d+(?:\.\d+)?)"
                     r"(?:\s*,\s*(-?\d+(?:\.\d+)?))?(?:\s*,\s*(-?\d+(?:\.\d+)?))?\s*$")


def norm_mac(s):
    """Повертає ID маяка так, як його друкує прошивка (A1B2C3D4E5F6), або None.
    Приймає і формат з двокрапками чи дефісами."""
    s = str(s).strip()
    if not re.fullmatch(r"[0-9A-Fa-f:\-.\s]+", s):
        return None
    h = re.sub(r"[^0-9A-Fa-f]", "", s).upper()
    if len(h) != 12:
        return None
    return h


def parse_line(line, default_txp):
    """Розбирає рядок приймача. Повертає (id, rssi, snr, txp) або None.
    Рядки, що починаються з «#», заголовок і службові повідомлення ігноруються."""
    m = LINE_RE.match(line)
    if not m:
        return None
    mac = norm_mac(m.group(1))
    rssi = float(m.group(2))
    if not mac or rssi >= 0:          # RSSI завжди від’ємний; інше — сміття
        return None
    snr = float(m.group(3)) if m.group(3) is not None else None
    txp = float(m.group(4)) if m.group(4) is not None else float(default_txp)
    return mac, rssi, snr, txp
