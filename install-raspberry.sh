#!/usr/bin/env bash
# Інсталятор esp32-locator для Raspberry Pi (Raspberry Pi OS).
#
#   ./install-raspberry.sh              — встановити або оновити
#   ./install-raspberry.sh --uart       — ще й увімкнути UART (/dev/serial0) для приймача на GPIO
#   ./install-raspberry.sh --no-autostart — без автозапуску разом із робочим столом
#   ./install-raspberry.sh --uninstall  — видалити програму (налаштування залишаться)
#
# Куди встановлюється:
#   /opt/esp32-locator/locator.py           — точка входу
#   /opt/esp32-locator/esp32_locator/       — модулі програми
#   /usr/local/bin/esp32-locator            — команда для запуску
#   ~/.config/esp32-locator/config.json     — ваші налаштування і маяки (не перезаписуються)
#   ~/.config/autostart/esp32-locator.desktop — автозапуск
#
# Оновлення: git pull у папці репозиторію і знову ./install-raspberry.sh
set -e

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="/opt/esp32-locator"
BIN="/usr/local/bin/esp32-locator"

# --- хто користувач (працює і через sudo, і без нього)
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
  TARGET_USER="${SUDO_USER:-root}"
else
  SUDO="sudo"
  TARGET_USER="$(id -un)"
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
CONF_DIR="$TARGET_HOME/.config/esp32-locator"
AUTOSTART="$TARGET_HOME/.config/autostart/esp32-locator.desktop"

UART=0; AUTO=1; UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --uart) UART=1 ;;
    --no-autostart) AUTO=0 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "Невідомий параметр: $arg (див. --help)"; exit 1 ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if [ "$UNINSTALL" -eq 1 ]; then
  say "Видалення esp32-locator"
  $SUDO rm -rf "$APP_DIR" "$BIN"
  rm -f "$AUTOSTART"
  echo "Програму видалено. Налаштування залишились у $CONF_DIR"
  exit 0
fi

if [ ! -f "$SRC_DIR/locator.py" ] || [ ! -d "$SRC_DIR/esp32_locator" ]; then
  echo "Не знайдено locator.py або esp32_locator/ поруч зі скриптом. Запускайте з папки репозиторію."
  exit 1
fi
if ! grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
  echo "Увага: це, схоже, не Raspberry Pi. Продовжую — скрипт працює й на Armbian/Debian/Ubuntu."
fi

say "1/5  Встановлення залежностей"
if [ -z "$ESP32_LOCATOR_SKIP_APT" ]; then
  $SUDO apt-get update
  $SUDO apt-get install -y python3 python3-serial python3-pygame fonts-dejavu-core
fi

say "2/5  Копіювання програми в $APP_DIR"
$SUDO install -d -m 755 "$APP_DIR" "$APP_DIR/esp32_locator"
$SUDO install -m 755 "$SRC_DIR/locator.py" "$APP_DIR/locator.py"
$SUDO rm -f "$APP_DIR"/esp32_locator/*.py          # прибираємо модулі, яких уже немає в репо
$SUDO install -m 644 "$SRC_DIR"/esp32_locator/*.py "$APP_DIR/esp32_locator/"
$SUDO tee "$BIN" >/dev/null <<'LAUNCHER'
#!/bin/sh
export ESP32_LOCATOR_CONFIG="$HOME/.config/esp32-locator/config.json"
exec python3 /opt/esp32-locator/locator.py "$@"
LAUNCHER
$SUDO chmod 755 "$BIN"

say "3/5  Налаштування в $CONF_DIR"
mkdir -p "$CONF_DIR"
if [ ! -f "$CONF_DIR/config.json" ]; then
  # переносимо маяки зі старого ручного встановлення, якщо воно було
  for old in "$TARGET_HOME/esp32-locator/pi/config.json" "$TARGET_HOME/esp32-locator/config.json" "$SRC_DIR/config.json"; do
    if [ -f "$old" ]; then
      cp "$old" "$CONF_DIR/config.json"
      echo "Перенесено налаштування з $old"
      break
    fi
  done
else
  echo "Існуючий config.json залишено без змін."
fi

say "4/5  Доступ до послідовних портів"
$SUDO usermod -aG dialout "$TARGET_USER"
echo "Користувача $TARGET_USER додано до групи dialout."
if [ "$UART" -eq 1 ]; then
  if command -v raspi-config >/dev/null; then
    $SUDO raspi-config nonint do_serial_cons 1 2>/dev/null && $SUDO raspi-config nonint do_serial_hw 0 2>/dev/null \
      || $SUDO raspi-config nonint do_serial 2
    echo "UART увімкнено (/dev/serial0), системну консоль на ньому вимкнено. Потрібне перезавантаження."
  else
    echo "raspi-config не знайдено — увімкніть UART вручну."
  fi
fi

say "5/5  Автозапуск"
rm -f "$TARGET_HOME/.config/autostart/locator.desktop"   # старий файл із ручного встановлення
if [ "$AUTO" -eq 1 ]; then
  mkdir -p "$(dirname "$AUTOSTART")"
  cat > "$AUTOSTART" <<DESKTOP
[Desktop Entry]
Type=Application
Name=ESP32 Locator
Exec=$BIN
X-GNOME-Autostart-enabled=true
DESKTOP
  echo "Програма запускатиметься разом із робочим столом."
else
  rm -f "$AUTOSTART"
  echo "Автозапуск вимкнено."
fi

# файли в домашній папці мають належати користувачу, навіть якщо скрипт запущено через sudo
if [ "$(id -u)" -eq 0 ] && [ "$TARGET_USER" != "root" ]; then
  chown -R "$TARGET_USER": "$CONF_DIR" "$TARGET_HOME/.config/autostart" 2>/dev/null || true
fi

say "Готово!"
cat <<TXT
Перезавантажте Raspberry Pi (sudo reboot), щоб застосувався доступ до портів.

Команди:
  esp32-locator --calibrate                      перевірка зв’язку і калібрування
  esp32-locator --add A1B2C3D4E5F6 0 0 --name A  зареєструвати маяк
  esp32-locator --list                           список маяків
  esp32-locator                                  екран з мапою
Налаштування: $CONF_DIR/config.json
TXT
