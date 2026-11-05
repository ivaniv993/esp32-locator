"""Екран з мапою (pygame)."""
import math
import time
from collections import deque

from .config import save_config
from .mapedit import beacon_at, remove_beacon, screen_to_world
from .packetlog import PacketFeed, format_row
from .solver import Locator

BG = (14, 21, 34)
PAPER = (22, 32, 50)
GRID = (31, 44, 65)
MAJOR = (46, 62, 88)
INK = (227, 233, 240)
MUTED = (150, 165, 184)
TX = (63, 196, 204)
EST = (255, 111, 156)
WARN = (240, 179, 90)
TRUTH = (124, 222, 134)


def fmt(v, n=2):
    return f"{v:.{n}f}".replace(".", ",")


def mix(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def run_gui(cfg, cfg_path, readings, reader, truth=None, banner=None):
    """readings і reader (потік із полем status) створює і запускає викликач.
    cfg_path=None — режим перегляду: маяки не можна реєструвати з екрана (запис, відтворення).
    truth — справжнє положення приймача (x, y): малюється на мапі разом із похибкою.
    banner — рядок угорі мапи (наприклад, «● Запис»)."""
    import pygame

    loc = Locator(cfg, readings)
    feed = PacketFeed(readings)      # журнал пакетів; порожній, якщо Readings створено без keep_recent
    show_log = True
    editable = cfg_path is not None

    pygame.display.init()   # без звуку: аудіо не потрібне
    pygame.font.init()
    fullscreen = bool(cfg["fullscreen"])

    def open_window():
        if fullscreen:
            return pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        return pygame.display.set_mode((960, 560), pygame.RESIZABLE)

    open_window()
    pygame.display.set_caption("Позиціонування за LoRa-маяками")

    def font(size, bold=False):
        return pygame.font.SysFont("dejavusans,freesans,liberationsans", size, bold=bold)

    f_big, f_mid, f_txt, f_small = font(26, True), font(19, True), font(16), font(13)
    clock = pygame.time.Clock()
    trail = deque(maxlen=80)
    last_calc = 0.0
    rate, rate_t, rate_n = 0.0, time.monotonic(), readings.total
    selected = None          # MAC незареєстрованого передавача, який зараз розміщуємо
    unknown_rects = []       # області рядків списку для натискання
    geo = {}                 # геометрія мапи з попереднього кадру
    msg, msg_t = "", 0.0
    active = None            # вибраний зареєстрований маяк (для переміщення / видалення)
    dragging = False         # маяк зараз тягнуть
    drag_from = None         # де маяк був до перетягування (для Esc)
    hand_cursor = False

    def text(surf, s, f, color, pos, anchor="topleft"):
        img = f.render(s, True, color)
        rect = img.get_rect(**{anchor: pos})
        surf.blit(img, rect)
        return rect

    def dashed_circle(surf, color, c, r, dash=7):
        r = min(r, 4000)
        n = max(16, int(2 * math.pi * r / dash))
        n -= n % 2
        for i in range(0, n, 2):
            a1, a2 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
            pygame.draw.line(surf, color, (c[0] + r * math.cos(a1), c[1] + r * math.sin(a1)),
                             (c[0] + r * math.cos(a2), c[1] + r * math.sin(a2)), 1)

    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                return
            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE and dragging:
                    active["x"], active["y"] = drag_from          # повернути, як було
                    dragging = False
                    last_calc = 0.0
                elif e.key == pygame.K_ESCAPE and selected:
                    selected = None
                elif e.key == pygame.K_ESCAPE and active:
                    active = None
                elif e.key in (pygame.K_ESCAPE, pygame.K_q):
                    return
                elif e.key in (pygame.K_DELETE, pygame.K_BACKSPACE) and active and not dragging:
                    remove_beacon(cfg, active["mac"])
                    save_config(cfg_path, cfg)
                    msg, msg_t = (f"Видалено {active.get('name')}: {active['mac']} — він знову в «Незареєстрованих»",
                                  time.monotonic())
                    active = None
                    last_calc = 0.0
                if e.key == pygame.K_f:
                    fullscreen = not fullscreen
                    open_window()
                if e.key == pygame.K_c:
                    trail.clear()
                if e.key == pygame.K_l:
                    show_log = not show_log
            if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1 and geo and editable:
                hit = next((mac for mac, r in unknown_rects if r.collidepoint(e.pos)), None)
                grab = None if selected else beacon_at(e.pos, cfg["transmitters"], geo)
                if hit:
                    selected = None if selected == hit else hit
                    active = None
                elif grab:
                    active, dragging, drag_from = grab, True, (grab["x"], grab["y"])
                elif not selected and geo["map"].collidepoint(e.pos):
                    active = None                                   # клік по порожньому місцю
                elif selected and geo["map"].collidepoint(e.pos):
                    wx = (e.pos[0] - geo["ox"]) / geo["scale"]
                    wy = (geo["oy"] - e.pos[1]) / geo["scale"]
                    if 0 <= wx <= geo["aw"] and 0 <= wy <= geo["ah"]:
                        wx, wy = round(wx, 1), round(wy, 1)
                        used = {t.get("name") for t in cfg["transmitters"]}
                        name = next((chr(c) for c in range(65, 91) if chr(c) not in used),
                                    str(len(cfg["transmitters"]) + 1))
                        cfg["transmitters"].append({"name": name, "mac": selected, "x": wx, "y": wy})
                        save_config(cfg_path, cfg)
                        msg, msg_t = f"Зареєстровано {name}: {selected}  ({fmt(wx, 1)}; {fmt(wy, 1)} м)", time.monotonic()
                        selected = None
                        last_calc = 0.0
            if e.type == pygame.MOUSEMOTION and geo and editable:
                if dragging:
                    active["x"], active["y"] = screen_to_world(e.pos, geo)
                    last_calc = 0.0
                over = dragging or (not selected and beacon_at(e.pos, cfg["transmitters"], geo) is not None)
                if over != hand_cursor:
                    hand_cursor = over
                    try:
                        pygame.mouse.set_cursor(pygame.SYSTEM_CURSOR_HAND if over else pygame.SYSTEM_CURSOR_ARROW)
                    except (AttributeError, pygame.error):
                        pass
            if e.type == pygame.MOUSEBUTTONUP and e.button == 1 and dragging:
                dragging = False
                if (active["x"], active["y"]) != drag_from:
                    save_config(cfg_path, cfg)
                    trail.clear()
                    msg, msg_t = (f"Переміщено {active.get('name')}: ({fmt(active['x'], 1)}; {fmt(active['y'], 1)} м)",
                                  time.monotonic())

        now = time.monotonic()
        feed.update(cfg["transmitters"])
        if now - last_calc >= 0.2:
            last_calc = now
            loc.update()
            if loc.pos:
                trail.append(loc.pos)
        if now - rate_t >= 1.0:
            rate = (readings.total - rate_n) / (now - rate_t)
            rate_t, rate_n = now, readings.total

        screen = pygame.display.get_surface()
        W, H = screen.get_size()
        screen.fill(BG)

        # --- геометрія мапи
        panel_w = max(260, min(340, int(W * 0.36)))
        if W < 640:                      # вузький екран: панель знизу
            map_rect = pygame.Rect(0, 0, W, int(H * 0.58))
            panel = pygame.Rect(0, map_rect.bottom, W, H - map_rect.bottom)
        else:
            map_rect = pygame.Rect(0, 0, W - panel_w, H)
            panel = pygame.Rect(map_rect.right, 0, panel_w, H)
        log_rect = None
        if show_log and map_rect.h >= 360:     # журнал пакетів — смуга внизу мапи
            log_h = min(190, int(map_rect.h * 0.34))
            log_rect = pygame.Rect(map_rect.x, map_rect.bottom - log_h, map_rect.w, log_h)
            map_rect = pygame.Rect(map_rect.x, map_rect.y, map_rect.w, map_rect.h - log_h)

        aw, ah = float(cfg["area"]["width"]), float(cfg["area"]["height"])
        pad = 34
        scale = min((map_rect.w - 2 * pad) / aw, (map_rect.h - 2 * pad) / ah)
        ox = map_rect.x + (map_rect.w - aw * scale) / 2
        oy = map_rect.y + (map_rect.h + ah * scale) / 2

        def P(x, y):
            return int(ox + x * scale), int(oy - y * scale)

        geo = {"map": map_rect, "ox": ox, "oy": oy, "scale": scale, "aw": aw, "ah": ah}

        x0, y0 = P(0, ah)
        pygame.draw.rect(screen, PAPER, (x0, y0, int(aw * scale), int(ah * scale)))
        step = 1 if scale >= 12 else (5 if scale >= 3 else 10)
        major = 5 if step == 1 else step * 2
        gx = 0.0
        while gx <= aw + 1e-9:
            col = MAJOR if round(gx) % major == 0 else GRID
            pygame.draw.line(screen, col, P(gx, 0), P(gx, ah))
            if round(gx) % major == 0:
                text(screen, str(int(gx)), f_small, MUTED, (P(gx, 0)[0], P(gx, 0)[1] + 5), "midtop")
            gx += step
        gy = 0.0
        while gy <= ah + 1e-9:
            col = MAJOR if round(gy) % major == 0 else GRID
            pygame.draw.line(screen, col, P(0, gy), P(aw, gy))
            if round(gy) % major == 0 and gy > 0:
                text(screen, str(int(gy)), f_small, MUTED, (P(0, gy)[0] - 6, P(0, gy)[1]), "midright")
            gy += step
        text(screen, "м", f_small, MUTED, (P(aw, 0)[0] + 16, P(aw, 0)[1] + 5))

        # --- кола відстаней, слід, передавачі, положення
        for t, r, d in loc.rows:
            if d is not None:
                dashed_circle(screen, mix(PAPER, TX, 0.55), P(t["x"], t["y"]), int(d * scale))
        for i, p in enumerate(trail):
            pygame.draw.circle(screen, mix(PAPER, EST, 0.1 + 0.4 * i / len(trail)), P(*p), 3)
        for t, r, d in loc.rows:
            c = P(t["x"], t["y"])
            if t is active:
                pygame.draw.circle(screen, EST, c, 15, 2)
            pygame.draw.circle(screen, TX if r is not None else mix(PAPER, TX, 0.35), c, 9)
            pygame.draw.circle(screen, PAPER, c, 9, 2)
            label = f"{t.get('name') or t['mac'][-5:]}  " + (f"{r['rssi']:.0f} дБм" if r else "немає")
            right = c[0] > map_rect.right - 120
            text(screen, label, f_txt, INK, (c[0] + (-14 if right else 14), c[1]),
                 "midright" if right else "midleft")
        if loc.pos:   # маркер не виходить за межі мапи, точні числа — на панелі
            c = P(min(aw, max(0.0, loc.pos[0])), min(ah, max(0.0, loc.pos[1])))
            pygame.draw.circle(screen, EST, c, 14, 3)
            pygame.draw.line(screen, EST, (c[0] - 20, c[1]), (c[0] + 20, c[1]), 2)
            pygame.draw.line(screen, EST, (c[0], c[1] - 20), (c[0], c[1] + 20), 2)
        if truth:     # справжнє положення приймача (зі запису або з --truth)
            c = P(*truth)
            pygame.draw.rect(screen, TRUTH, (c[0] - 9, c[1] - 9, 18, 18), 2)
            text(screen, "справжнє", f_small, TRUTH, (c[0], c[1] + 14), "midtop")

        if selected:
            text(screen, f"Клацніть на мапі, де стоїть {selected}   (Esc — скасувати)", f_txt, EST,
                 (map_rect.x + 16, map_rect.y + 6))
        elif dragging:
            text(screen, f"{active.get('name')}: ({fmt(active['x'], 1)}; {fmt(active['y'], 1)} м)   (Esc — повернути)",
                 f_txt, EST, (map_rect.x + 16, map_rect.y + 6))
        elif msg and now - msg_t < 5:
            text(screen, msg, f_txt, TX, (map_rect.x + 16, map_rect.y + 6))
        elif active:
            text(screen, f"{active.get('name')} вибрано: перетягніть, щоб перемістити   Delete — видалити   Esc — зняти",
                 f_small, EST, (map_rect.x + 16, map_rect.y + 8))
        elif banner:
            text(screen, banner, f_txt, TRUTH if truth else TX, (map_rect.x + 16, map_rect.y + 6))

        # --- журнал пакетів: за порядком надходження, нові внизу
        if log_rect:
            pygame.draw.rect(screen, PAPER, log_rect)
            pygame.draw.line(screen, MAJOR, log_rect.topleft, log_rect.topright)
            screen.set_clip(log_rect)
            text(screen, f"Пакети від приймача (нові внизу)   усього: {readings.total}", f_small, MUTED,
                 (log_rect.x + 14, log_rect.y + 6))
            rows = list(feed.rows)[-max(1, (log_rect.h - 32) // 16):]
            ly = log_rect.y + 26
            if not rows:
                text(screen, "Пакетів ще немає. Перевірте приймач і маяки.", f_small, WARN, (log_rect.x + 14, ly))
            for i, r in enumerate(rows):
                color = WARN if r.name is None else (INK if i >= len(rows) - 3 else MUTED)
                text(screen, format_row(r), f_small, color, (log_rect.x + 14, ly))
                ly += 16
            screen.set_clip(None)

        # --- панель
        pygame.draw.rect(screen, PAPER, panel)
        x, y = panel.x + 18, panel.y + 16
        text(screen, "Положення приймача", f_txt, MUTED, (x, y)); y += 24
        if loc.pos:
            text(screen, f"x = {fmt(loc.pos[0])} м", f_big, EST, (x, y)); y += 32
            text(screen, f"y = {fmt(loc.pos[1])} м", f_big, EST, (x, y)); y += 34
            text(screen, f"Узгодженість вимірів: ±{fmt(loc.rms, 1)} м", f_small, MUTED, (x, y)); y += 22
            if truth:
                err = math.hypot(loc.pos[0] - truth[0], loc.pos[1] - truth[1])
                text(screen, f"Похибка: {fmt(err, 2)} м  (справжнє {fmt(truth[0], 1)}; {fmt(truth[1], 1)})",
                     f_small, TRUTH, (x, y)); y += 22
        else:
            text(screen, "невідоме", f_big, MUTED, (x, y)); y += 40
        if loc.heard < 3:
            text(screen, f"Чути {loc.heard} з {len(cfg['transmitters'])} маяків", f_small, WARN, (x, y))
            y += 18
            text(screen, "Для точного положення потрібно 3.", f_small, WARN, (x, y))
            y += 22
        y += 10
        text(screen, "Маяки", f_mid, INK, (x, y)); y += 28
        for t, r, d in loc.rows:
            name = t.get("name") or "—"
            text(screen, f"{name}  {t['mac']}", f_small, INK, (x, y)); y += 17
            if r is None:
                text(screen, "немає сигналу", f_small, WARN, (x, y))
            else:
                snr = f"   SNR {fmt(r['snr'], 0)}" if r["snr"] is not None else ""
                text(screen, f"{r['rssi']:.0f} дБм{snr}   {fmt(d, 1)} м   пак.: {r['n']}", f_small, MUTED, (x, y))
            y += 24
        if editable and cfg["transmitters"]:
            text(screen, "Маяк можна перетягнути на мапі", f_small, MUTED, (x, y - 4)); y += 16
        unknown_rects = []
        if loc.unknown:
            y += 4
            text(screen, f"Незареєстровані ({len(loc.unknown)}):", f_small, WARN, (x, y)); y += 20
            limit = max(1, (panel.bottom - 100 - y) // 36)
            for mac, r, d in loc.unknown[:limit]:
                row = pygame.Rect(panel.x + 10, y - 3, panel.w - 20, 34)
                if mac == selected:
                    pygame.draw.rect(screen, mix(PAPER, EST, 0.3), row, border_radius=5)
                elif row.collidepoint(pygame.mouse.get_pos()):
                    pygame.draw.rect(screen, mix(PAPER, INK, 0.08), row, border_radius=5)
                text(screen, mac, f_small, INK, (x, y)); y += 15
                text(screen, f"{r['rssi']:.0f} дБм   ~{fmt(d, 1)} м", f_small, MUTED, (x, y)); y += 21
                unknown_rects.append((mac, row))
            if editable and not selected:
                text(screen, "Торкніться ID, щоб розмістити на мапі", f_small, MUTED, (x, y + 2))
        by = panel.bottom - 16
        text(screen, "L — журнал пакетів   Q — вихід", f_small, MUTED, (x, by), "bottomleft")
        text(screen, "F — повний екран   C — очистити слід", f_small, MUTED, (x, by - 18), "bottomleft")
        text(screen, f"{reader.status}   {rate:.0f} пак./с", f_small, MUTED, (x, by - 38), "bottomleft")

        pygame.display.flip()
        clock.tick(30)
