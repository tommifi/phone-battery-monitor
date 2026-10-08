"""Unit tests for the battery_monitor module using pytest."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from battery_monitor import BatteryMonitor


@pytest.fixture
def monitor() -> BatteryMonitor:
    """Fixture providing a default BatteryMonitor instance.

    Returns:
        BatteryMonitor configured with default settings.
    """
    return BatteryMonitor()


def test_is_ac_or_usb_powered_true(monitor: BatteryMonitor) -> None:
    """Test battery power detection when USB powered is true."""
    mock_output = "Current Battery Service state:\n  AC powered: false\n  USB powered: true\n"
    with patch.object(monitor, "_run_adb_cmd", return_value=mock_output):
        assert monitor.is_ac_or_usb_powered() is True


def test_is_ac_or_usb_powered_false(monitor: BatteryMonitor) -> None:
    """Test battery power detection when both AC and USB are false."""
    mock_output = "Current Battery Service state:\n  AC powered: false\n  USB powered: false\n"
    with patch.object(monitor, "_run_adb_cmd", return_value=mock_output):
        assert monitor.is_ac_or_usb_powered() is False


def test_get_battery_level_success(monitor: BatteryMonitor) -> None:
    """Test parsing battery percentage level from ADB output."""
    mock_output = "Current Battery Service state:\n  level: 85\n"
    with patch.object(monitor, "_run_adb_cmd", return_value=mock_output):
        assert monitor.get_battery_level() == 85


def test_stop_heavy_services_executes_commands(monitor: BatteryMonitor) -> None:
    """Test that stop_heavy_services issues force-stop ADB commands."""
    with patch.object(monitor, "_run_adb_cmd") as mock_adb:
        monitor.stop_heavy_services()
        assert mock_adb.call_count >= len(monitor.heavy_services) + 1


def test_systemd_service_file_validity() -> None:
    """Validate that the systemd service file exists and contains mandatory directives."""
    service_path = Path.home() / ".config/systemd/user/battery-monitor.service"
    assert service_path.is_file(), "Systemd service file does not exist."

    content = service_path.read_text(encoding="utf-8")
    assert "[Unit]" in content
    assert "[Service]" in content
    assert "ExecStart=" in content
    assert "Restart=always" in content


# ---------------------------------------------------------------------------
# Charge control tests
# ---------------------------------------------------------------------------


def test_invalid_thresholds_raise() -> None:
    """Stop level must be greater than resume level."""
    with pytest.raises(ValueError):
        BatteryMonitor(stop_level=40, resume_level=80)


def test_charge_stops_at_stop_level(monitor: BatteryMonitor) -> None:
    """The port is switched off when the level reaches the stop level."""
    with patch.object(monitor, "set_charging") as mock_set:
        monitor._update_charge_state(80)
        mock_set.assert_called_once_with(False)


def test_hysteresis_no_change_between_levels(monitor: BatteryMonitor) -> None:
    """Between resume and stop level nothing changes, in either state."""
    with patch.object(monitor, "set_charging") as mock_set:
        monitor.charging_enabled = True
        monitor._update_charge_state(60)
        monitor.charging_enabled = False
        monitor._update_charge_state(60)
        mock_set.assert_not_called()


def test_charge_resumes_at_resume_level(monitor: BatteryMonitor) -> None:
    """The port is switched on when the level drops to the resume level."""
    monitor.charging_enabled = False
    with patch.object(monitor, "set_charging") as mock_set:
        monitor._update_charge_state(40)
        mock_set.assert_called_once_with(True)


def test_critical_level_forces_resume() -> None:
    """A resume level below the critical level is raised to the critical one."""
    mon = BatteryMonitor(stop_level=80, resume_level=5)
    mon.charging_enabled = False
    with patch.object(mon, "set_charging") as mock_set:
        mon._update_charge_state(15)
        mock_set.assert_called_once_with(True)


def test_failsafe_after_repeated_read_failures(monitor: BatteryMonitor) -> None:
    """Three failed reads with the port off switch the port back on."""
    monitor.charging_enabled = False
    with patch.object(monitor, "set_charging") as mock_set:
        for _ in range(3):
            monitor._handle_read_failure()
        mock_set.assert_called_once_with(True)


def test_no_failsafe_when_port_already_on(monitor: BatteryMonitor) -> None:
    """Read failures do not toggle the port when it is already on."""
    with patch.object(monitor, "set_charging") as mock_set:
        for _ in range(5):
            monitor._handle_read_failure()
        mock_set.assert_not_called()


def test_no_power_loss_check_when_port_off(monitor: BatteryMonitor) -> None:
    """A port switched off by us is not reported as power loss."""
    monitor.charging_enabled = False
    with patch.object(monitor, "stop_heavy_services") as mock_stop:
        monitor._check_power_loss(70)
        mock_stop.assert_not_called()


def test_grace_period_after_resume(monitor: BatteryMonitor) -> None:
    """Power loss is ignored during the grace cycles after switching on."""
    monitor.grace_cycles = 2
    with patch.object(monitor, "is_ac_or_usb_powered", return_value=False), patch.object(
        monitor, "stop_heavy_services"
    ) as mock_stop:
        monitor._check_power_loss(50)
        monitor._check_power_loss(50)
        mock_stop.assert_not_called()
        monitor._check_power_loss(50)
        mock_stop.assert_called_once()


def test_set_charging_failure_keeps_state(monitor: BatteryMonitor) -> None:
    """A failed uhubctl call does not change the tracked state."""
    with patch("battery_monitor.subprocess.run", side_effect=OSError("boom")):
        assert monitor.set_charging(False) is False
    assert monitor.charging_enabled is True


def test_switch_failure_alert_sent_once(monitor: BatteryMonitor) -> None:
    """One alert is sent after repeated failures, not one per attempt."""
    err = subprocess.CalledProcessError(1, "sudo", stderr="a password is required")
    with patch("battery_monitor.subprocess.run", side_effect=err), patch(
        "battery_monitor.send_telegram_message", return_value=True
    ) as mock_tg:
        for _ in range(6):
            assert monitor.set_charging(True) is False
        mock_tg.assert_called_once()


def test_alert_retried_if_telegram_fails(monitor: BatteryMonitor) -> None:
    """If the alert could not be delivered, the next failure retries it."""
    err = subprocess.CalledProcessError(1, "sudo", stderr="boom")
    with patch("battery_monitor.subprocess.run", side_effect=err), patch(
        "battery_monitor.send_telegram_message", return_value=False
    ) as mock_tg:
        for _ in range(5):
            monitor.set_charging(True)
        assert mock_tg.call_count == 3


def test_switch_success_resets_failures(monitor: BatteryMonitor) -> None:
    """A successful switch clears the failure counter and the alert flag."""
    monitor.switch_failures = 2
    monitor.alert_sent = True
    with patch("battery_monitor.subprocess.run"):
        assert monitor.set_charging(False) is True
    assert monitor.switch_failures == 0
    assert monitor.alert_sent is False


def test_low_level_alert_sent_once(monitor: BatteryMonitor) -> None:
    """One alert is sent while the level stays low with the port off."""
    monitor.charging_enabled = False
    with patch("battery_monitor.send_telegram_message", return_value=True) as mock_tg:
        for _ in range(3):
            monitor._check_low_level(20)
        mock_tg.assert_called_once()


def test_low_level_no_alert_when_port_on(monitor: BatteryMonitor) -> None:
    """No alert is sent while the port is on, even at a low level."""
    with patch("battery_monitor.send_telegram_message") as mock_tg:
        monitor._check_low_level(10)
        mock_tg.assert_not_called()


def test_low_level_no_alert_above_threshold(monitor: BatteryMonitor) -> None:
    """No alert is sent above the threshold with the port off."""
    monitor.charging_enabled = False
    with patch("battery_monitor.send_telegram_message") as mock_tg:
        monitor._check_low_level(30)
        mock_tg.assert_not_called()


def test_low_level_alert_flag_resets_when_port_on(monitor: BatteryMonitor) -> None:
    """The alert flag is cleared once charging is enabled again."""
    monitor.low_alert_sent = True
    monitor._check_low_level(50)
    assert monitor.low_alert_sent is False


# ---------------------------------------------------------------------------
# ADB over Wi-Fi recovery tests
# ---------------------------------------------------------------------------

DEVICES_BOTH = "List of devices attached\n192.168.100.115:5555\tdevice\n4a64b8f0\tdevice"


def test_usb_serial_picks_usb_device(monitor: BatteryMonitor) -> None:
    """The USB serial is returned when a ready USB device is listed."""
    with patch.object(monitor, "_run_adb_global", return_value=DEVICES_BOTH):
        assert monitor._usb_serial() == "4a64b8f0"


def test_usb_serial_none_when_only_wifi(monitor: BatteryMonitor) -> None:
    """A Wi-Fi entry is not a USB device."""
    out = "List of devices attached\n192.168.100.115:5555\tdevice"
    with patch.object(monitor, "_run_adb_global", return_value=out):
        assert monitor._usb_serial() is None


def test_usb_serial_ignores_offline_device(monitor: BatteryMonitor) -> None:
    """A USB device that is not in the "device" state is ignored."""
    out = "List of devices attached\n4a64b8f0\toffline"
    with patch.object(monitor, "_run_adb_global", return_value=out):
        assert monitor._usb_serial() is None


def test_restore_wifi_adb_plain_reconnect(monitor: BatteryMonitor) -> None:
    """If a plain reconnect works, tcpip is not used."""
    with patch.object(monitor, "_run_adb_global") as mock_global, patch.object(
        monitor, "_wifi_adb_reachable", return_value=True
    ):
        assert monitor._restore_wifi_adb() is True
        mock_global.assert_called_once_with(["connect", monitor.device])


def test_restore_wifi_adb_uses_tcpip_via_usb(monitor: BatteryMonitor) -> None:
    """If the reconnect fails, tcpip is sent through the USB device."""
    with patch.object(monitor, "_run_adb_global") as mock_global, patch.object(
        monitor, "_wifi_adb_reachable", side_effect=[False, True]
    ), patch.object(monitor, "_usb_serial", return_value="4a64b8f0"), patch(
        "battery_monitor.time.sleep"
    ):
        assert monitor._restore_wifi_adb() is True
        calls = [c.args[0] for c in mock_global.call_args_list]
        assert ["-s", "4a64b8f0", "tcpip", "5555"] in calls


def test_restore_wifi_adb_no_usb_device(monitor: BatteryMonitor) -> None:
    """Without a USB device nothing more is tried."""
    with patch.object(monitor, "_run_adb_global") as mock_global, patch.object(
        monitor, "_wifi_adb_reachable", return_value=False
    ), patch.object(monitor, "_usb_serial", return_value=None):
        assert monitor._restore_wifi_adb() is False
        mock_global.assert_called_once_with(["connect", monitor.device])


def test_recover_skips_below_threshold(monitor: BatteryMonitor) -> None:
    """No recovery attempt is made after a single failed read."""
    monitor.read_failures = 1
    with patch.object(monitor, "_restore_wifi_adb") as mock_restore:
        monitor._recover_wifi_adb()
        mock_restore.assert_not_called()


def test_recover_runs_at_threshold(monitor: BatteryMonitor) -> None:
    """Recovery is attempted once the failure threshold is reached."""
    monitor.read_failures = 2
    with patch.object(monitor, "_restore_wifi_adb") as mock_restore:
        monitor._recover_wifi_adb()
        mock_restore.assert_called_once()


def test_adb_down_alert_sent_once(monitor: BatteryMonitor) -> None:
    """One alert is sent while ADB over Wi-Fi stays down."""
    monitor.read_failures = 10
    with patch.object(monitor, "_restore_wifi_adb"), patch(
        "battery_monitor.send_telegram_message", return_value=True
    ) as mock_tg:
        for _ in range(3):
            monitor._recover_wifi_adb()
        mock_tg.assert_called_once()
    assert monitor.adb_alert_sent is True
