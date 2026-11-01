"""Статистика похибок положення (чисті функції, без залежності від решти програми)."""
import math


def percentile(values, p):
    """p-й процентиль з лінійною інтерполяцією; None для порожнього списку."""
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * p / 100
    lo = math.floor(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def summarize(errors, ticks):
    """errors — похибки (м) на кроках, де положення визначено; ticks — усього кроків.
    coverage — частка кроків із визначеним положенням."""
    n = len(errors)
    out = {"ticks": ticks, "fixes": n, "coverage": n / ticks if ticks else 0.0,
           "mean": None, "median": None, "p95": None, "max": None, "rmse": None}
    if n:
        out.update(mean=sum(errors) / n, median=percentile(errors, 50), p95=percentile(errors, 95),
                   max=max(errors), rmse=math.sqrt(sum(e * e for e in errors) / n))
    return out


def spread(points):
    """Розкид оцінок навколо їхнього середнього (м): наскільки маркер «тремтить» на місці."""
    if not points:
        return None
    mx = sum(p[0] for p in points) / len(points)
    my = sum(p[1] for p in points) / len(points)
    return math.sqrt(sum((p[0] - mx) ** 2 + (p[1] - my) ** 2 for p in points) / len(points))


def bias(points, truth):
    """Систематичний зсув: середня оцінка мінус справжнє положення, (dx, dy) у метрах."""
    if not points:
        return None
    return (sum(p[0] for p in points) / len(points) - truth[0],
            sum(p[1] for p in points) / len(points) - truth[1])
