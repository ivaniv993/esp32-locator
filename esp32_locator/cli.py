"""Командний рядок: реєстрація маяків, калібрування, запис і відтворення сесій, запуск екрана."""
import argparse
import glob
import os
import signal
import subprocess
import sys
import time

from .config import CONFIG_PATH, MODES, field_spread_warning, load_config, next_name, save_config
from .evaluate import (collect_paths, compare, evaluate_files, format_report, load_baseline,
                       save_baseline)
from .geo import format_latlon, frame_from_config, parse_latlon
from .gui import run_gui
from .mavlink import MavlinkSender, make_publisher, pymavlink_available
from .web import WebState, addresses, ensure_certificate, start_server
from .protocol import norm_mac
from .readings import Readings
from .session import (Recorder, SessionWriter, default_recordings_dir, describe, load_session, make_header,
                      new_session_path, radio_label, session_config)
from .solver import Locator, distance_from_loss
from .sources import ReplaySource, SerialReader

KEEP_RECENT = 200      # скільки останніх пакетів пам’ятати для журналу на екрані


def parse_xy(pair):
    """['2,5', '1'] -> (2.5, 1.0); sys.exit з поясненням, якщо це не числа."""
    try:
        return float(pair[0].replace(",", ".")), float(pair[1].replace(",", "."))
    except ValueError:
        sys.exit("Координати мають бути числами, наприклад 2.5")


def add_beacon(cfg, mac, name=None, **coords):
    """Реєструє маяк або оновлює координати наявного. coords — x, y (тестовий режим) або lat, lon (бойовий)."""
    shown = ", ".join(f"{v}" for v in coords.values())
    tx = next((t for t in cfg["transmitters"] if t["mac"] == mac), None)
    if tx:
        tx.update(coords)
        if name:
            tx["name"] = name
        print(f"Оновлено {mac} -> ({shown})")
    else:
        cfg["transmitters"].append({"name": name or next_name(cfg["transmitters"]), "mac": mac, **coords})
        print(f"Зареєстровано {mac} -> ({shown})")
    if "x" in coords:
        aw, ah = cfg["area"]["width"], cfg["area"]["height"]
        if not (0 <= coords["x"] <= aw and 0 <= coords["y"] <= ah):
            print(f"Увага: маяк поза зоною мапи 0…{aw:g} × 0…{ah:g} м. Збільшіть area, "
                  f"інакше його не буде видно на екрані.")


def parse_truth(cfg, raw):
    """Справжнє положення приймача для --record: метри (тестовий режим) або широта й довгота (бойовий)."""
    if cfg["mode"] != "field":
        return parse_xy(raw)
    try:
        lat, lon = parse_latlon(*raw)
    except ValueError as e:
        sys.exit(f"--truth: {e}. У бойовому режимі вкажіть широту й довготу приймача.")
    return frame_from_config(cfg["geo"]).to_local(lat, lon)


def set_mode(cfg, path, mode):
    """--mode: перемикає режим у config.json. З бойового в тестовий маяки переходять з метрами сітки;
    у бойовий — лише якщо маяків немає або в усіх уже є широта й довгота."""
    if cfg["mode"] == mode:
        print(f"Режим уже {MODES[mode]}.")
        return
    if mode == "field":
        missing = [t.get("name") or t["mac"] for t in cfg["transmitters"] if "lat" not in t or "lon" not in t]
        if missing:
            sys.exit(f"У бойовому режимі маяки задаються широтою й довготою, а в {', '.join(missing)} їх немає. "
                     f"Видаліть ці маяки (--remove ID) і задайте заново на веб-сторінці або через --add-geo.")
    else:
        for t in cfg["transmitters"]:               # x, y уже пораховано з широти й довготи при завантаженні
            t.pop("lat", None)
            t.pop("lon", None)
    cfg["mode"] = mode
    save_config(path, cfg)
    print(f"Режим: {MODES[mode]}. Якщо працює служба, перезапустіть її: sudo systemctl restart esp32-locator")


def start_mavlink(cfg, args):
    """Для --mavlink: запускає MavlinkSender і повертає (sender, функція публікації); без --mavlink — (None, None).
    Положення в ArduPilot передається як широта й довгота, тож потрібен бойовий режим."""
    if not args.mavlink:
        return None, None
    frame = frame_from_config(cfg.get("geo"))
    if frame is None:
        sys.exit('--mavlink працює лише в бойовому режимі: потрібні широта й довгота маяків '
                 '("mode": "field" у config.json і маяки через --add-geo).')
    if not pymavlink_available():
        sys.exit("Не встановлено pymavlink: pip3 install --user --break-system-packages pymavlink  (див. README)")
    baud = args.mavlink_baud or cfg["mavlink"]["baud"]
    sender = MavlinkSender(args.mavlink, baud)
    sender.start()
    print(f"Передача в ArduPilot: {args.mavlink}, {baud} бод (GPS_INPUT; на контролері GPS1_TYPE = 14)")
    return sender, make_publisher(frame, sender, cfg["mavlink"])


def combine(*callbacks):
    """Одна функція on_update(loc) з кількох (передача в ArduPilot, веб-сторінка); None — якщо жодної."""
    callbacks = [c for c in callbacks if c]
    if not callbacks:
        return None

    def call(loc):
        for c in callbacks:
            c(loc)
    return call


def start_web(cfg, args, readings, source, link=None, cfg_path=None, truth=None, recorder=None):
    """Для --web: запускає веб-сервер і повертає його on_update(loc); без --web — None.
    cfg_path=None — сторінка лише показує (запис, відтворення); інакше на ній можна змінювати маяки.
    recorder — session.Recorder: запис кнопкою на сторінці (звичайний режим)."""
    if args.web is None:
        return None
    port = args.web or cfg["web"]["port"]
    tls = None
    if args.web_https:
        try:
            tls = ensure_certificate(os.path.dirname(os.path.abspath(args.config)))
        except (OSError, subprocess.CalledProcessError) as e:
            sys.exit(f"Не вдалося створити сертифікат для HTTPS (потрібен openssl): {e}")
    state = WebState(cfg, cfg_path, readings, source, link, truth, pin=cfg["web"]["pin"], recorder=recorder)
    try:
        start_server(state, port, tls=tls)
    except OSError as e:
        sys.exit(f"Веб-сервер не запустився на порту {port}: {e}")
    scheme = "https" if tls else "http"
    print("Веб-сторінка: " + "  або  ".join(f"{scheme}://{a}:{port}" for a in addresses()))
    if cfg_path is not None and not state.pin:
        print("Увага: web.pin у config.json не задано — маяки може змінити кожен, хто відкрив сторінку.")
    if recorder:
        print(f"Записи з веб-сторінки: {recorder.directory}")
    return state.on_update


def run_headless(cfg, readings, source, on_update=None, link=None, extra=None, every=5.0):
    """Без екрана: положення рахується кожні 0,2 с, як на екрані; стан друкується раз на every секунд."""
    loc = Locator(cfg, readings)
    frame = frame_from_config(cfg.get("geo"))
    last = 0.0
    while True:
        time.sleep(0.2)
        loc.update()
        if on_update:
            on_update(loc)
        now = time.monotonic()
        if now - last < every:
            continue
        last = now
        if loc.pos and loc.heard >= 3:
            where = format_latlon(*frame.to_latlon(*loc.pos)) if frame else f"x={loc.pos[0]:.2f} y={loc.pos[1]:.2f} м"
            where += f" ±{loc.err:.1f} м" if loc.err is not None and loc.err != float("inf") else " (похибка дуже велика)"
            if loc.alt is not None:
                where += "  УВАГА: маяки майже на одній лінії, можливе дзеркальне положення"
        else:
            where = f"положення невідоме (чути маяків: {loc.heard})"
        print("  ".join([f"[{source.status}]", where] + ([link.status] if link else []) + ([extra()] if extra else [])))


def record(cfg, args):
    """Записує сесію: пакети від приймача + справжні положення маяків і приймача (він стоїть на місці)."""
    field = cfg["mode"] == "field"
    if not args.truth:
        sys.exit("Вкажіть, де стоїть приймач: --record --truth " +
                 ("ШИРОТА ДОВГОТА" if field else "X Y  (метри, від кута (0, 0) мапи)"))
    if not cfg["transmitters"]:
        sys.exit("Немає зареєстрованих маяків. Додайте: locator.py " + ("--add-geo ID ШИРОТА ДОВГОТА" if field else "--add ID X Y"))
    truth = parse_truth(cfg, args.truth)
    aw, ah = cfg["area"]["width"], cfg["area"]["height"]
    if not (0 <= truth[0] <= aw and 0 <= truth[1] <= ah):
        if field:
            sys.exit(f"Приймач далі ніж field_margin_m ({cfg['field_margin_m']:g} м) від прямокутника навколо маяків. "
                     f"Перевірте --truth або збільште field_margin_m.")
        sys.exit(f"Приймач ({truth[0]}; {truth[1]}) поза зоною {aw} × {ah} м (параметр area у config.json)")
    truth = round(truth[0], 2), round(truth[1], 2)
    if len(cfg["transmitters"]) < 3:
        print("Увага: маяків менше 3 — за таким записом положення не визначити.")
    # приймач не повідомляє SF, тож записуємо те, що вказано (--sf/--bw або radio у config.json)
    cfg["radio"] = dict(cfg["radio"])
    if args.sf is not None:
        cfg["radio"]["sf"] = args.sf
    if args.bw is not None:
        cfg["radio"]["bw_khz"] = args.bw
    # усе, що може завершити програму з помилкою (MAVLink, порт веб-сервера), — до створення файлу запису,
    # інакше лишився б порожній запис
    link, publish = start_mavlink(cfg, args)
    readings = Readings(cfg["window_sec"], keep_recent=KEEP_RECENT)
    reader = SerialReader(cfg["serial_port"], cfg["baud"], readings, cfg["default_tx_power_dbm"],
                          on_line=lambda line: writer.write_rx(line), exclude=[args.mavlink] if args.mavlink else ())
    web_update = start_web(cfg, args, readings, reader, link, truth=truth)

    directory = args.out or default_recordings_dir(cfg)
    path = new_session_path(directory)
    header = make_header(cfg, truth, args.note or "")
    writer = SessionWriter(path, header)
    reader.start()                                  # після writer: on_line пише в нього
    on_update = combine(publish, web_update)
    radio = radio_label(cfg["radio"])
    src = "--sf" if args.sf is not None else "radio у config.json"
    rt = header["receiver_truth"]
    where = format_latlon(rt["lat"], rt["lon"]) if "lat" in rt else f"({truth[0]}; {truth[1]}) м"
    print(f"Запис у {path}\nРежим: {MODES[cfg['mode']]}. Приймач: {where}, маяків: {len(cfg['transmitters'])}.")
    print(f"Радіо: {radio} (з {src} — якщо маяки прошито інакше, зупиніть і вкажіть --sf)")
    print("Відстані до маяків: " + ", ".join(f"{t['name'] or t['mac'][-4:]} {t['distance']:.1f} м"
                                              for t in header["transmitters"]))
    print("Ctrl+C або Q — зупинити.")
    try:
        if args.headless:
            run_headless(cfg, readings, reader, on_update, link,
                         extra=lambda: f"пакетів: {writer.count}  ({writer.duration:.0f} с)")
        else:
            run_gui(cfg, None, readings, reader, truth=truth,
                    banner=f"● Запис: {os.path.basename(path)}  ·  {radio}", on_update=on_update, link=link)
    except KeyboardInterrupt:
        pass
    finally:
        writer.close()
    if writer.count == 0:
        os.remove(path)
        print("Пакетів не отримано — порожній файл видалено.")
    else:
        print(f"Збережено {path}: {writer.count} пакетів за {writer.duration:.0f} с.")


def evaluate(cfg, args):
    """Прогін алгоритму по записах без екрана і приймача: похибки відносно справжнього положення.
    Код виходу 1, якщо результат гірший за --baseline."""
    paths = collect_paths(args.evaluate)
    if not paths:
        sys.exit("Записів не знайдено.")
    try:
        results = evaluate_files(paths, cfg)
        baseline = load_baseline(args.baseline) if args.baseline else None
    except (OSError, ValueError) as e:
        sys.exit(f"Помилка: {e}")
    print(format_report(results, cfg))
    verdict = None
    if baseline:
        text, verdict = compare(results, cfg, baseline)
        print(f"\nПорівняння з {args.baseline}:\n{text}")
    if args.save:
        save_baseline(args.save, results, cfg)
        print(f"\nРезультати збережено: {args.save}")
    if verdict == "гірше":
        sys.exit(1)


def replay(cfg, args):
    """Демо-режим: екран працює на записаній сесії замість приймача."""
    try:
        session = load_session(args.replay)
    except (OSError, ValueError) as e:
        sys.exit(f"Не вдалося прочитати запис: {e}")
    rcfg = session_config(cfg, session)
    try:
        frame_from_config(rcfg.get("geo"))
    except ValueError as e:
        print(f"Увага: прив’язку geo у записі пошкоджено ({e}) — показую без широти й довготи.")
        rcfg["geo"] = None
    readings = Readings(rcfg["window_sec"], keep_recent=KEEP_RECENT)
    link, publish = start_mavlink(rcfg, args)       # запис можна «прогнати» через контролер на стенді
    source = ReplaySource(session, readings, rcfg["default_tx_power_dbm"], loop=args.loop)
    truth = session.truth
    if session.moving:                              # приймач носили: справжнє положення рухається за GPS
        truth = (lambda: session.truth_at(source.elapsed)) if session.track else None
        if not session.track:
            print("У записі немає GPS телефона — справжнє положення не показується.")
    publish = combine(publish, start_web(rcfg, args, readings, source, link, truth=truth))
    source.start()
    note = session.header.get("note")
    banner = (f"▶ {os.path.basename(args.replay)}" + (f"  ·  {radio_label(session.radio)}" if session.radio else "")
              + (f" — {note}" if note else ""))
    try:
        if args.headless:
            run_headless(rcfg, readings, source, publish, link)
        else:
            run_gui(rcfg, None, readings, source, truth=truth, banner=banner, on_update=publish, link=link)
    except KeyboardInterrupt:
        pass


def info(cfg, args):
    """Опис записів: умови (SF, відстані до маяків) і скільки пакетів прийнято від кожного маяка.
    Зіпсований файл не зупиняє решту, але код виходу тоді 1."""
    paths = collect_paths(args.info)
    if not paths:
        sys.exit("Записів не знайдено.")
    failed = False
    for i, p in enumerate(paths):
        try:
            print(("\n" if i else "") + describe(load_session(p), cfg["default_tx_power_dbm"]))
        except (OSError, ValueError) as e:
            print(("\n" if i else "") + f"{p}\n  Помилка: {e}")
            failed = True
    if failed:
        sys.exit(1)


def calibrate(cfg):
    readings = Readings(5.0)
    reader = SerialReader(cfg["serial_port"], cfg["baud"], readings, cfg["default_tx_power_dbm"])
    reader.start()
    names = {t["mac"]: t.get("name", "") for t in cfg["transmitters"]}
    offsets = {t["mac"]: t.get("pl_offset", 0.0) for t in cfg["transmitters"]}
    print("Поставте приймач на 1 м від маяка і запишіть «втрати» як pl_1m,")
    print("потім на 10 м — як pl_10m (у config.json, розділ signal). Ctrl+C — вихід.")
    print("Зручніше — на веб-сторінці (--web), розділ «Калібрування»: там модель рахується сама.\n")
    try:
        while True:
            time.sleep(1)
            snap = readings.snapshot()
            print(f"[{reader.status}]")
            for mac, r in sorted(snap.items()):
                snr = f"{r['snr']:5.1f}" if r["snr"] is not None else "  —  "
                print(f"  {names.get(mac, '?'):>3}  {mac}  RSSI {r['rssi']:6.1f} дБм  SNR {snr}  "
                      f"потужність {r['txp']:.0f} дБм  втрати {r['pl']:5.1f} дБ  "
                      f"-> {distance_from_loss(r['pl'], cfg, offsets.get(mac, 0.0)):5.1f} м  (пакетів: {r['n']})")
            print()
    except KeyboardInterrupt:
        pass


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        sys.exit("Не встановлено pyserial: sudo apt install python3-serial")
    found = False
    for p in sorted(lp.comports(), key=lambda p: p.device):
        if p.device.startswith("/dev/ttyS"):
            continue                      # вбудовані UART показуємо нижче окремо
        print(f"  {p.device:16} {p.description}")
        found = True
    for p in sorted(glob.glob("/dev/ttyS*")):
        print(f"  {p:16} вбудований UART (може бути системною консоллю)")
        found = True
    if not found:
        print("Портів не знайдено.")


def main():
    ap = argparse.ArgumentParser(description="Позиціонування приймача за LoRa-маяками")
    ap.add_argument("--config", default=CONFIG_PATH, help="шлях до config.json")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--add", nargs=3, metavar=("ID", "X", "Y"), help="тестовий режим: зареєструвати або оновити маяк, м")
    g.add_argument("--add-geo", nargs=3, metavar=("ID", "LAT", "LON"),
                   help="бойовий режим: зареєструвати або оновити маяк за широтою й довготою")
    g.add_argument("--remove", metavar="ID", help="видалити маяк")
    g.add_argument("--list", action="store_true", help="показати маяки")
    g.add_argument("--calibrate", action="store_true", help="режим калібрування")
    g.add_argument("--record", action="store_true", help="записати сесію (потрібен --truth X Y)")
    g.add_argument("--replay", metavar="FILE", help="відтворити записану сесію замість приймача")
    g.add_argument("--evaluate", nargs="+", metavar="FILE", help="оцінити алгоритм на записах (файли або папка)")
    ap.add_argument("--baseline", metavar="JSON", help="--evaluate: порівняти з раніше збереженим результатом")
    ap.add_argument("--save", metavar="JSON", help="--evaluate: зберегти результат для наступних порівнянь")
    ap.add_argument("--truth", nargs=2, metavar=("X", "Y"),
                    help="де стоїть приймач під час --record: метри (тестовий режим) або широта й довгота (бойовий)")
    ap.add_argument("--note", help="коментар до запису (--record)")
    ap.add_argument("--sf", type=int, choices=range(5, 13), metavar="5-12",
                    help="--record: spreading factor, з яким прошиті маяки (інакше radio.sf з config.json)")
    ap.add_argument("--bw", type=float, metavar="кГц", help="--record: ширина каналу LoRa (інакше radio.bw_khz)")
    g.add_argument("--info", nargs="+", metavar="FILE", help="опис записів: SF, відстані, пакети від маяків")
    ap.add_argument("--out", metavar="DIR", help="папка для записів (за замовчуванням recordings_dir)")
    ap.add_argument("--headless", action="store_true",
                    help="без екрана (на борту, по ssh): звичайний режим, --record або --replay")
    ap.add_argument("--mavlink", metavar="ПОРТ",
                    help="передавати положення в ArduPilot як GPS (GPS_INPUT), наприклад /dev/serial/by-id/…; "
                         "лише бойовий режим")
    ap.add_argument("--mavlink-baud", type=int, metavar="БОД", help="швидкість порту контролера (інакше mavlink.baud)")
    ap.add_argument("--web", type=int, nargs="?", const=0, metavar="ПОРТ",
                    help="веб-сторінка з мапою для телефона (порт за замовчуванням — web.port, 8080)")
    ap.add_argument("--web-https", action="store_true",
                    help="--web через HTTPS із власним сертифікатом: потрібно для кнопки «Маяк тут» (GPS телефона)")
    ap.add_argument("--loop", action="store_true", help="--replay по колу")
    ap.add_argument("--name", help="назва маяка для --add / --add-geo")
    g.add_argument("--ports", action="store_true", help="показати послідовні порти цієї плати")
    g.add_argument("--mode", choices=sorted(MODES), help="перемкнути режим: local — тестовий (метри), "
                                                         "field — бойовий (широта й довгота)")
    ap.add_argument("--port", help="порт приймача, наприклад /dev/ttyUSB0 або /dev/ttyS5")
    args = ap.parse_args()
    # systemctl stop / restart надсилає SIGTERM: завершуємось як після Ctrl+C, щоб запис закрився як слід
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        cfg = load_config(args.config)
    except ValueError as e:
        sys.exit(f"Помилка в {args.config}: {e}")
    if args.port:
        cfg["serial_port"] = args.port
    field = cfg["mode"] == "field"
    other_command = args.add or args.add_geo or args.remove or args.list or args.ports or args.calibrate \
        or args.info or args.evaluate or args.mode
    if (args.web is not None or args.web_https) and other_command:
        sys.exit("--web працює зі звичайним режимом, --record або --replay")
    if args.web_https and args.web is None:
        args.web = 0                                 # --web-https сам по собі означає й --web
    if args.mavlink:
        if other_command:
            sys.exit("--mavlink працює з екраном мапи, --record або --replay")
        if cfg["serial_port"] != "auto" and os.path.realpath(cfg["serial_port"]) == os.path.realpath(args.mavlink) \
                and not args.replay:
            sys.exit(f"Приймач і контролер вказано на одному порту ({args.mavlink}). У варіанті з USB-UART адаптером "
                     f'приймач зазвичай на "/dev/serial0", а контролер — на адаптері (/dev/serial/by-id/…).')

    if args.add or args.add_geo:
        mac = norm_mac((args.add or args.add_geo)[0])
        if not mac:
            sys.exit("Неправильний ID. Приклад: A1B2C3D4E5F6")
        if args.add and field:
            sys.exit("У бойовому режимі маяки задаються широтою й довготою: --add-geo ID ШИРОТА ДОВГОТА")
        if args.add_geo and not field:
            sys.exit('Широта й довгота — для бойового режиму: вкажіть "mode": "field" у config.json. '
                     "У тестовому режимі: --add ID X Y (метри)")
        if args.add:
            x, y = parse_xy(args.add[1:])
            add_beacon(cfg, mac, args.name, x=x, y=y)
        else:
            try:
                lat, lon = parse_latlon(*args.add_geo[1:])
            except ValueError as e:
                sys.exit(f"Помилка: {e}")
            add_beacon(cfg, mac, args.name, lat=lat, lon=lon)
        save_config(args.config, cfg)
        if field:
            warning = field_spread_warning(load_config(args.config))
            if warning:
                print(warning)
    elif args.remove:
        mac = norm_mac(args.remove)
        before = len(cfg["transmitters"])
        cfg["transmitters"] = [t for t in cfg["transmitters"] if t["mac"] != mac]
        save_config(args.config, cfg)
        print("Видалено" if len(cfg["transmitters"]) < before else "Такого ID немає в списку")
    elif args.list:
        print(f"Режим: {MODES[cfg['mode']]}")
        for t in cfg["transmitters"]:
            if field:
                print(f"{t.get('name', ''):>3}  {t['mac']}  {format_latlon(t['lat'], t['lon'])}"
                      f"  (на мапі x={t['x']:g}, y={t['y']:g} м)")
            else:
                print(f"{t.get('name', ''):>3}  {t['mac']}  x={t['x']}  y={t['y']}")
        if not cfg["transmitters"]:
            print("Список порожній. Додайте: locator.py " + ("--add-geo ID ШИРОТА ДОВГОТА" if field else "--add ID X Y"))
        elif field and field_spread_warning(cfg):
            print(field_spread_warning(cfg))
    elif args.ports:
        list_ports()
    elif args.calibrate:
        calibrate(cfg)
    elif args.record:
        record(cfg, args)
    elif args.replay:
        replay(cfg, args)
    elif args.info:
        info(cfg, args)
    elif args.evaluate:
        evaluate(cfg, args)
    elif args.mode:
        set_mode(cfg, args.config, args.mode)
    else:
        readings = Readings(cfg["window_sec"], keep_recent=KEEP_RECENT)
        # з --web запис вмикають кнопкою на сторінці; поки запису немає, write_rx нічого не робить
        recorder = Recorder(cfg, args.out or default_recordings_dir(cfg)) if args.web is not None else None
        reader = SerialReader(cfg["serial_port"], cfg["baud"], readings, cfg["default_tx_power_dbm"],
                              on_line=recorder.write_rx if recorder else None,
                              exclude=[args.mavlink] if args.mavlink else ())
        reader.start()
        link, publish = start_mavlink(cfg, args)
        publish = combine(publish, start_web(cfg, args, readings, reader, link, cfg_path=args.config, recorder=recorder),
                          recorder.on_update if recorder else None)
        try:
            if args.headless:
                run_headless(cfg, readings, reader, publish, link)
            else:
                run_gui(cfg, args.config, readings, reader, on_update=publish, link=link)
        except KeyboardInterrupt:
            pass
        finally:
            if recorder and recorder.active:
                res = recorder.stop()
                if res and res["path"]:
                    print(f"Запис зупинено: {res['path']}")
