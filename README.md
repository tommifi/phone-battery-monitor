# phone-battery-monitor

Keeps the battery of an Android phone, permanently attached to a Raspberry Pi, between 40% and 80% to reduce Li-ion wear. The Pi cuts and restores the power of the phone's USB port with uhubctl, and reads the battery level over ADB via Wi-Fi.

## How it works

- Level >= 80%: the USB port is switched off, the phone discharges.
- Level <= 40%: the USB port is switched on, the phone charges.
- Between the two thresholds nothing changes (hysteresis).
- ADB runs over Wi-Fi because USB data is lost while the port is off.
- A real power loss (port on, but phone not powered) stops the heavy services and turns the screen off.

## Safety

- At start the port is always switched on.
- If the level cannot be read 3 times in a row with the port off, the port is switched on.
- At or below 15% the port is always switched on.
- If uhubctl fails 3 times in a row, one Telegram alert is sent.
- If the level drops below 25% with the port off, one Telegram alert is sent per discharge cycle.
- If the level stays unreadable for 10 cycles (ADB over Wi-Fi down), one Telegram alert is sent.

## Requirements

- Raspberry Pi with a USB hub supporting per-port power switching (ppps), and uhubctl installed.
- Android phone with ADB over Wi-Fi enabled (adb tcpip 5555). Without root this mode is lost at every phone reboot and must be re-enabled over USB.
- DHCP reservation for the phone, so its address does not change.
- Python 3.10 or newer, standard library only at runtime.
- Telegram alerts (optional): TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in an environment file, for example ~/.config/telegram/infra.env (mode 600, one bot per file). Do not put comments or quotes on those lines, systemd does not strip them.

## Setup

1. Edit the constants at the top of battery_monitor.py: DEFAULT_DEVICE, HUB_LOCATION, HUB_PORT, thresholds.
2. Find the hub and port of the phone with: sudo uhubctl
3. Add a passwordless sudo rule at the END of /etc/sudoers, after any rule that requires a password, because the last matching rule wins. A file in /etc/sudoers.d is not enough if an earlier include is overridden by a later line. Validate with visudo -cf before installing.
4. Install battery-monitor.service in ~/.config/systemd/user/ (user unit), enable linger with loginctl enable-linger, then: systemctl --user daemon-reload and systemctl --user enable --now battery-monitor.service

## Operation

    systemctl --user status battery-monitor.service
    journalctl --user -u battery-monitor.service -n 50 --no-pager
    sudo uhubctl -l 1 -p 1 -a on

The last command restores power to the port manually.

## Development

    uv venv && uv pip install pytest ruff mypy
    .venv/bin/ruff check .
    .venv/bin/mypy battery_monitor.py
    .venv/bin/python -m pytest -q

## Known limitations

- No root on the phone, so the charge limit is done from outside, by cutting the port power.
- The ADB over Wi-Fi mode is lost when the phone reboots; the monitor re-enables it through the USB connection (adb tcpip) once the port is powered.
- Tested on a ZUK Z2 Pro (Android 8) with a Raspberry Pi 5 and uhubctl 2.5.0.