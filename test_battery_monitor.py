"""Unit tests for the battery_monitor module using pytest."""

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
