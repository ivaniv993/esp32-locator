#!/usr/bin/env python3
"""
Позиціонування приймача за LoRa-маяками (прошивка Akceptor/esp-beacon).

Маяк двічі на секунду надсилає свій chip ID і потужність передавача.
Приймач на кожен пакет друкує в Serial рядок CSV:
    chipId,rssi_dBm,snr_dB,txPower_dBm
    A1B2C3D4E5F6,-67.5,9.2,14
Програма рахує втрати сигналу (потужність − RSSI), усереднює їх для кожного маяка,
переводить у відстань, обчислює положення приймача і малює його на екрані.

Запуск:
    python3 locator.py                          — екран з мапою
    python3 locator.py --list                   — зареєстровані маяки
    python3 locator.py --add ID X Y --name A    — зареєструвати / оновити маяк
    python3 locator.py --remove ID              — видалити маяк
    python3 locator.py --calibrate              — режим калібрування
    python3 locator.py --record --truth X Y     — записати сесію (приймач стоїть у точці X, Y)
    python3 locator.py --replay FILE            — відтворити запис замість приймача
    python3 locator.py --evaluate FILE...       — оцінити алгоритм на записах (--save / --baseline JSON)
Клавіші на екрані: F — повний екран, C — очистити слід, Q або Esc — вихід.
Незареєстрований маяк: торкніться його ID у списку, потім місця на мапі.

Уся логіка — у пакеті esp32_locator/ (config, protocol, sources, readings, session, solver, metrics, evaluate, gui, cli).
"""
from esp32_locator.cli import main

if __name__ == "__main__":
    main()
