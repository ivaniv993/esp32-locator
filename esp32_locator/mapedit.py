"""Редагування маяків на мапі: перерахунок координат екрана і пошук маяка під курсором.
Без pygame — щоб логіку можна було перевірити тестами."""
import math


def screen_to_world(pos, geo, step=0.1):
    """Точка екрана -> координати в метрах, обмежені зоною й округлені до step.
    geo — геометрія мапи з gui: ox, oy (екранні координати точки (0; 0)), scale (пікселів на метр), aw, ah."""
    wx = (pos[0] - geo["ox"]) / geo["scale"]
    wy = (geo["oy"] - pos[1]) / geo["scale"]
    wx = min(geo["aw"], max(0.0, wx))
    wy = min(geo["ah"], max(0.0, wy))
    return round(round(wx / step) * step, 3), round(round(wy / step) * step, 3)


def world_to_screen(x, y, geo):
    return geo["ox"] + x * geo["scale"], geo["oy"] - y * geo["scale"]


def beacon_at(pos, transmitters, geo, radius=22):
    """Найближчий маяк у межах radius пікселів від pos (радіус із запасом для пальця) або None."""
    best, best_d = None, radius
    for t in transmitters:
        px, py = world_to_screen(t["x"], t["y"], geo)
        d = math.hypot(pos[0] - px, pos[1] - py)
        if d <= best_d:
            best, best_d = t, d
    return best


def remove_beacon(cfg, mac):
    """Прибирає маяк із cfg; повертає видалений запис або None."""
    for i, t in enumerate(cfg["transmitters"]):
        if t["mac"] == mac:
            return cfg["transmitters"].pop(i)
    return None
