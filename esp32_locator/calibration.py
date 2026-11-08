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
"""
import math

MIN_N10, MAX_N10 = 15.0, 45.0    # дБ на 10× відстані (показник згасання 1,5…4,5); поза цим — помилка вимірів
MIN_SPREAD = 0.05                # Σ (log10 d − середнє)²: менше — відстані надто близькі для нахилу
MIN_PACKETS = 5                  # пакетів у вимірі, менше — вимір не зараховується
MIN_DISTANCE, MAX_DISTANCE = 0.2, 2000.0


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
