#!/usr/bin/env bash
# Інсталятор esp32-locator для Raspberry Pi (Raspberry Pi OS).
#
#   ./install-raspberry.sh              — встановити або оновити
#   ./install-raspberry.sh --uart       — ще й увімкнути UART (/dev/serial0) для приймача на GPIO
#   ./install-raspberry.sh --no-autostart — без автозапуску разом із робочим столом
#   ./install-raspberry.sh --service    — без екрана: служба з веб-сторінкою стартує при ввімкненні
#   ./install-raspberry.sh --service --https — те саме через HTTPS (кнопка «Маяк тут» з GPS телефона)
#   ./install-raspberry.sh --hotspot    — Pi роздає власну Wi-Fi «esp32-locator»; телефон підключається
#                                          до неї й відкриває https://10.42.0.1:8080
#   ./install-raspberry.sh --wifi-country UA — країна Wi-Fi (без неї Raspberry Pi OS блокує Wi-Fi)
#   ./install-raspberry.sh --no-hotspot — прибрати точку доступу
#   ./install-raspberry.sh --uninstall  — видалити програму (налаштування залишаться)
#
# Пароль точки доступу: ESP32_LOCATOR_HOTSPOT_PASSWORD=... ./install-raspberry.sh --hotspot
# (8–63 символи; без нього створюється випадковий і друкується в кінці).
#
# Куди встановлюється:
#   /opt/esp32-locator/locator.py           — точка входу
#   /opt/esp32-locator/esp32_locator/       — модулі програми й веб-сторінка (static/)
#   /usr/local/bin/esp32-locator            — команда для запуску
#   ~/.config/esp32-locator/config.json     — ваші налаштування і маяки (не перезаписуються)
#   ~/.config/autostart/esp32-locator.desktop — автозапуск з екраном
#   /etc/systemd/system/esp32-locator.service — служба без екрана (--service)
#   /etc/sudoers.d/esp32-locator            — дозвіл службі вимкнути Pi (кнопка на веб-сторінці)
#   NetworkManager: з’єднання esp32-locator-hotspot — точка доступу (--hotspot)
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
SERVICE="/etc/systemd/system/esp32-locator.service"
SUDOERS="/etc/sudoers.d/esp32-locator"
HOTSPOT_CON="esp32-locator-hotspot"
HOTSPOT_SSID="${ESP32_LOCATOR_HOTSPOT_SSID:-esp32-locator}"
HOTSPOT_IP="10.42.0.1"

UART=0; AUTO=1; UNINSTALL=0; SVC=0; HTTPS=0; HOTSPOT=0; NO_HOTSPOT=0; WIFI_COUNTRY=""
ORIG_ARGS="$*"
while [ $# -gt 0 ]; do
  case "$1" in
    --uart) UART=1 ;;
    --no-autostart) AUTO=0 ;;
    --service) SVC=1 ;;
    --https) HTTPS=1 ;;
    --hotspot) HOTSPOT=1 ;;
    --no-hotspot) NO_HOTSPOT=1 ;;
    --wifi-country) shift; WIFI_COUNTRY="$(printf '%s' "${1:-}" | tr '[:lower:]' '[:upper:]')" ;;
    --wifi-country=*) WIFI_COUNTRY="$(printf '%s' "${1#*=}" | tr '[:lower:]' '[:upper:]')" ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n "2,$(grep -n '^# Оновлення' "$0" | cut -d: -f1)p" "$0"; exit 0 ;;
    *) echo "Невідомий параметр: $1 (див. --help)"; exit 1 ;;
  esac
  shift
done
if [ "$HOTSPOT" -eq 1 ] && [ "$NO_HOTSPOT" -eq 1 ]; then
  echo "--hotspot і --no-hotspot разом не можна."; exit 1
fi
if [ -n "$WIFI_COUNTRY" ] && ! printf '%s' "$WIFI_COUNTRY" | grep -qE '^[A-Z]{2}$'; then
  echo "--wifi-country: двобуквений код країни, наприклад UA, PL, DE."; exit 1
fi
if [ "$HTTPS" -eq 1 ] && [ "$SVC" -eq 0 ]; then
  echo "--https працює разом із --service."; exit 1
fi

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

remove_service() {
  if [ -f "$SERVICE" ]; then
    $SUDO systemctl disable --now esp32-locator.service 2>/dev/null || true
    $SUDO rm -f "$SERVICE"
    $SUDO systemctl daemon-reload
  fi
  $SUDO rm -f "$SUDOERS"
}

# кнопка «Вимкнути Pi» на веб-сторінці: служба працює від користувача, тож дозволяємо йому без пароля
# лише одну команду — systemctl poweroff (обидва шляхи: на старіших системах systemctl лежить у /bin)
allow_poweroff() {
  [ "$TARGET_USER" = "root" ] && return
  local tmp="$SUDOERS.new"     # файли з крапкою в назві sudo не читає — поки не перевірено, він не діє
  printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /bin/systemctl poweroff\n' "$TARGET_USER" \
    | $SUDO tee "$tmp" >/dev/null
  $SUDO chmod 440 "$tmp"
  if $SUDO visudo -cf "$tmp" >/dev/null; then
    $SUDO mv "$tmp" "$SUDOERS"
    echo "Службі дозволено вимикати Pi (кнопка «Вимкнути Pi» на сторінці)."
  else
    $SUDO rm -f "$tmp"
    echo "Увага: не вдалося додати дозвіл на вимкнення — кнопка «Вимкнути Pi» не працюватиме."
  fi
}

hotspot_exists() {
  command -v nmcli >/dev/null && nmcli -t -f NAME con show 2>/dev/null | grep -qx "$HOTSPOT_CON"
}

remove_hotspot() {
  if hotspot_exists; then
    $SUDO nmcli con delete "$HOTSPOT_CON" >/dev/null
    echo "Точку доступу $HOTSPOT_SSID прибрано."
  fi
}

# Перевіряє все, що потрібно точці доступу, ДО змін у системі; задає HOTSPOT_IFACE.
check_hotspot() {
  if ! command -v nmcli >/dev/null; then
    echo "Для --hotspot потрібен NetworkManager (є в Raspberry Pi OS Bookworm і новіших)."
    echo "На старішій системі оновіть Raspberry Pi OS."
    exit 1
  fi
  HOTSPOT_IFACE="$(nmcli -t -f DEVICE,TYPE dev 2>/dev/null | awk -F: '$2 == "wifi" {print $1; exit}')"
  if [ -z "$HOTSPOT_IFACE" ]; then
    echo "Wi-Fi модуль не знайдено. У Raspberry Pi Zero 2 W Wi-Fi вбудований — перевірте, чи його не вимкнено"
    echo "(рядок dtoverlay=disable-wifi у /boot/firmware/config.txt) і чи бачить його система: nmcli dev status"
    exit 1
  fi
  if command -v iw >/dev/null; then
    local phy
    phy="$(iw dev "$HOTSPOT_IFACE" info 2>/dev/null | awk '/wiphy/ {print "phy" $2}')"
    if [ -n "$phy" ] && ! iw phy "$phy" info 2>/dev/null | sed -n '/Supported interface modes/,/^\s*[A-Z]/p' | grep -qE '\* AP$'; then
      echo "Wi-Fi модуль $HOTSPOT_IFACE не вміє працювати точкою доступу (немає режиму AP)."
      echo "Потрібен інший USB Wi-Fi адаптер — перевірка: iw list | grep -A 10 'Supported interface modes'"
      exit 1
    fi
  fi
  local p="${ESP32_LOCATOR_HOTSPOT_PASSWORD:-}"
  if [ -n "$p" ] && { [ "${#p}" -lt 8 ] || [ "${#p}" -gt 63 ]; }; then
    echo "Пароль точки доступу має бути від 8 до 63 символів."; exit 1
  fi
  # Raspberry Pi OS блокує Wi-Fi, поки не задано країну
  if [ -z "$WIFI_COUNTRY" ] && command -v rfkill >/dev/null && rfkill list wifi 2>/dev/null | grep -q "Soft blocked: yes"; then
    echo "Wi-Fi заблоковано: не задано країну. Повторіть з --wifi-country, наприклад:"
    echo "  ./install-raspberry.sh $ORIG_ARGS --wifi-country UA"
    exit 1
  fi
}

setup_hotspot() {
  if [ -n "$WIFI_COUNTRY" ]; then
    if command -v raspi-config >/dev/null; then
      $SUDO raspi-config nonint do_wifi_country "$WIFI_COUNTRY" >/dev/null 2>&1 || true
    elif command -v iw >/dev/null; then
      $SUDO iw reg set "$WIFI_COUNTRY" || true
    fi
    echo "Країна Wi-Fi: $WIFI_COUNTRY"
  fi
  if command -v rfkill >/dev/null && rfkill list wifi 2>/dev/null | grep -q "Soft blocked: yes"; then
    $SUDO rfkill unblock wifi
  fi

  local pass="${ESP32_LOCATOR_HOTSPOT_PASSWORD:-}"
  if [ -z "$pass" ] && hotspot_exists; then
    pass="$($SUDO nmcli -s -g 802-11-wireless-security.psk con show "$HOTSPOT_CON" 2>/dev/null || true)"
  fi
  [ -z "$pass" ] && pass="$(tr -dc 'a-z0-9' </dev/urandom | head -c 10)"
  if [ "${#pass}" -lt 8 ] || [ "${#pass}" -gt 63 ]; then
    echo "Пароль точки доступу має бути від 8 до 63 символів."; exit 1
  fi

  # Пріоритет 100: точка доступу перемагає збережені домашні мережі — Pi роздає Wi-Fi завжди.
  # ipv4.method shared: Pi видає телефону адресу сам, а собі бере сталу 10.42.0.1.
  local settings=(ifname "$HOTSPOT_IFACE" autoconnect yes connection.autoconnect-priority 100
    ssid "$HOTSPOT_SSID" 802-11-wireless.mode ap 802-11-wireless.band bg
    ipv4.method shared ipv4.addresses "$HOTSPOT_IP/24" ipv6.method disabled
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$pass")
  if hotspot_exists; then
    $SUDO nmcli con modify "$HOTSPOT_CON" "${settings[@]}"
    echo "Точку доступу $HOTSPOT_SSID оновлено."
  else
    $SUDO nmcli con add type wifi con-name "$HOTSPOT_CON" "${settings[@]}" >/dev/null
    echo "Створено точку доступу $HOTSPOT_SSID."
  fi
  HOTSPOT_PASS="$pass"
}

# Вмикає точку доступу одразу — але не обриває ssh, що йде через цей самий Wi-Fi.
start_hotspot() {
  if [ -n "${SSH_CONNECTION:-}" ]; then
    local client via
    client="$(printf '%s' "$SSH_CONNECTION" | awk '{print $1}')"
    via="$(ip route get "$client" 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") print $(i + 1)}')"
    if [ "$via" = "$HOTSPOT_IFACE" ]; then
      echo "Ви підключені по ssh через Wi-Fi ($HOTSPOT_IFACE), тож точку доступу зараз не вмикаю —"
      echo "інакше з’єднання обірветься. Вона ввімкнеться після sudo reboot."
      return
    fi
  fi
  if $SUDO nmcli con up "$HOTSPOT_CON" >/dev/null 2>&1; then
    echo "Точка доступу працює: $HOTSPOT_SSID, адреса Pi $HOTSPOT_IP."
  else
    echo "Не вдалося ввімкнути точку доступу зараз — спробуйте після sudo reboot. Діагностика:"
    echo "  nmcli con up $HOTSPOT_CON"
  fi
}

if [ "$UNINSTALL" -eq 1 ]; then
  say "Видалення esp32-locator"
  remove_service
  remove_hotspot
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
HOTSPOT_PASS=""
[ "$HOTSPOT" -eq 1 ] && check_hotspot

say "1/5  Встановлення залежностей"
if [ -z "$ESP32_LOCATOR_SKIP_APT" ]; then
  $SUDO apt-get update
  $SUDO apt-get install -y python3 python3-serial python3-pygame fonts-dejavu-core
  # точці доступу NetworkManager потрібен dnsmasq (видає телефону адресу); iw — для перевірки режиму AP
  [ "$HOTSPOT" -eq 1 ] && $SUDO apt-get install -y dnsmasq-base iw
fi

say "2/5  Копіювання програми в $APP_DIR"
$SUDO install -d -m 755 "$APP_DIR" "$APP_DIR/esp32_locator" "$APP_DIR/esp32_locator/static"
$SUDO install -m 755 "$SRC_DIR/locator.py" "$APP_DIR/locator.py"
$SUDO rm -f "$APP_DIR"/esp32_locator/*.py "$APP_DIR"/esp32_locator/static/*   # прибираємо те, чого вже немає в репо
$SUDO install -m 644 "$SRC_DIR"/esp32_locator/*.py "$APP_DIR/esp32_locator/"
$SUDO install -m 644 "$SRC_DIR"/esp32_locator/static/* "$APP_DIR/esp32_locator/static/"
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
if [ "$SVC" -eq 1 ]; then
  # служба і програма з робочого столу одночасно змагалися б за порт приймача — лишаємо одне
  AUTO=0
  WEB_ARGS="--web"
  [ "$HTTPS" -eq 1 ] && WEB_ARGS="--web-https"
  $SUDO tee "$SERVICE" >/dev/null <<UNIT
[Unit]
Description=ESP32 LoRa locator (без екрана, веб-сторінка)
After=network.target

[Service]
User=$TARGET_USER
Environment=ESP32_LOCATOR_CONFIG=$CONF_DIR/config.json
ExecStart=/usr/bin/python3 -u $APP_DIR/locator.py --headless $WEB_ARGS
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  allow_poweroff
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable esp32-locator.service
  $SUDO systemctl restart esp32-locator.service
  echo "Служба esp32-locator запускається при ввімкненні ($WEB_ARGS). Журнал: journalctl -u esp32-locator -f"
else
  remove_service
fi
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
  [ "$SVC" -eq 1 ] || echo "Автозапуск вимкнено."
fi

if [ "$HOTSPOT" -eq 1 ]; then
  say "Wi-Fi точка доступу"
  setup_hotspot
  start_hotspot
elif [ "$NO_HOTSPOT" -eq 1 ]; then
  say "Wi-Fi точка доступу"
  remove_hotspot
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
if [ -n "$HOTSPOT_PASS" ]; then
  SCHEME="http"; [ "$HTTPS" -eq 1 ] && SCHEME="https"
  cat <<TXT

Телефон:
  1. Wi-Fi «$HOTSPOT_SSID», пароль: $HOTSPOT_PASS
  2. Браузер: $SCHEME://$HOTSPOT_IP:8080
Pi тепер завжди роздає цю мережу (і не підключається до домашньої Wi-Fi).
Інтернет на Pi вдома: тимчасово  sudo nmcli con down $HOTSPOT_CON  (Pi підключиться до домашньої Wi-Fi),
потім  sudo nmcli con up $HOTSPOT_CON  або перезавантаження.
TXT
fi
