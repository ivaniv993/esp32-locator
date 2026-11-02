"""Джерела даних: читання приймача з послідовного порту."""
import glob
import os
import threading
import time

from .protocol import parse_line


class SerialReader(threading.Thread):
    """Читає приймач у фоні, сам знаходить порт і перепідключається."""
    def __init__(self, port, baud, readings, default_txp=14, on_line=None):
        super().__init__(daemon=True)
        self.port, self.baud, self.readings = port, baud, readings
        self.default_txp = default_txp
        self.on_line = on_line        # викликається з сирим рядком кожного розпізнаного виміру (для запису)
        self.status = "Пошук приймача…"

    def candidates(self):
        """Автопошук шукає лише USB-приймачі (/dev/ttyUSB*, /dev/ttyACM*) і /dev/serial0.
        На Orange Pi апаратні UART називаються /dev/ttyS<N>, і один із них зазвичай
        зайнятий системною консоллю, тому для UART порт треба вказати явно (serial_port)."""
        if self.port != "auto":
            return [self.port]
        ports = sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
        if os.path.exists("/dev/serial0"):
            ports.append("/dev/serial0")
        return ports

    def run(self):
        try:
            import serial
        except ImportError:
            self.status = "Не встановлено pyserial: sudo apt install python3-serial"
            return
        while True:
            ports = self.candidates()
            if not ports:
                self.status = "Приймач не знайдено. Перевірте кабель (або вкажіть serial_port для UART)."
                time.sleep(2)
                continue
            for p in ports:
                try:
                    with serial.Serial(p, self.baud, timeout=1) as ser:
                        self.status = f"Підключено: {p}"
                        last_data = time.monotonic()
                        while True:
                            raw = ser.readline()
                            if raw:
                                got = self.parse(raw.decode("utf-8", "ignore"))
                                if got:
                                    last_data = time.monotonic()
                            # в автоматичному режимі пробуємо інший порт, якщо цей мовчить
                            if self.port == "auto" and len(ports) > 1 and time.monotonic() - last_data > 5:
                                self.status = f"Немає даних на {p}, пробую інший порт…"
                                break
                except (serial.SerialException, OSError) as e:
                    self.status = f"Помилка порту {p}: {e}"
                    time.sleep(1)

    def parse(self, line):
        """Додає пакет у Readings, якщо рядок — вимір. Повертає True/False."""
        got = parse_line(line, self.default_txp)
        if not got:
            return False
        self.readings.add(*got)
        if self.on_line:
            self.on_line(line)
        return True


class ReplaySource(threading.Thread):
    """Відтворює запис у реальному часі, ніби це живий приймач (той самий інтерфейс, що й SerialReader)."""

    def __init__(self, session, readings, default_txp=14, loop=False):
        super().__init__(daemon=True)
        self.session, self.readings = session, readings
        self.default_txp, self.loop = default_txp, loop
        self.name_ = os.path.basename(session.path)
        self.status = f"Відтворення: {self.name_}"

    def run(self):
        total = self.session.duration
        if not self.session.rx:
            self.status = f"У записі немає пакетів: {self.name_}"
            return
        while True:
            start = time.monotonic()
            for t, line in self.session.rx:
                delay = start + t - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                got = parse_line(line, self.default_txp)
                if got:
                    self.readings.add(*got)
                self.status = f"Відтворення: {self.name_}  {t:.0f}/{total:.0f} с"
            if not self.loop:
                self.status = f"Відтворення завершено: {self.name_}"
                return
