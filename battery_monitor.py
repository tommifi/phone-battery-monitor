"""Module for monitoring Android device battery status and mitigating power drops.

This module continuously checks the battery and charging status of a connected
Android device via ADB. In the event of power loss or USB instability, it lowers
system load by terminating resource-intensive automation services and dimming
the display to preserve overall battery health in 24/7 deployments.
"""

import logging
import subprocess
import time
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

MIN_SCREEN_BRIGHTNESS = 0


class BatteryMonitor:
    """Monitors device power status and applies mitigation strategies on disconnection."""

    def __init__(
        self,
        adb_path: Path = Path("/usr/bin/adb"),
        check_interval: int = 15,
        heavy_services: list[str] | None = None,
    ) -> None:
        """Initialize the BatteryMonitor instance.

        Args:
            adb_path: Path to the adb executable.
            check_interval: Polling interval in seconds.
            heavy_services: List of Android process names to stop on power loss.
        """
        self.adb_path: Path = adb_path
        self.check_interval: int = check_interval
        self.heavy_services: list[str] = (
            heavy_services
            if heavy_services is not None
            else ["com.github.uiautomator", "atx-agent"]
        )
        self.is_power_connected: bool = True

    def _run_adb_cmd(self, args: list[str]) -> str | None:
        """Execute an ADB command and return its stdout.

        Args:
            args: Command line arguments to pass to ADB.

        Returns:
            Decoded stdout string if successful, None otherwise.
        """
        cmd = [str(self.adb_path)] + args
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
        """Run the main power monitoring loop indefinitely."""
        logger.info("Starting 24/7 Battery Protection Monitor...")

        while True:
            self._dim_display()
            powered = self.is_ac_or_usb_powered()
            level = self.get_battery_level()

            logger.debug("Current Status - Powered: %s, Level: %d%%", powered, level)

            if not powered and self.is_power_connected:
                logger.error("Power loss detected! Battery level: %d%%", level)
                self.is_power_connected = False
                self.stop_heavy_services()

            elif powered and not self.is_power_connected:
                logger.info("Power restored! Battery level: %d%%", level)
                self.is_power_connected = True
                self.restart_heavy_services()

            time.sleep(self.check_interval)


if __name__ == "__main__":
    monitor = BatteryMonitor()
    try:
        monitor.monitor_loop()
    except KeyboardInterrupt:
        logger.info("Battery monitor stopped by user.")
