"""Android battery protection monitor for a phone attached to the Raspberry Pi.

Keeps the battery between CHARGE_RESUME_LEVEL and CHARGE_STOP_LEVEL by
switching the phone's USB port power on and off with uhubctl (hub 1, port 1).
The battery level is read over ADB via Wi-Fi, because USB data is lost while
the port is off. It also reacts to real power loss by stopping heavy services
and turning the screen off.

How it is run
-------------
USER-level systemd service (not a system unit, not cron):

    Unit file : ~/.config/systemd/user/battery-monitor.service
    Commands  : systemctl --user status|restart battery-monitor.service
    Logs      : journalctl --user -u battery-monitor.service -n 50 --no-pager

Requirements
------------
- Linger enabled for the user (loginctl enable-linger).
- ADB over Wi-Fi enabled on the phone (adb tcpip 5555). The mode is lost when
  the phone reboots; the monitor re-enables it through the USB connection.
- Sudoers rule /etc/sudoers.d/uhubctl-phone allowing only
  "uhubctl -l 1 -p 1 -a on|off" without password.

Safety
------
- On start the port is switched on.
- If the level cannot be read MAX_READ_FAILURES times in a row while the port
  is off, the port is switched back on.
- At or below CRITICAL_LEVEL the port is always switched on.
"""

import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

MIN_SCREEN_BRIGHTNESS = 0
DEFAULT_DEVICE = "192.168.100.115:5555"
UHUBCTL_PATH = Path("/usr/sbin/uhubctl")
HUB_LOCATION = "1"
HUB_PORT = "1"
CHARGE_STOP_LEVEL = 80
CHARGE_RESUME_LEVEL = 40
CRITICAL_LEVEL = 15
MAX_READ_FAILURES = 3
GRACE_CYCLES = 2
MAX_SWITCH_FAILURES = 3
LOW_LEVEL_ALERT = 25
WIFI_RESTORE_AFTER_FAILURES = 2
ADB_DOWN_ALERT_FAILURES = 10
TCPIP_SETTLE_SECONDS = 3
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TG_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TG_CHAT_ENV = "TELEGRAM_CHAT_ID"


def send_telegram_message(text: str) -> bool:
    """Send a Telegram message using credentials from the environment.

    Uses only the standard library, so it works with the system Python.

    Args:
        text: Message body to send.

    Returns:
        True if the message was sent, False otherwise.
    """
    token = os.environ.get(TG_TOKEN_ENV)
    chat_id = os.environ.get(TG_CHAT_ENV)
    if not token or not chat_id:
        logger.error("Telegram credentials not set in environment; alert not sent.")
        return False
    url = TELEGRAM_API_URL.format(token=token)
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10):
            return True
    except (urllib.error.URLError, OSError) as err:
        logger.error("Telegram alert failed: %s", type(err).__name__)
        return False


class BatteryMonitor:
    """Controls phone charging by port power and reacts to power loss."""

    def __init__(
        self,
        adb_path: Path = Path("/usr/bin/adb"),
        check_interval: int = 15,
        heavy_services: list[str] | None = None,
        device: str = DEFAULT_DEVICE,
        stop_level: int = CHARGE_STOP_LEVEL,
        resume_level: int = CHARGE_RESUME_LEVEL,
    ) -> None:
        """Initialize the BatteryMonitor instance.

        Args:
            adb_path: Path to the adb executable.
            check_interval: Polling interval in seconds.
            heavy_services: List of Android process names to stop on power loss.
            device: ADB serial of the phone (Wi-Fi address host:port).
            stop_level: Battery level at which the USB port is switched off.
            resume_level: Battery level at which the USB port is switched on.

        Raises:
            ValueError: If stop_level is not greater than resume_level.
        """
        if stop_level <= resume_level:
            raise ValueError("stop_level must be greater than resume_level")
        self.adb_path: Path = adb_path
        self.check_interval: int = check_interval
        self.heavy_services: list[str] = (
            heavy_services
            if heavy_services is not None
            else ["com.github.uiautomator", "atx-agent"]
        )
        self.device: str = device
        self.stop_level: int = stop_level
        self.resume_level: int = resume_level
        self.is_power_connected: bool = True
        self.charging_enabled: bool = True
        self.read_failures: int = 0
        self.grace_cycles: int = 0
        self.switch_failures: int = 0
        self.alert_sent: bool = False
        self.low_alert_sent: bool = False
        self.adb_alert_sent: bool = False

    def _run_adb_cmd(self, args: list[str]) -> str | None:
        """Execute an ADB command on the configured device and return stdout.

        Args:
            args: Command line arguments to pass to ADB.

        Returns:
            Decoded stdout string if successful, None otherwise.
        """
        cmd = [str(self.adb_path), "-s", self.device] + args
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
            logger.error("ADB command failed (%s): %s", " ".join(cmd), err)
            return None

    def is_ac_or_usb_powered(self) -> bool:
        """Check if the device is receiving power via AC or USB.

        Returns:
            True if device is charging/powered, False otherwise.
        """
        output = self._run_adb_cmd(["shell", "dumpsys", "battery"])
        if not output:
            logger.warning("Unable to fetch battery status via ADB.")
            return False

        ac_online = "AC powered: true" in output
        usb_online = "USB powered: true" in output
        return ac_online or usb_online

    def get_battery_level(self) -> int:
        """Get the current battery percentage level.

        Returns:
            Integer battery level percentage, or -1 on error.
        """
        output = self._run_adb_cmd(["shell", "dumpsys", "battery"])
        if not output:
            return -1

        for line in output.splitlines():
            if "level:" in line:
                try:
                    return int(line.split(":")[1].strip())
                except ValueError:
                    break
        return -1

    def set_charging(self, enabled: bool) -> bool:
        """Switch the phone's USB port power on or off with uhubctl.

        Args:
            enabled: True to power the port, False to cut it.

        Returns:
            True if the command succeeded, False otherwise.
        """
        action = "on" if enabled else "off"
        cmd = [
            "sudo", "-n", str(UHUBCTL_PATH),
            "-l", HUB_LOCATION, "-p", HUB_PORT, "-a", action,
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=20)
        except subprocess.CalledProcessError as err:
            logger.error(
                "uhubctl %s failed (exit %d): %s",
                action,
                err.returncode,
                (err.stderr or "").strip(),
            )
            self._register_switch_failure(action)
            return False
        except (subprocess.TimeoutExpired, OSError) as err:
            logger.error("uhubctl %s failed: %s", action, err)
            self._register_switch_failure(action)
            return False
        self.switch_failures = 0
        self.alert_sent = False
        self.charging_enabled = enabled
        self.grace_cycles = GRACE_CYCLES if enabled else 0
        logger.info("USB port power switched %s.", action)
        return True

    def _register_switch_failure(self, action: str) -> None:
        """Count a failed port switch and send one alert per failure streak.

        Args:
            action: The attempted action, "on" or "off".
        """
        self.switch_failures += 1
        if self.switch_failures >= MAX_SWITCH_FAILURES and not self.alert_sent:
            host = socket.gethostname()
            self.alert_sent = send_telegram_message(
                f"battery-monitor on {host}: uhubctl {action} failed "
                f"{self.switch_failures} times in a row. Check the journal."
            )

    def _check_low_level(self, level: int) -> None:
        """Send one alert per discharge cycle if the level is low with the port off.

        Args:
            level: Current battery level in percent.
        """
        if self.charging_enabled:
            self.low_alert_sent = False
            return
        if level < LOW_LEVEL_ALERT and not self.low_alert_sent:
            logger.error("Level %d%% with USB port off, sending alert.", level)
            host = socket.gethostname()
            self.low_alert_sent = send_telegram_message(
                f"battery-monitor on {host}: phone at {level}% with USB port off. "
                "Run: sudo uhubctl -l 1 -p 1 -a on"
            )

    def _run_adb_global(self, args: list[str], timeout: int = 15) -> str | None:
        """Execute an ADB command without selecting a device.

        Args:
            args: Command line arguments to pass to ADB.
            timeout: Seconds before the command is abandoned.

        Returns:
            Decoded stdout string if successful, None otherwise.
        """
        cmd = [str(self.adb_path)] + args
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=timeout
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as err:
            logger.warning("ADB command failed (%s): %s", " ".join(cmd), err)
            return None

    def _wifi_adb_reachable(self) -> bool:
        """Check whether the phone answers over its Wi-Fi ADB address.

        Returns:
            True if the device state is "device", False otherwise.
        """
        return self._run_adb_cmd(["get-state"]) == "device"

    def _usb_serial(self) -> str | None:
        """Find a phone attached over USB and ready for commands.

        Returns:
            The USB serial, or None if no ready USB device is listed.
        """
        output = self._run_adb_global(["devices"])
        if not output:
            return None
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device" and ":" not in parts[0]:
                return parts[0]
        return None

    def _restore_wifi_adb(self) -> bool:
        """Bring ADB over Wi-Fi back after a phone reboot or a network drop.

        Tries a plain reconnect first. If the phone does not answer, it looks
        for the phone on USB and switches it back to TCP mode with adb tcpip.

        Returns:
            True if the phone answers over Wi-Fi at the end, False otherwise.
        """
        self._run_adb_global(["connect", self.device])
        if self._wifi_adb_reachable():
            logger.info("ADB over Wi-Fi reconnected.")
            return True
        serial = self._usb_serial()
        if serial is None:
            logger.warning("No USB device available to re-enable ADB over Wi-Fi.")
            return False
        port = self.device.rpartition(":")[2] if ":" in self.device else "5555"
        logger.warning("Re-enabling ADB over Wi-Fi through USB device %s.", serial)
        self._run_adb_global(["-s", serial, "tcpip", port])
        time.sleep(TCPIP_SETTLE_SECONDS)
        self._run_adb_global(["connect", self.device])
        restored = self._wifi_adb_reachable()
        if restored:
            logger.info("ADB over Wi-Fi restored.")
        else:
            logger.warning("ADB over Wi-Fi still unreachable after tcpip.")
        return restored

    def _recover_wifi_adb(self) -> None:
        """Try to restore ADB over Wi-Fi while reads fail, and alert once if it stays down."""
        if self.read_failures >= WIFI_RESTORE_AFTER_FAILURES:
            self._restore_wifi_adb()
        if self.read_failures >= ADB_DOWN_ALERT_FAILURES and not self.adb_alert_sent:
            host = socket.gethostname()
            self.adb_alert_sent = send_telegram_message(
                f"battery-monitor on {host}: ADB over Wi-Fi unreachable for "
                f"{self.read_failures} cycles. If the phone rebooted, run: adb tcpip 5555"
            )

    def _update_charge_state(self, level: int) -> None:
        """Apply the hysteresis rules to the charging port.

        Args:
            level: Current battery level in percent.
        """
        resume = max(self.resume_level, CRITICAL_LEVEL)
        if self.charging_enabled and level >= self.stop_level:
            logger.info("Level %d%% reached, stopping charge.", level)
            self.set_charging(False)
        elif not self.charging_enabled and level <= resume:
            logger.info("Level %d%% reached, resuming charge.", level)
            self.set_charging(True)

    def _handle_read_failure(self) -> None:
        """Count a failed level read and force the port on if it persists."""
        self.read_failures += 1
        logger.warning("Battery level unreadable (%d).", self.read_failures)
        if self.read_failures >= MAX_READ_FAILURES and not self.charging_enabled:
            logger.error("Fail-safe: level unknown, switching port on.")
            self.set_charging(True)

    def _check_power_loss(self, level: int) -> None:
        """Detect real power loss while the port is meant to be on.

        Args:
            level: Current battery level in percent, used for logging.
        """
        if not self.charging_enabled:
            return
        if self.grace_cycles > 0:
            self.grace_cycles -= 1
            return
        powered = self.is_ac_or_usb_powered()
        if not powered and self.is_power_connected:
            logger.error("Power loss detected! Battery level: %d%%", level)
            self.is_power_connected = False
            self.stop_heavy_services()
        elif powered and not self.is_power_connected:
            logger.info("Power restored! Battery level: %d%%", level)
            self.is_power_connected = True
            self.restart_heavy_services()

    def _dim_display(self) -> None:
        """Set display brightness to minimum level via ADB."""
        logger.info("Setting screen brightness to minimum (%d).", MIN_SCREEN_BRIGHTNESS)
        self._run_adb_cmd(
            ["shell", "settings", "put", "system", "screen_brightness", str(MIN_SCREEN_BRIGHTNESS)]
        )

    def stop_heavy_services(self) -> None:
        """Stop resource-intensive services and dim display to save power."""
        logger.warning("Power disconnected! Executing power mitigation steps.")
        for service in self.heavy_services:
            logger.info("Stopping background service: %s", service)
            self._run_adb_cmd(["shell", "am", "force-stop", service])

        self._dim_display()
        logger.info("Turning screen off to conserve power.")
        self._run_adb_cmd(["shell", "input", "keyevent", "26"])

    def restart_heavy_services(self) -> None:
        """Restart services after power restored."""
        logger.info("Power restored! Resuming operations.")
        for service in self.heavy_services:
            logger.info("Power stable. Ready to resume: %s", service)

    def monitor_loop(self) -> None:
        """Run the main monitoring loop indefinitely."""
        logger.info("Starting 24/7 Battery Protection Monitor...")
        self.set_charging(True)
        self._dim_display()

        while True:
            level = self.get_battery_level()
            if level < 0:
                self._handle_read_failure()
                self._recover_wifi_adb()
            else:
                self.read_failures = 0
                self.adb_alert_sent = False
                logger.debug("Level: %d%%, charging: %s", level, self.charging_enabled)
                self._update_charge_state(level)
                self._check_low_level(level)
                self._check_power_loss(level)
            time.sleep(self.check_interval)


if __name__ == "__main__":
    monitor = BatteryMonitor()
    try:
        monitor.monitor_loop()
    except KeyboardInterrupt:
        logger.info("Battery monitor stopped by user.")
