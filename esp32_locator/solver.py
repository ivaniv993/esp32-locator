"""Алгоритм визначення положення: втрати сигналу → відстані → трилатерація (з відкиданням
неузгоджених маяків) → перевірка геометрії й оцінка похибки → згладжування."""
import math


def distance_from_loss(pl, cfg, offset=0.0):
    """Модель log-distance: втрати ростуть на (pl_10m − pl_1m) дБ при кожному 10-кратному збільшенні відстані.
    offset — власний зсув маяка з калібрування (pl_offset), дБ."""
    p1, p10 = cfg["signal"]["pl_1m"], cfg["signal"]["pl_10m"]
    return 10 ** ((pl - offset - p1) / max(1.0, p10 - p1))


def solve(pts, start=None):
    """pts: [(x, y, d)]. Зважений метод найменших квадратів (Левенберг–Марквардт).
    start — звідки почати пошук (за замовчуванням — зважений центр маяків).
    Повертає (x, y, rms) — rms показує, наскільки виміри узгоджуються між собою."""
    if not pts:
        return None

    def w(d):
        return 1.0 / max(d, 0.3) ** 2

    if start is None:
        sw = sum(w(d) for _, _, d in pts)
        x = sum(w(d) * px for px, _, d in pts) / sw
        y = sum(w(d) * py for _, py, d in pts) / sw
    else:
        x, y = float(start[0]), float(start[1])

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
    return x, y, rms_m(x, y, pts)


def rms_m(x, y, pts):
    """Середньоквадратична різниця між відстанями від (x, y) до маяків і виміряними, м."""
    return math.sqrt(sum((math.hypot(x - px, y - py) - d) ** 2 for px, py, d in pts) / len(pts))


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


# ---------------------------------------------------------------- геометрія й похибка
AMBIGUITY_CHI2 = 4.0     # другий розв’язок пояснює виміри не гірше ніж на ~2σ — дані їх не розрізняють
MIN_ALT_DISTANCE = 1.0   # м: ближчий «другий» розв’язок — та сама точка, а не дзеркальна


def error_estimate(x, y, pts, slope, sigma_db):
    """Оцінка похибки положення (м) і HDOP для маяків pts у точці (x, y).

    Відстань до маяка помиляється пропорційно самій відстані: розкид втрат sigma_db дБ дає
    σ_d = d · ln(10) / slope · sigma_db. Кожен маяк задає похибку вздовж напрямку на себе; разом вони
    дають коваріацію положення (HᵀWH)⁻¹, де рядки H — одиничні вектори від маяків, W = 1/σ_d².
    Оцінка — корінь зі сліду коваріації (≈ радіус, у який положення потрапляє в 2 випадках з 3).
    HDOP — те саме без ваг: наскільки розташування маяків підсилює похибки відстаней
    (~1 — маяки довкола приймача; великий — маяки майже на одній лінії з ним чи купкою вдалині).
    Повертає (оцінка, hdop); math.inf — положення в цьому напрямку маяки не визначають зовсім."""
    k = math.log(10) / slope * sigma_db
    a = b = c = fa = fb = fc = 0.0
    for px, py, _ in pts:
        r = max(math.hypot(x - px, y - py), 0.3)
        ux, uy = (x - px) / r, (y - py) / r
        w = 1.0 / (k * r) ** 2
        a += ux * ux; b += ux * uy; c += uy * uy
        fa += w * ux * ux; fb += w * ux * uy; fc += w * uy * uy
    det, fdet = a * c - b * b, fa * fc - fb * fb
    hdop = math.sqrt((a + c) / det) if det > 1e-9 * (a + c) ** 2 else math.inf
    err = math.sqrt((fa + fc) / fdet) if fdet > 1e-9 * (fa + fc) ** 2 else math.inf
    return err, hdop


def chi2(x, y, pts, slope, sigma_db):
    """Сума квадратів розбіжностей маяків з положенням (x, y), у одиницях розкиду sigma_db."""
    return sum((mismatch_db(x, y, p, slope) / sigma_db) ** 2 for p in pts)


def _starts(x, y, pts):
    """Звідки ще шукати розв’язок: дзеркальне відображення (x, y) відносно осі, уздовж якої витягнуті маяки,
    і точки по обидва боки від осі на типовій відстані до маяків. Якщо маяки майже на одній лінії,
    точка й її дзеркальне відображення дають майже однакові відстані до всіх маяків."""
    n = len(pts)
    mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    syy = sum((p[1] - my) ** 2 for p in pts)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    angle = 0.5 * math.atan2(2 * sxy, sxx - syy)          # напрям осі (головна компонента)
    nx, ny = -math.sin(angle), math.cos(angle)            # перпендикуляр до неї
    off = (x - mx) * nx + (y - my) * ny                   # відстань від осі зі знаком
    fx, fy = x - off * nx, y - off * ny                   # основа перпендикуляра на осі
    s = max(sum(p[2] for p in pts) / n, 1.0)
    return [(x - 2 * off * nx, y - 2 * off * ny), (fx + s * nx, fy + s * ny), (fx - s * nx, fy - s * ny)]


def check_geometry(x, y, pts, slope, sigma_db, prefer=None):
    """Шукає другий розв’язок, який пояснює виміри так само добре (дзеркальне положення).

    Розв’язок шукається ще з кількох початкових точок (_starts). Береться той, що найкраще пояснює виміри
    (найменший chi2). Якщо серед знайдених є інший, віддалений більше ніж на max(MIN_ALT_DISTANCE,
    2 оцінки похибки), і він гірший менше ніж на AMBIGUITY_CHI2, — дані не дають вибрати між ними:
    положення неоднозначне. Тоді береться той із двох, що ближчий до prefer (попереднього положення),
    щоб маркер не перестрибував між ними.
    Повертає (x, y, другий розв’язок або None)."""
    cands = [(x, y)] + [solve(pts, start=s)[:2] for s in _starts(x, y, pts)]
    scored = sorted(((chi2(cx, cy, pts, slope, sigma_db), (cx, cy)) for cx, cy in cands), key=lambda c: c[0])
    best_c, best = scored[0]
    err = error_estimate(best[0], best[1], pts, slope, sigma_db)[0]
    far = max(MIN_ALT_DISTANCE, 2 * err) if math.isfinite(err) else MIN_ALT_DISTANCE
    alt = next((p for c, p in scored[1:]
                if c - best_c < AMBIGUITY_CHI2 and math.hypot(p[0] - best[0], p[1] - best[1]) > far), None)
    if alt and prefer and math.hypot(alt[0] - prefer[0], alt[1] - prefer[1]) < \
            math.hypot(best[0] - prefer[0], best[1] - prefer[1]):
        best, alt = alt, best
    return best[0], best[1], alt


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
        self.err = None      # оцінка похибки положення, м (math.inf — маяки положення не визначають)
        self.hdop = None     # наскільки розташування маяків підсилює похибки відстаней (~1 — добре)
        self.alt = None      # другий розв’язок (x, y), що пояснює виміри так само добре, — або None
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
            self._clear()
            return
        out = self.cfg.get("outliers") or {}
        if out.get("enabled", True):
            sig = self.cfg["signal"]
            res = solve_consistent(pts, max(1.0, sig["pl_10m"] - sig["pl_1m"]), float(out.get("max_db", 12.0)))
        else:
            res = solve(pts)
            res = res and (*res, [])
        if res is None:
            self._clear()
            return
        x, y, _, rejected = res
        self.rejected = {used[i] for i in rejected}
        kept = [p for i, p in enumerate(pts) if i not in rejected]
        sig, acc = self.cfg["signal"], self._accuracy()
        slope, sigma = max(1.0, sig["pl_10m"] - sig["pl_1m"]), acc["sigma_db"]
        x, y, self.alt = check_geometry(x, y, kept, slope, sigma, prefer=self.pos)
        self.rms = rms_m(x, y, kept)
        est, self.hdop = error_estimate(x, y, kept, slope, sigma)
        self.err = max(est, self.rms)              # виміри між собою не узгоджуються — похибка не менша
        if self.alt is not None:                   # можливо, приймач у другій точці — похибка охоплює й її
            self.err = max(self.err, math.hypot(x - self.alt[0], y - self.alt[1]))
        if self.alt is not None and self.pos is not None and \
                math.hypot(x - self.pos[0], y - self.pos[1]) > 2 * max(self.err, MIN_ALT_DISTANCE):
            self.pos = None                        # перескочили на інший розв’язок — не згладжувати між ними
        a = float(self.cfg["smoothing"])
        if self.pos is None:
            self.pos = (x, y)
        else:
            self.pos = (self.pos[0] + a * (x - self.pos[0]), self.pos[1] + a * (y - self.pos[1]))

    @property
    def warnings(self):
        """Попередження про розташування маяків для екрана й сторінки (порожній список — усе гаразд)."""
        if self.pos is None:
            return []
        if self.alt is not None:
            return ["Маяки майже на одній лінії: приймач може бути й у дзеркальній точці «?» "
                    "по інший бік від них. Поставте маяк збоку від цієї лінії."]
        if self.hdop is not None and self.hdop > self._accuracy()["max_hdop"]:
            return ["Маяки стоять погано відносно приймача (з одного боку чи майже на одній лінії з ним) — "
                    "похибка велика."]
        return []

    def _accuracy(self):
        acc = self.cfg.get("accuracy") or {}
        return {"sigma_db": float(acc.get("sigma_db", 4.0)), "max_hdop": float(acc.get("max_hdop", 3.0))}

    def _clear(self):
        self.pos = self.rms = self.err = self.hdop = self.alt = None
        self.rejected = set()
