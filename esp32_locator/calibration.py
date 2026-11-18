"""Калібрування моделі сигналу за вимірами на відомих відстанях.

Модель: втрати (дБ) = pl_1m + зсув маяка + n10 · log10(d). n10 = pl_10m − pl_1m — на скільки дБ ростуть
втрати при 10-кратному збільшенні відстані; залежить від місцевості, тож спільний для всіх маяків.
Зсув маяка (pl_offset у маяка в config.json) — його власна різниця (антена, кріплення, потужність);
середнє зсувів виміряних маяків — 0.

Вимір — медіана втрат від одного маяка за кілька секунд, коли приймач стоїть на відомій відстані d.
  • Є маяк, виміряний на різних відстанях (наприклад, 1 і 10 м) → n10 рахується з таких маяків,
    pl_1m і зсуви — з усіх вимірів.
  • Усі маяки виміряно на одній відстані → n10 лишається попереднім, рахуються pl_1m і зсуви.
  • Кожен маяк на своїй, але єдиній відстані → n10 зі всіх разом, зсуви не рахуються (їх не відділити
    від відстані).

Калібрування за записами (walk_samples, fit_walk): приймач носили полем, а його положення відоме в кожен
момент (GPS телефона чи --truth), тож кожен пакет — це вимір «відстань — втрати» на робочих відстанях і
висоті антен. Модель одна на всі маяки, без зсувів: у полі вони не відтворюються від запису до запису.
"""
import math
import statistics

from .protocol import parse_line

MIN_N10, MAX_N10 = 15.0, 45.0    # дБ на 10× відстані (показник згасання 1,5…4,5); поза цим — помилка вимірів
MIN_SPREAD = 0.05                # Σ (log10 d − середнє)²: менше — відстані надто близькі для нахилу
MIN_PACKETS = 5                  # пакетів у вимірі, менше — вимір не зараховується
MIN_DISTANCE, MAX_DISTANCE = 0.2, 2000.0

# калібрування за записами
MIN_WALK_PACKETS = 50            # пакетів з відомою відстанню; менше — модель не рахується
MIN_WALK_SPREAD = 0.15           # стандартне відхилення log10 d; менше — відстані надто схожі для нахилу
MIN_GPS_DISTANCE = 10.0          # м: ближче до маяка похибка GPS телефона (3–5 м) порівнянна з відстанню
GPS_DISTANCE_FACTOR = 3.0        # …і не ближче ніж стільки заявлених точностей GPS
GPS_WARMUP = 3.0                 # с: на початку запису телефон віддає стару точку з хорошою «точністю»
BEACON_BIAS_NOTE = 6.0           # дБ: маяк у середньому відхиляється більше — попередження
BIN_EDGES = [0.2, 1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 70, 100, 150, 200, 300, 500, 700, 1000, 2000]
MIN_BIN_PACKETS = 10             # пакетів у проміжку відстаней, менше — рядок таблиці не показується


def _sums(groups):
    """Σ(x − x̄)² і Σ(x − x̄)(y − ȳ) усередині кожної групи (x̄, ȳ — свої для групи)."""
    sxx = sxy = 0.0
    for pts in groups:
        mx = sum(x for x, _ in pts) / len(pts)
        my = sum(y for _, y in pts) / len(pts)
        sxx += sum((x - mx) ** 2 for x, _ in pts)
        sxy += sum((x - mx) * (y - my) for x, y in pts)
    return sxx, sxy


def fit(samples, n10_prev):
    """samples: [{"mac", "d", "loss"}]. Повертає {"pl_1m", "pl_10m", "offsets": {mac: дБ}, "rms": дБ,
    "slope_measured": bool, "notes": [текст]} або None, якщо вимірів немає."""
    if not samples:
        return None
    groups = {}
    for s in samples:
        groups.setdefault(s["mac"], []).append((math.log10(float(s["d"])), float(s["loss"])))
    allpts = [p for pts in groups.values() for p in pts]
    notes = []

    n10, measured, pooled = float(n10_prev), False, False
    sxx, sxy = _sums(groups.values())
    if sxx < MIN_SPREAD:                          # жоден маяк не виміряно на різних відстанях
        sxx, sxy = _sums([allpts])
        pooled = sxx >= MIN_SPREAD
    if sxx >= MIN_SPREAD:
        cand = sxy / sxx
        if MIN_N10 <= cand <= MAX_N10:
            n10, measured = cand, True
        else:
            pooled = False
            notes.append(f"Втрати ростуть на {cand:.0f} дБ при 10-кратному збільшенні відстані — так не буває "
                         f"(очікується {MIN_N10:.0f}…{MAX_N10:.0f}). Залишено попереднє значення {n10:.1f} дБ: "
                         f"перевірте відстані у вимірах.")
    else:
        notes.append("Виміряйте хоча б один маяк ще й на іншій відстані (наприклад, 1 і 10 м), "
                     "щоб порахувати, як швидко слабне сигнал з відстанню.")

    if pooled:
        p1 = sum(y - n10 * x for x, y in allpts) / len(allpts)
        offsets = {}
        if len(groups) > 1:
            notes.append("Відмінності між маяками не враховано: кожен маяк виміряно лише на одній відстані.")
    else:
        a = {mac: sum(y - n10 * x for x, y in pts) / len(pts) for mac, pts in groups.items()}
        p1 = sum(a.values()) / len(a)
        offsets = {mac: v - p1 for mac, v in a.items()} if len(a) > 1 else {}

    res = [y - p1 - offsets.get(mac, 0.0) - n10 * x for mac, pts in groups.items() for x, y in pts]
    rms = math.sqrt(sum(r * r for r in res) / len(res))
    return {"pl_1m": round(p1, 1), "pl_10m": round(p1 + n10, 1),
            "offsets": {m: round(o, 1) for m, o in offsets.items()},
            "rms": round(rms, 1), "slope_measured": measured, "notes": notes}


def apply(cfg, result):
    """Записує результат fit у cfg: signal і pl_offset маяків (маяки без виміру — без зсуву)."""
    cfg["signal"] = {**cfg["signal"], "pl_1m": result["pl_1m"], "pl_10m": result["pl_10m"]}
    for t in cfg["transmitters"]:
        o = result["offsets"].get(t["mac"])
        if o:
            t["pl_offset"] = o
        else:
            t.pop("pl_offset", None)


# ---------------------------------------------------------------- калібрування за записами
def walk_samples(session, default_txp=14):
    """Виміри «відстань — втрати» із запису: по одному на кожен пакет зареєстрованого маяка, для якого
    відоме положення приймача (session.truth_at). Повертає [{"mac", "name", "d", "loss"}].

    Якщо приймач носили, відстань рахується з GPS телефона, тож пропускаються перші GPS_WARMUP секунд
    і пакети ближче до маяка, ніж max(MIN_GPS_DISTANCE, GPS_DISTANCE_FACTOR · точність GPS): там похибка
    GPS більша за саму відстань і нахил виходив би заниженим."""
    beacons = {t["mac"]: t for t in session.header["transmitters"]}
    d_min, t_min = MIN_DISTANCE, 0.0
    if session.moving:
        acc = session.gps_accuracy() or 0.0
        d_min, t_min = max(MIN_GPS_DISTANCE, GPS_DISTANCE_FACTOR * acc), GPS_WARMUP
    out = []
    for t, line in session.rx:
        got = parse_line(line, default_txp)
        if not got or got[0] not in beacons or t < t_min:
            continue
        truth = session.truth_at(t)
        if truth is None:
            continue
        b = beacons[got[0]]
        d = math.hypot(truth[0] - float(b["x"]), truth[1] - float(b["y"]))
        if d_min <= d <= MAX_DISTANCE:
            out.append({"mac": got[0], "name": b.get("name") or got[0][-4:], "d": d, "loss": got[3] - got[1]})
    return out


def fit_walk(samples, n10_prev):
    """Модель із вимірів walk_samples (можна з кількох записів разом): звичайна регресія втрат за log10 d,
    спільна для всіх маяків. Повертає {"pl_1m", "pl_10m", "rms", "slope_measured", "notes", "n", "d_min",
    "d_max", "beacons": [{"mac", "name", "n", "bias"}], "bins": [{"lo", "hi", "n", "loss", "model"}]}
    або None, якщо вимірів менше MIN_WALK_PACKETS.

    Нахил береться з вимірів, лише якщо відстані достатньо різні (MIN_WALK_SPREAD) і він реальний
    (MIN_N10…MAX_N10); інакше лишається n10_prev, а pl_1m підбирається під нього — тоді модель правильна
    на тих відстанях, де ходили. bias маяка — наскільки його втрати в середньому більші за модель, дБ."""
    n = len(samples)
    if n < MIN_WALK_PACKETS:
        return None
    pts = [(math.log10(s["d"]), float(s["loss"])) for s in samples]
    mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    d_min, d_max = min(s["d"] for s in samples), max(s["d"] for s in samples)
    n10, measured, notes = float(n10_prev), False, []
    if math.sqrt(sxx / n) >= MIN_WALK_SPREAD:
        cand = sum((x - mx) * (y - my) for x, y in pts) / sxx
        if MIN_N10 <= cand <= MAX_N10:
            n10, measured = cand, True
        else:
            notes.append(f"Втрати ростуть на {cand:.0f} дБ при 10-кратному збільшенні відстані — так не буває "
                         f"(очікується {MIN_N10:.0f}…{MAX_N10:.0f}). Залишено попереднє значення {n10:.1f} дБ: "
                         f"перевірте координати маяків у записах.")
    else:
        notes.append(f"Відстані до маяків у записах надто схожі ({d_min:.0f}–{d_max:.0f} м), щоб порахувати, як швидко "
                     f"слабне сигнал: нахил лишився попереднім ({n10:.1f} дБ), підібрано лише pl_1m. Пройдіть так, "
                     f"щоб відстань до маяків змінювалась у 5–10 разів (наприклад, від 20 до 200 м), або додайте "
                     f"запис з іншими відстанями.")
    p1 = my - n10 * mx
    model = lambda d: p1 + n10 * math.log10(d)
    res = [s["loss"] - model(s["d"]) for s in samples]

    groups = {}
    for s, r in zip(samples, res):
        groups.setdefault(s["mac"], (s.get("name") or s["mac"][-4:], []))[1].append(r)
    beacons = [{"mac": mac, "name": name, "n": len(rs), "bias": round(sum(rs) / len(rs), 1)}
               for mac, (name, rs) in sorted(groups.items(), key=lambda g: g[1][0])]
    for b in beacons:
        if abs(b["bias"]) >= BEACON_BIAS_NOTE:
            notes.append(f"Маяк {b['name']}: втрати в середньому на {abs(b['bias']):.0f} дБ "
                         f"{'більші' if b['bias'] > 0 else 'менші'} за модель — перевірте його координати, антену "
                         f"й висоту.")
    bins = []
    for lo, hi in zip(BIN_EDGES, BIN_EDGES[1:]):
        part = [s for s in samples if lo <= s["d"] < hi]
        if len(part) >= MIN_BIN_PACKETS:
            bins.append({"lo": lo, "hi": hi, "n": len(part),
                         "loss": round(statistics.median(s["loss"] for s in part), 1),
                         "model": round(model(statistics.median(s["d"] for s in part)), 1)})
    return {"pl_1m": round(p1, 1), "pl_10m": round(p1 + n10, 1),
            "rms": round(math.sqrt(sum(r * r for r in res) / n), 1), "slope_measured": measured, "notes": notes,
            "n": n, "d_min": d_min, "d_max": d_max, "beacons": beacons, "bins": bins}


def format_walk(result, signal):
    """Текст для термінала: нова модель поруч із поточною (signal з config.json), таблиця за відстанями
    (чи тримається один нахил на всіх відстанях) і відхилення кожного маяка."""
    r, old = result, signal["pl_10m"] - signal["pl_1m"]
    lines = [f"Пакетів з відомою відстанню: {r['n']}, відстані {r['d_min']:.0f}–{r['d_max']:.0f} м.",
             f"Модель: pl_1m = {r['pl_1m']:g} дБ, pl_10m = {r['pl_10m']:g} дБ "
             f"({r['pl_10m'] - r['pl_1m']:.1f} дБ на 10-кратне збільшення відстані).",
             f"Зараз:  pl_1m = {signal['pl_1m']:g} дБ, pl_10m = {signal['pl_10m']:g} дБ ({old:.1f} дБ).",
             f"Розкид втрат одного пакета навколо моделі: {r['rms']:g} дБ.", "",
             f"{'Відстань, м':>12} {'Пакетів':>8} {'Втрати':>8} {'Модель':>8} {'Різниця':>8}"]
    lines += [f"{b['lo']:>5g}–{b['hi']:<6g} {b['n']:>8} {b['loss']:>8.1f} {b['model']:>8.1f} {b['loss'] - b['model']:>+8.1f}"
              for b in r["bins"]]
    lines += ["", f"{'Маяк':>6} {'Пакетів':>8} {'Відхилення, дБ':>15}"]
    lines += [f"{b['name']:>6} {b['n']:>8} {b['bias']:>+15.1f}" for b in r["beacons"]]
    if r["notes"]:
        lines += [""] + [f"Увага: {note}" for note in r["notes"]]
    return "\n".join(lines)


def apply_walk(cfg, result):
    """Записує модель fit_walk у cfg. Зсуви маяків і виміри з веб-сторінки прибираються: модель із записів
    їх замінює, а якби виміри лишились, наступний вимір на сторінці перерахував би signal лише з них.
    Повертає (скільки зсувів прибрано, скільки вимірів прибрано)."""
    cfg["signal"] = {**cfg["signal"], "pl_1m": result["pl_1m"], "pl_10m": result["pl_10m"]}
    offsets = sum(1 for t in cfg["transmitters"] if t.pop("pl_offset", None) is not None)
    samples = len(cfg.get("calibration") or [])
    cfg["calibration"] = []
    return offsets, samples
