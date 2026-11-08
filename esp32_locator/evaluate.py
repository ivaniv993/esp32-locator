"""Офлайн-оцінка алгоритму: прогін по записаних сесіях і порівняння з попереднім результатом.

Алгоритм крокує по запису на симульованому годиннику з тим самим кроком, що й екран,
і тими самими Readings та Locator, тож результат збігається з роботою на живому приймачі.
Якщо приймач носили (запис з веб-сторінки), справжнє положення на кожному кроці — трек GPS телефона.
"""
import json
import math
import os
from dataclasses import dataclass

from .metrics import bias, spread, summarize
from .protocol import parse_line
from .readings import Readings
from .session import load_session, session_config
from .solver import Locator

STEP = 0.2            # с, як у GUI (положення рахується раз на 0,2 с)
MIN_BEACONS = 3       # менше маяків — положення не визначене
TOLERANCE = 0.02      # м, різниця середньої похибки, меншу за яку вважаємо шумом
COVERAGE_TOLERANCE = 0.01
BASELINE_VERSION = 1
LAGS = [i * 0.5 for i in range(11)]   # с: затримки, з якими порівнюємо визначене положення з GPS (0…5 с)


@dataclass
class Result:
    name: str
    truth: tuple       # де стояв приймач; None — його носили, тоді справжнє положення в truths
    duration: float
    ticks: int         # кроки, на яких відомо справжнє положення
    fixes: list        # [(x, y)] оцінки на кроках, де чути ≥ MIN_BEACONS маяків
    sf: int = None     # SF із запису (None — не вказано)
    truths: list = None    # приймач носили: [(x, y)] за GPS телефона для кожної оцінки
    lag: tuple = None      # приймач носили: (затримка, с; середня похибка, м) — найкращий збіг із GPS
    gps_acc: float = None  # медіанна точність GPS телефона, м

    @property
    def errors(self):
        truths = self.truths if self.truth is None else [self.truth] * len(self.fixes)
        return [math.hypot(x - tx, y - ty) for (x, y), (tx, ty) in zip(self.fixes, truths)]

    def metrics(self):
        m = summarize(self.errors, self.ticks)
        m["duration"] = self.duration
        ref = self.truth
        if ref is None and self.truths:             # середнє (оцінка − GPS) = середня оцінка − середня точка GPS
            ref = (sum(p[0] for p in self.truths) / len(self.truths), sum(p[1] for p in self.truths) / len(self.truths))
        b = bias(self.fixes, ref) if ref else None
        m["bias_x"], m["bias_y"] = b if b else (None, None)
        m["spread"] = spread(self.fixes) if self.truth is not None else None   # у русі «тремтіння» не виміряти
        m["sf"] = self.sf
        return m


def best_lag(session, times, fixes):
    """Приймач носили: з якою затримкою визначене положення найкраще збігається з GPS
    (вікно медіани й згладжування відстають від руху). (затримка, с; середня похибка, м) або None."""
    best = None
    for lag in LAGS:
        errs = []
        for t, (x, y) in zip(times, fixes):
            p = session.truth_at(t - lag)
            if p:
                errs.append(math.hypot(x - p[0], y - p[1]))
        if errs and len(errs) >= len(fixes) / 2:
            mean = sum(errs) / len(errs)
            if best is None or mean < best[1] - 1e-9:
                best = (lag, mean)
    return best


def evaluate_session(session, cfg, step=STEP):
    """Прогін алгоритму по одній сесії. Перші window_sec секунд пропускаються: вікно ще не заповнене.
    Кроки, на яких справжнє положення невідоме (приймач носили, а GPS поруч немає), не рахуються."""
    rcfg = session_config(cfg, session)
    readings = Readings(rcfg["window_sec"])
    locator = Locator(rcfg, readings)
    events = []
    for t, line in session.rx:
        got = parse_line(line, rcfg["default_tx_power_dbm"])
        if got:
            events.append((t, got))

    fixes, truths, times, ticks, i, k = [], [], [], 0, 0, 0
    while k * step <= session.duration + 1e-9:
        now, k = k * step, k + 1
        while i < len(events) and events[i][0] <= now:
            readings.add(*events[i][1], ts=events[i][0])
            i += 1
        locator.update(now)
        if now < rcfg["window_sec"]:
            continue
        truth = session.truth_at(now)
        if truth is None:
            continue
        ticks += 1
        if locator.heard >= MIN_BEACONS and locator.pos:
            fixes.append(locator.pos)
            truths.append(truth)
            times.append(now)
    name = os.path.basename(session.path)
    if not session.moving:
        return Result(name, session.truth, session.duration, ticks, fixes, session.sf)
    return Result(name, None, session.duration, ticks, fixes, session.sf, truths=truths,
                  lag=best_lag(session, times, fixes), gps_acc=session.gps_accuracy())


def collect_paths(args):
    """Файли як є, а для папки — усі *.jsonl у ній."""
    out = []
    for a in args:
        if os.path.isdir(a):
            out += sorted(os.path.join(a, f) for f in os.listdir(a) if f.endswith(".jsonl"))
        else:
            out.append(a)
    return out


def evaluate_files(paths, cfg):
    """[Result] для кожного файлу; помилки читання піднімаються як ValueError/OSError."""
    return [evaluate_session(load_session(p), cfg) for p in paths]


def total_metrics(results):
    """Загальні метрики по всіх сесіях разом (довші записи важать більше)."""
    errors = [e for r in results for e in r.errors]
    return summarize(errors, sum(r.ticks for r in results))


def params(cfg):
    """Параметри алгоритму, від яких залежить результат."""
    return {"pl_1m": cfg["signal"]["pl_1m"], "pl_10m": cfg["signal"]["pl_10m"],
            "smoothing": cfg["smoothing"], "window_sec": cfg["window_sec"], "step": STEP}


def to_baseline(results, cfg):
    return {"version": BASELINE_VERSION, "params": params(cfg),
            "files": {r.name: r.metrics() for r in results}}


def save_baseline(path, results, cfg):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_baseline(results, cfg), f, ensure_ascii=False, indent=2)


def load_baseline(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("version") != BASELINE_VERSION:
        raise ValueError(f"{path}: непідтримувана версія базової лінії")
    return data


# ---------------------------------------------------------------- звіт
def _f(v, n=2, signed=False):
    if v is None:
        return "—"
    return f"{v:+.{n}f}" if signed else f"{v:.{n}f}"


def _row(name, m, extra=True):
    bias_s = f"({_f(m['bias_x'], 1, True)};{_f(m['bias_y'], 1, True)})" if extra and m.get("bias_x") is not None else ""
    spread_s = _f(m.get("spread")) if extra else ""
    sf_s = (str(m["sf"]) if m.get("sf") is not None else "—") if extra else ""
    return (f"{name[:28]:28} {sf_s:>3} {m.get('duration', 0):5.0f} с {m['coverage'] * 100:6.0f}% "
            f"{_f(m['mean']):>6} {_f(m['median']):>6} {_f(m['p95']):>6} {_f(m['max']):>6} {_f(m['rmse']):>6} "
            f"{bias_s:>14} {spread_s:>6}")


HEADER = f"{'Запис':28} {'SF':>3} {'Трив.':>7} {'Покр.':>7} {'Сер.':>6} {'Мед.':>6} {'p95':>6} {'Макс':>6} {'СКВ':>6} {'Зсув x;y':>14} {'Розкид':>6}"


def format_report(results, cfg):
    """Таблиця похибок у метрах: сер./мед./p95/макс./СКВ — відстань від справжнього положення,
    покриття — частка кроків із ≥3 маяками, зсув — систематичне відхилення, розкид — «тремтіння» оцінки."""
    p = params(cfg)
    lines = [f"Параметри: pl_1m={p['pl_1m']} pl_10m={p['pl_10m']} smoothing={p['smoothing']} "
             f"window={p['window_sec']} с, крок {p['step']} с", "", HEADER, "-" * len(HEADER)]
    lines += [_row(r.name, r.metrics()) for r in results]
    if len(results) > 1:
        lines += ["-" * len(HEADER), _row(f"Разом ({len(results)})", {**total_metrics(results), "duration": sum(r.duration for r in results)}, extra=False)]
    if len({r.sf for r in results}) > 1:
        lines += ["", "Увага: записи зроблено з різним SF — різниця в похибці може бути через радіо, а не через алгоритм."]
    moving = [r for r in results if r.truth is None]
    if moving:
        lines += ["", "Приймач носили — похибка відносно GPS телефона (він і сам помиляється на кілька метрів):"]
    for r in moving:
        if not r.ticks:
            lines.append(f"  {r.name}: GPS телефона немає — порівнювати нема з чим.")
            continue
        text = f"  {r.name}: точність GPS ±{_f(r.gps_acc, 0)} м"
        if r.lag and r.lag[0] > 0:
            text += (f"; найменша середня похибка {r.lag[1]:.2f} м — якщо порівнювати з положенням "
                     f"{r.lag[0]:g} с тому (визначене положення відстає від руху: вікно й згладжування)")
        lines.append(text + ".")
    return "\n".join(lines)


def _verdict(cur, base):
    """«краще» / «гірше» / «без змін» за середньою похибкою і покриттям."""
    if cur["mean"] is None and base["mean"] is None:
        return "без змін"
    if cur["mean"] is None:
        return "гірше"           # раніше положення визначалось, тепер ні
    if base["mean"] is None:
        return "краще"
    d_mean, d_cov = cur["mean"] - base["mean"], cur["coverage"] - base["coverage"]
    if d_mean > TOLERANCE or d_cov < -COVERAGE_TOLERANCE:
        return "гірше"
    if d_mean < -TOLERANCE and d_cov >= -COVERAGE_TOLERANCE:
        return "краще"
    return "без змін"


def compare(results, cfg, baseline):
    """Текст порівняння з базовою лінією і загальний вердикт ('краще' / 'гірше' / 'без змін')."""
    lines = []
    if baseline["params"] != params(cfg):
        lines.append("Увага: параметри відрізняються від базової лінії:")
        for k, v in params(cfg).items():
            if baseline["params"].get(k) != v:
                lines.append(f"  {k}: було {baseline['params'].get(k)}, зараз {v}")
        lines.append("")
    lines += [f"{'Запис':28} {'Δ сер., м':>10} {'Δ p95, м':>10} {'Δ покр.':>9}  Вердикт", "-" * 70]
    cur_files = {r.name: r.metrics() for r in results}
    for name, cur in cur_files.items():
        base = baseline["files"].get(name)
        if base is None:
            lines.append(f"{name[:28]:28} {'—':>10} {'—':>10} {'—':>9}  немає в базовій лінії")
            continue
        d = lambda k: None if cur[k] is None or base[k] is None else cur[k] - base[k]
        lines.append(f"{name[:28]:28} {_f(d('mean'), 2, True):>10} {_f(d('p95'), 2, True):>10} "
                     f"{(cur['coverage'] - base['coverage']) * 100:+8.0f}%  {_verdict(cur, base)}")
    for name in baseline["files"]:
        if name not in cur_files:
            lines.append(f"{name[:28]:28} {'—':>10} {'—':>10} {'—':>9}  немає серед поточних")
    common = [r for r in results if r.name in baseline["files"]]
    if not common:
        return "\n".join(lines + ["", "Немає спільних записів для порівняння."]), "без змін"
    # загальний підсумок — лише по записах, які є і в базовій лінії, і серед поточних
    cur_t = total_metrics(common)
    base_t = _pooled_from_files({r.name: baseline["files"][r.name] for r in common})
    verdict = _verdict(cur_t, base_t)
    d_mean = None if cur_t["mean"] is None or base_t["mean"] is None else cur_t["mean"] - base_t["mean"]
    lines += ["-" * 70, f"{'Разом':28} {_f(d_mean, 2, True):>10} {'':>10} "
              f"{(cur_t['coverage'] - base_t['coverage']) * 100:+8.0f}%  {verdict.upper()}"]
    return "\n".join(lines), verdict


def _pooled_from_files(files):
    """Загальні метрики з метрик окремих файлів (середнє зважене кількістю визначень)."""
    ticks = sum(m["ticks"] for m in files.values())
    fixes = sum(m["fixes"] for m in files.values())
    mean = sum(m["mean"] * m["fixes"] for m in files.values() if m["mean"] is not None) / fixes if fixes else None
    return {"ticks": ticks, "fixes": fixes, "coverage": fixes / ticks if ticks else 0.0, "mean": mean}
