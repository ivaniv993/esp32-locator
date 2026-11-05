"""Перерахунок між сіткою мапи (x, y у метрах) і географічними координатами (широта, довгота).

Сітка описується так (зберігається в записах і рахується в бойовому режимі, див. config.py):
    {"origin": [50.450100, 30.523400], "x_bearing_deg": 90}
origin — широта й довгота точки (0, 0) мапи; x_bearing_deg — азимут осі X від справжньої півночі
за годинниковою стрілкою (90 — X на схід, Y на північ, як у бойовому режимі).
Вісь Y повернута на 90° проти годинникової стрілки від X, як на мапі на екрані.

Для зони в сотні метрів Землю можна вважати плоскою: похибка перерахунку — міліметри.
Радіуси кривини беруться для еліпсоїда WGS84 на широті точки (0, 0).
"""
import math

_A = 6378137.0                    # велика піввісь WGS84, м
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)


class GeoFrame:
    def __init__(self, origin_lat, origin_lon, x_bearing_deg=90.0):
        check_latlon(origin_lat, origin_lon)
        self.lat0, self.lon0 = float(origin_lat), float(origin_lon)
        self.bearing = float(x_bearing_deg) % 360.0
        phi = math.radians(self.lat0)
        s = 1 - _E2 * math.sin(phi) ** 2
        self._m = _A * (1 - _E2) / s ** 1.5            # меридіональний радіус кривини
        self._n = _A / math.sqrt(s) * math.cos(phi)    # радіус паралелі
        b = math.radians(self.bearing)
        self._sin, self._cos = math.sin(b), math.cos(b)

    def _en(self, lat, lon):
        """Зсув точки від (0, 0) на схід і на північ, м."""
        return (math.radians(lon - self.lon0) * self._n, math.radians(lat - self.lat0) * self._m)

    def to_latlon(self, x, y):
        e = x * self._sin - y * self._cos
        n = x * self._cos + y * self._sin
        return self.lat0 + math.degrees(n / self._m), self.lon0 + math.degrees(e / self._n)

    def to_local(self, lat, lon):
        e, n = self._en(lat, lon)
        return e * self._sin + n * self._cos, -e * self._cos + n * self._sin


def check_latlon(lat, lon):
    if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
        raise ValueError(f"координати ({lat}; {lon}) поза межами: широта −90…90, довгота −180…180")


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _pair(value, key):
    if not (isinstance(value, (list, tuple)) and len(value) == 2 and all(_number(v) for v in value)):
        raise ValueError(f"geo.{key} має бути [широта, довгота], наприклад [50.4501, 30.5234]")
    check_latlon(*value)
    return float(value[0]), float(value[1])


def field_frame(points, margin):
    """Сітка бойового режиму: X — на схід, Y — на північ, (0, 0) — у південно-західному куті
    прямокутника навколо маяків з відступом margin метрів. points — [(широта, довгота)].
    Повертає (опис сітки, ширина, висота мапи в метрах)."""
    sw = GeoFrame(min(p[0] for p in points), min(p[1] for p in points), 90.0)
    lat0, lon0 = sw.to_latlon(-margin, -margin)
    frame = GeoFrame(lat0, lon0, 90.0)
    xy = [frame.to_local(*p) for p in points]
    # round перед ceil: частки міліметра від переходу до іншої широти не мають додавати цілий метр
    width = math.ceil(round(max(x for x, _ in xy) + margin, 2))
    height = math.ceil(round(max(y for _, y in xy) + margin, 2))
    return {"origin": [round(lat0, 8), round(lon0, 8)], "x_bearing_deg": 90.0}, width, height


def frame_from_config(geo):
    """GeoFrame з опису сітки {"origin": [...], "x_bearing_deg": ...} або None, якщо прив’язки немає.
    ValueError — з поясненням (наприклад, якщо опис у записі пошкоджено)."""
    if not geo:
        return None
    if not isinstance(geo, dict) or "origin" not in geo:
        raise ValueError('geo має містити "origin": [широта, довгота] точки (0, 0) мапи')
    lat0, lon0 = _pair(geo["origin"], "origin")
    bearing = geo.get("x_bearing_deg", 90.0)
    if not _number(bearing):
        raise ValueError("geo.x_bearing_deg має бути числом, градуси від півночі")
    return GeoFrame(lat0, lon0, bearing)


def format_latlon(lat, lon):
    """'50.450123, 30.523456' — як у Google Maps (6 знаків ≈ 0,1 м)."""
    return f"{lat:.6f}, {lon:.6f}"


def parse_latlon(lat_s, lon_s):
    """Розбирає широту й довготу з командного рядка; приймає і скопійоване з карти «50.4501, 30.5234»."""
    try:
        lat = float(lat_s.strip().rstrip(",;").replace(",", "."))
        lon = float(lon_s.strip().rstrip(",;").replace(",", "."))
    except ValueError:
        raise ValueError("широта й довгота мають бути числами, наприклад 50.4501 30.5234") from None
    check_latlon(lat, lon)
    return lat, lon
