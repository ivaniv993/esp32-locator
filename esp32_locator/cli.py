"""Командний рядок: реєстрація маяків, калібрування, запуск екрана."""
import argparse
import glob
import sys
import time

from .config import CONFIG_PATH, load_config, save_config
from .gui import run_gui
from .protocol import norm_mac
from .readings import Readings
from .solver import distance_from_loss
from .sources import SerialReader


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
        try:
            x, y = float(args.add[1].replace(",", ".")), float(args.add[2].replace(",", "."))
        except ValueError:
            sys.exit("Координати мають бути числами, наприклад 2.5")
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
    else:
        run_gui(cfg, args.config)
