"""Командний рядок: реєстрація маяків, калібрування, запис і відтворення сесій, запуск екрана."""
import argparse
import glob
import os
import sys
import time

from .config import CONFIG_PATH, load_config, save_config
from .evaluate import (collect_paths, compare, evaluate_files, format_report, load_baseline,
                       save_baseline)
from .gui import run_gui
from .protocol import norm_mac
from .readings import Readings
from .session import (SessionWriter, default_recordings_dir, describe, load_session, make_header,
                      new_session_path, radio_label, session_config)
from .solver import distance_from_loss
from .sources import ReplaySource, SerialReader

KEEP_RECENT = 200      # скільки останніх пакетів пам’ятати для журналу на екрані


def parse_xy(pair):
    """['2,5', '1'] -> (2.5, 1.0); sys.exit з поясненням, якщо це не числа."""
    try:
        return float(pair[0].replace(",", ".")), float(pair[1].replace(",", "."))
    except ValueError:
        sys.exit("Координати мають бути числами, наприклад 2.5")


def record(cfg, args):
    """Записує сесію: пакети від приймача + справжні положення маяків і приймача (він стоїть на місці)."""
    if not args.truth:
        sys.exit("Вкажіть, де стоїть приймач: --record --truth X Y  (метри, від кута (0, 0) мапи)")
    if not cfg["transmitters"]:
        sys.exit("Немає зареєстрованих маяків. Додайте: locator.py --add ID X Y")
    truth = parse_xy(args.truth)
    aw, ah = cfg["area"]["width"], cfg["area"]["height"]
    if not (0 <= truth[0] <= aw and 0 <= truth[1] <= ah):
        sys.exit(f"Приймач ({truth[0]}; {truth[1]}) поза зоною {aw} × {ah} м (параметр area у config.json)")
    if len(cfg["transmitters"]) < 3:
        print("Увага: маяків менше 3 — за таким записом положення не визначити.")
    # приймач не повідомляє SF, тож записуємо те, що вказано (--sf/--bw або radio у config.json)
    cfg["radio"] = dict(cfg["radio"])
    if args.sf is not None:
        cfg["radio"]["sf"] = args.sf
    if args.bw is not None:
        cfg["radio"]["bw_khz"] = args.bw

    directory = args.out or default_recordings_dir(cfg)
    path = new_session_path(directory)
    header = make_header(cfg, truth, args.note or "")
    writer = SessionWriter(path, header)
    readings = Readings(cfg["window_sec"], keep_recent=KEEP_RECENT)
    reader = SerialReader(cfg["serial_port"], cfg["baud"], readings, cfg["default_tx_power_dbm"],
                          on_line=writer.write_rx)
    reader.start()
    radio = radio_label(cfg["radio"])
    src = "--sf" if args.sf is not None else "radio у config.json"
    print(f"Запис у {path}\nПриймач: ({truth[0]}; {truth[1]}) м, маяків: {len(cfg['transmitters'])}.")
    print(f"Радіо: {radio} (з {src} — якщо маяки прошито інакше, зупиніть і вкажіть --sf)")
    print("Відстані до маяків: " + ", ".join(f"{t['name'] or t['mac'][-4:]} {t['distance']:.1f} м"
                                              for t in header["transmitters"]))
    print("Ctrl+C або Q — зупинити.")
    try:
        if args.headless:
            while True:
                time.sleep(5)
                print(f"[{reader.status}]  пакетів: {writer.count}  ({writer.duration:.0f} с)")
        else:
            run_gui(cfg, None, readings, reader, truth=truth,
                    banner=f"● Запис: {os.path.basename(path)}  ·  {radio}")
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
    readings = Readings(rcfg["window_sec"], keep_recent=KEEP_RECENT)
    source = ReplaySource(session, readings, rcfg["default_tx_power_dbm"], loop=args.loop)
    source.start()
    note = session.header.get("note")
    banner = (f"▶ {os.path.basename(args.replay)}" + (f"  ·  {radio_label(session.radio)}" if session.radio else "")
              + (f" — {note}" if note else ""))
    run_gui(rcfg, None, readings, source, truth=session.truth, banner=banner)


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
    print("Поставте приймач на 1 м від маяка і запишіть «втрати» як pl_1m,")
    print("потім на 10 м — як pl_10m (у config.json, розділ signal). Ctrl+C — вихід.\n")
    try:
        while True:
            time.sleep(1)
            snap = readings.snapshot()
            print(f"[{reader.status}]")
            for mac, r in sorted(snap.items()):
                snr = f"{r['snr']:5.1f}" if r["snr"] is not None else "  —  "
                print(f"  {names.get(mac, '?'):>3}  {mac}  RSSI {r['rssi']:6.1f} дБм  SNR {snr}  "
                      f"потужність {r['txp']:.0f} дБм  втрати {r['pl']:5.1f} дБ  "
                      f"-> {distance_from_loss(r['pl'], cfg):5.1f} м  (пакетів: {r['n']})")
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
    g.add_argument("--add", nargs=3, metavar=("ID", "X", "Y"), help="зареєструвати або оновити маяк")
    g.add_argument("--remove", metavar="ID", help="видалити маяк")
    g.add_argument("--list", action="store_true", help="показати маяки")
    g.add_argument("--calibrate", action="store_true", help="режим калібрування")
    g.add_argument("--record", action="store_true", help="записати сесію (потрібен --truth X Y)")
    g.add_argument("--replay", metavar="FILE", help="відтворити записану сесію замість приймача")
    g.add_argument("--evaluate", nargs="+", metavar="FILE", help="оцінити алгоритм на записах (файли або папка)")
    ap.add_argument("--baseline", metavar="JSON", help="--evaluate: порівняти з раніше збереженим результатом")
    ap.add_argument("--save", metavar="JSON", help="--evaluate: зберегти результат для наступних порівнянь")
    ap.add_argument("--truth", nargs=2, metavar=("X", "Y"), help="де стоїть приймач під час --record, м")
    ap.add_argument("--note", help="коментар до запису (--record)")
    ap.add_argument("--sf", type=int, choices=range(5, 13), metavar="5-12",
                    help="--record: spreading factor, з яким прошиті маяки (інакше radio.sf з config.json)")
    ap.add_argument("--bw", type=float, metavar="кГц", help="--record: ширина каналу LoRa (інакше radio.bw_khz)")
    g.add_argument("--info", nargs="+", metavar="FILE", help="опис записів: SF, відстані, пакети від маяків")
    ap.add_argument("--out", metavar="DIR", help="папка для записів (за замовчуванням recordings_dir)")
    ap.add_argument("--headless", action="store_true", help="--record без екрана (наприклад, по ssh)")
    ap.add_argument("--loop", action="store_true", help="--replay по колу")
    ap.add_argument("--name", help="назва маяка для --add")
    g.add_argument("--ports", action="store_true", help="показати послідовні порти цієї плати")
    ap.add_argument("--port", help="порт приймача, наприклад /dev/ttyUSB0 або /dev/ttyS5")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.port:
        cfg["serial_port"] = args.port

    if args.add:
        mac = norm_mac(args.add[0])
        if not mac:
            sys.exit("Неправильний ID. Приклад: A1B2C3D4E5F6")
        x, y = parse_xy(args.add[1:])
        tx = next((t for t in cfg["transmitters"] if t["mac"] == mac), None)
        if tx:
            tx.update(x=x, y=y)
            if args.name:
                tx["name"] = args.name
            print(f"Оновлено {mac} -> ({x}; {y})")
        else:
            cfg["transmitters"].append({"name": args.name or chr(65 + len(cfg["transmitters"])),
                                        "mac": mac, "x": x, "y": y})
            print(f"Зареєстровано {mac} -> ({x}; {y})")
        save_config(args.config, cfg)
    elif args.remove:
        mac = norm_mac(args.remove)
        before = len(cfg["transmitters"])
        cfg["transmitters"] = [t for t in cfg["transmitters"] if t["mac"] != mac]
        save_config(args.config, cfg)
        print("Видалено" if len(cfg["transmitters"]) < before else "Такого ID немає в списку")
    elif args.list:
        for t in cfg["transmitters"]:
            print(f"{t.get('name', ''):>3}  {t['mac']}  x={t['x']}  y={t['y']}")
        if not cfg["transmitters"]:
            print("Список порожній. Додайте: locator.py --add ID X Y")
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
    else:
        readings = Readings(cfg["window_sec"], keep_recent=KEEP_RECENT)
        reader = SerialReader(cfg["serial_port"], cfg["baud"], readings, cfg["default_tx_power_dbm"])
        reader.start()
        run_gui(cfg, args.config, readings, reader)
