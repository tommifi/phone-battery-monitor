"""Tests for zuk_current_log."""

from pathlib import Path

import zuk_current_log as zcl

BATTERY_TEXT = "Current Battery Service state:\r\n  status: 3\r\n  level: 71\r\n  Charge counter: 2516730\r\n"


def test_parse_battery() -> None:
    """Counter, level and status are extracted."""
    parsed = zcl.parse_battery(BATTERY_TEXT.replace("\r", ""))
    assert parsed == {"counter": 2516730, "level": 71, "status": 3}


def test_parse_healthd_seconds() -> None:
    """healthd CPU time is converted from HH:MM:SS to seconds."""
    text = "  PID     TIME NAME\n  565 01:55:55 healthd\n"
    assert zcl.parse_healthd_seconds(text) == 6955
    assert zcl.parse_healthd_seconds("1 00:00:01 init\n") is None


def test_parse_uptime_seconds() -> None:
    """/proc/uptime is parsed to whole seconds."""
    assert zcl.parse_uptime_seconds("1234.56 999.0\n") == 1234
    assert zcl.parse_uptime_seconds("") is None


def test_average_ma_matches_manual_measurement() -> None:
    """The measured 13711 uAh drop in 121 s is about 408 mA."""
    value = zcl.average_ma(2308766, 0.0, 2295055, 121.0)
    assert value is not None
    assert abs(value - 407.9) < 0.1
    assert zcl.average_ma(1, 10.0, 1, 10.0) is None


def test_append_rows_compute_average_from_previous(tmp_path: Path) -> None:
    """The second row carries the average against the first."""
    csv_path = tmp_path / "log.csv"
    first = zcl.build_row(1000.0, {"counter": 1000000, "level": 80}, 10, 100, None)
    zcl.append_row(csv_path, first)
    previous = zcl.read_previous(csv_path)
    assert previous == (1000.0, 1000000)
    second = zcl.build_row(1600.0, {"counter": 990000}, None, None, previous)
    zcl.append_row(csv_path, second)
    lines = csv_path.read_text().splitlines()
    assert lines[0].startswith("epoch_s,")
    assert len(lines) == 3
    assert lines[2].split(",")[-1] == "60.0"
