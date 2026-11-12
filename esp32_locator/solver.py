"""Алгоритм визначення положення: втрати сигналу → відстані → трилатерація (з відкиданням
неузгоджених маяків) → згладжування."""
import math


def distance_from_loss(pl, cfg, offset=0.0):
    """Модель log-distance: втрати ростуть на (pl_10m − pl_1m) дБ при кожному 10-кратному збільшенні відстані.
    offset — власний зсув маяка з калібрування (pl_offset), дБ."""
    p1, p10 = cfg["signal"]["pl_1m"], cfg["signal"]["pl_10m"]
    return 10 ** ((pl - offset - p1) / max(1.0, p10 - p1))


def solve(pts):
    """pts: [(x, y, d)]. Зважений метод найменших квадратів (Левенберг–Марквардт).
    Повертає (x, y, rms) — rms показує, наскільки виміри узгоджуються між собою."""
    if not pts:
        return None

    def w(d):
        return 1.0 / max(d, 0.3) ** 2

    sw = sum(w(d) for _, _, d in pts)
    x = sum(w(d) * px for px, _, d in pts) / sw
    y = sum(w(d) * py for _, py, d in pts) / sw

    def cost(cx, cy):
        return sum(w(d) * (math.hypot(cx - px, cy - py) - d) ** 2 for px, py, d in pts)

    if len(pts) > 1:
        c, lam = cost(x, y), 1e-3
        for _ in range(80):
            a = b = cc = g1 = g2 = 0.0
            for px, py, d in pts:
                dx, dy = x - px, y - py
                r = math.hypot(dx, dy) or 1e-6
                wi, res, jx, jy = w(d), r - d, dx / r, dy / r
                a += wi * jx * jx; b += wi * jx * jy; cc += wi * jy * jy
                g1 += wi * jx * res; g2 += wi * jy * res
            A, C = a * (1 + lam) + 1e-9, cc * (1 + lam) + 1e-9
            det = A * C - b * b
            if abs(det) < 1e-14:
                break
            sx, sy = -(C * g1 - b * g2) / det, -(A * g2 - b * g1) / det
            nc = cost(x + sx, y + sy)
            if nc < c:
                x, y, c, lam = x + sx, y + sy, nc, lam * 0.3
                if math.hypot(sx, sy) < 1e-4:
                    break
            else:
                lam *= 10
                if lam > 1e9:
                    break
    rms = math.sqrt(sum((math.hypot(x - px, y - py) - d) ** 2 for px, py, d in pts) / len(pts))
    return x, y, rms


def mismatch_db(x, y, pt, slope):
    """Наскільки маяк pt = (px, py, d) не згоден з положенням (x, y): різниця між відстанню до нього
    і виміряною, переведена в дБ втрат (slope — дБ на 10-кратне збільшення відстані)."""
    px, py, d = pt
    return slope * math.log10(max(math.hypot(x - px, y - py), 0.3) / max(d, 0.3))


def solve_consistent(pts, slope, max_db=12.0):
    """solve() з відкиданням маяків, що не узгоджуються з рештою (перебір «без кожного по черзі»).
    Для кожного маяка положення рахується без нього; вибирається той, без якого решта узгоджується
    найкраще. Він відкидається, якщо не згоден з цим положенням більше ніж на max_db. Перебір
    повторюється, але щонайменше 3 маяки лишаються завжди — тож відкидати можна лише з 4 і більше.
    Поріг у дБ, а не в метрах, щоб однаково працювати в кімнаті й у полі; ~5 розкидів RSSI дає
    близько 1–2 % хибних відкидань.
    Повертає (x, y, rms, rejected) — rejected: індекси відкинутих у pts — або None."""
    res = solve(pts)
    if res is None:
        return None
    keep, rejected = list(range(len(pts))), []
    while len(keep) >= 4:
        best = None
        for i in keep:
            rest = [pts[j] for j in keep if j != i]
            sub = solve(rest)
            spread = math.sqrt(sum(mismatch_db(sub[0], sub[1], p, slope) ** 2 for p in rest) / len(rest))
            if best is None or spread < best[0]:
                best = (spread, i, sub)
        _, i, sub = best
        if abs(mismatch_db(sub[0], sub[1], pts[i], slope)) <= max_db:
            break
        keep.remove(i)
        rejected.append(i)
        res = sub
    return (*res, rejected)


MIN_BEACONS = 3       # менше маяків — положення не визначається


class Locator:
    """Об’єднує виміри, розв’язок і згладжування.
    Положення рахується, лише коли чути щонайменше MIN_BEACONS маяків; інакше pos = None.
    Після такої перерви перше положення береться як є, без згладжування від старої точки.
    Так само, коли змінились координати маяків (перетягнули на екрані, змінили на сторінці чи через API,
    у бойовому режимі зсунулась сітка): старе згладжене положення пораховане за іншими координатами."""

    def __init__(self, cfg, readings):
        self.cfg, self.readings = cfg, readings
        self.pos = None      # згладжене положення (x, y); None — чути менше MIN_BEACONS маяків
        self.rms = None
        self.rows = []       # стан кожного передавача для екрана
        self.rejected = set()  # маяки (mac), відкинуті як неузгоджені з рештою
        self.unknown = []
        self.heard = 0
        self.moved = False   # на цьому кроці координати маяків змінились — згладжування почалось заново
        self._layout = self._beacon_layout()

    def _beacon_layout(self):
        return tuple((t["mac"], t["x"], t["y"]) for t in self.cfg["transmitters"])

    def update(self, now=None):
        layout = self._beacon_layout()
        self.moved = layout != self._layout
        if self.moved:
            self._layout, self.pos = layout, None
        snap = self.readings.snapshot(now)
        known = {t["mac"] for t in self.cfg["transmitters"]}
        pts, used, rows = [], [], []
        for t in self.cfg["transmitters"]:
            r = snap.get(t["mac"])
            if r:
                d = distance_from_loss(r["pl"], self.cfg, t.get("pl_offset", 0.0))
                pts.append((t["x"], t["y"], d))
                used.append(t["mac"])
                rows.append((t, r, d))
            else:
                rows.append((t, None, None))
        self.rows, self.heard = rows, len(pts)
        self.unknown = sorted(                     # незареєстровані — без власного зсуву калібрування
            ((m, r, distance_from_loss(r["pl"], self.cfg)) for m, r in snap.items() if m not in known),
            key=lambda u: -u[1]["rssi"])
        if len(pts) < MIN_BEACONS:                 # з 1–2 маяків положення не визначити — і не вгадуємо
            self.pos, self.rms, self.rejected = None, None, set()
            return
        out = self.cfg.get("outliers") or {}
        if out.get("enabled", True):
            sig = self.cfg["signal"]
            res = solve_consistent(pts, max(1.0, sig["pl_10m"] - sig["pl_1m"]), float(out.get("max_db", 12.0)))
        else:
            res = solve(pts)
            res = res and (*res, [])
        if res is None:
            self.pos, self.rms, self.rejected = None, None, set()
            return
        x, y, self.rms, rejected = res
        self.rejected = {used[i] for i in rejected}
        a = float(self.cfg["smoothing"])
        if self.pos is None:
            self.pos = (x, y)
        else:
            self.pos = (self.pos[0] + a * (x - self.pos[0]), self.pos[1] + a * (y - self.pos[1]))
