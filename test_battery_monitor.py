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
