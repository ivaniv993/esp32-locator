#!/usr/bin/env bash
# Встановлення на Orange Pi (Armbian / Orange Pi OS / Debian / Ubuntu).
#   ./install.sh              — встановити залежності
#   ./install.sh --autostart  — ще й запускати програму разом із робочим столом
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

sudo apt update
sudo apt install -y python3 python3-serial python3-pygame fonts-dejavu-core

# доступ до послідовних портів без sudo
sudo usermod -aG dialout "$USER"

if [ "$1" = "--autostart" ]; then
  mkdir -p "$HOME/.config/autostart"
  cat > "$HOME/.config/autostart/esp32-locator.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=ESP32 Locator
Exec=python3 $DIR/locator.py
X-GNOME-Autostart-enabled=true
DESKTOP
  echo "Автозапуск увімкнено: ~/.config/autostart/esp32-locator.desktop"
fi

echo
echo "Готово. Перезайдіть у систему (або перезавантажте плату), щоб застосувався доступ до портів."
echo "Далі: python3 $DIR/locator.py --calibrate"
