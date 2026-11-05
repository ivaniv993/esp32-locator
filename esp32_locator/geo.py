"""Прив’язка локальної сітки мапи (x, y у метрах) до географічних координат (широта, довгота).

Сітка задається в config.json:
    "geo": {"origin": [50.450100, 30.523400],           — широта, довгота точки (0, 0) мапи
            "x_axis_point": [50.450100, 30.524100]}     — будь-яка точка на осі X (праворуч від (0, 0))
або замість x_axis_point — "x_bearing_deg": 90 (азимут осі X від справжньої півночі за годинниковою
стрілкою; 90 — X на схід, Y на північ). Без обох — X на схід, Y на північ.
Вісь Y повернута на 90° проти годинникової стрілки від X, як на мапі на екрані.

Для зони в сотні метрів Землю можна вважати плоскою: похибка перерахунку — міліметри.
Радіуси кривини беруться для еліпсоїда WGS84 на широті точки (0, 0).
"""
import math

_A = 6378137.0                    # велика піввісь WGS84, м
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
MIN_AXIS_POINT_M = 5.0            # ближча точка на осі X дає ненадійний напрям


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


def _pair(value, key):
    if not (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
        raise ValueError(f"geo.{key} має бути [широта, довгота], наприклад [50.4501, 30.5234]")
    check_latlon(*value)
    return float(value[0]), float(value[1])


def frame_from_config(geo):
    """GeoFrame з розділу geo config.json або None, якщо прив’язки немає. ValueError — з поясненням."""
    if not geo:
        return None
    if not isinstance(geo, dict) or "origin" not in geo:
        raise ValueError('geo має містити "origin": [широта, довгота] точки (0, 0) мапи')
    lat0, lon0 = _pair(geo["origin"], "origin")
    if "x_axis_point" in geo and "x_bearing_deg" in geo:
        raise ValueError("у geo вкажіть або x_axis_point, або x_bearing_deg, а не обидва")
    if "x_axis_point" in geo:
        north_up = GeoFrame(lat0, lon0, 90.0)          # x = схід, y = північ
        e, n = north_up.to_local(*_pair(geo["x_axis_point"], "x_axis_point"))
        if math.hypot(e, n) < MIN_AXIS_POINT_M:
            raise ValueError(f"geo.x_axis_point ближче ніж {MIN_AXIS_POINT_M:g} м до origin — напрям осі X ненадійний")
        return GeoFrame(lat0, lon0, math.degrees(math.atan2(e, n)))
    bearing = geo.get("x_bearing_deg", 90.0)
    if not isinstance(bearing, (int, float)) or isinstance(bearing, bool):
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
