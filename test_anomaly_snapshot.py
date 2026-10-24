"""Unit tests for anomaly_snapshot."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import anomaly_snapshot as mod
from anomaly_snapshot import Sample, should_trigger

NOW = 10_000.0
MIN = 60.0


class SnapshotRecorder:
    """Stand-in for the snapshot logger that records the messages."""

    def __init__(self) -> None:
        """Start with an empty list of messages."""
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        """Record one snapshot message.

        Args:
            message: Text that would be appended to the snapshot log.
        """
        self.messages.append(message)


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the state file to a temporary location.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The temporary state file path.
    """
    path = tmp_path / "state" / "state.json"
    monkeypatch.setattr(mod, "STATE_PATH", path)
    return path


def test_trigger_on_powered_drop() -> None:
    """A 3 point drop over 25 minutes with the port on triggers."""
    samples = [
        Sample(NOW - 25 * MIN, 70, True),
        Sample(NOW - 10 * MIN, 69, True),
        Sample(NOW, 67, True),
    ]
    assert should_trigger(samples, NOW) is True


def test_no_trigger_on_small_drop() -> None:
    """A 1 point drop stays below the threshold."""
    samples = [Sample(NOW - 25 * MIN, 70, True), Sample(NOW, 69, True)]
    assert should_trigger(samples, NOW) is False


def test_no_trigger_when_port_was_off() -> None:
    """A drop with the port off in the window is expected, not anomalous."""
    samples = [
        Sample(NOW - 25 * MIN, 70, True),
        Sample(NOW - 10 * MIN, 68, False),
        Sample(NOW, 66, True),
    ]
    assert should_trigger(samples, NOW) is False


def test_no_trigger_on_short_window() -> None:
    """A fast drop over less than the minimum window does not trigger."""
    samples = [Sample(NOW - 10 * MIN, 70, True), Sample(NOW, 66, True)]
    assert should_trigger(samples, NOW) is False


def test_no_trigger_with_too_few_samples() -> None:
    """Zero or one sample is never enough."""
    assert should_trigger([], NOW) is False
    assert should_trigger([Sample(NOW, 50, True)], NOW) is False


def test_old_unpowered_sample_is_ignored() -> None:
    """Samples older than the horizon do not affect the decision."""
    samples = [
        Sample(NOW - 60 * MIN, 90, False),
        Sample(NOW - 20 * MIN, 70, True),
        Sample(NOW, 68, True),
    ]
    assert should_trigger(samples, NOW) is True


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("List of devices attached\n4a64b8f0\tdevice\n", True),
        ("List of devices attached\n192.168.100.115:5555\tdevice\n", False),
        ("List of devices attached\n4a64b8f0\tunauthorized\n", False),
        (None, False),
    ],
)
def test_port_is_powered(
    monkeypatch: pytest.MonkeyPatch, output: str | None, expected: bool
) -> None:
    """The port counts as powered only if the USB serial is in state device.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        output: Simulated adb devices output.
        expected: Expected result.
    """
    monkeypatch.setattr(mod, "run_adb", lambda args, serial=None: output)
    assert mod.port_is_powered() is expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Current Battery Service state:\n  level: 57\n  scale: 100\n", 57),
        ("nothing useful here", None),
        (None, None),
    ],
)
def test_read_level(
    monkeypatch: pytest.MonkeyPatch, output: str | None, expected: int | None
) -> None:
    """The level is parsed from dumpsys output, or None if unreadable.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        output: Simulated dumpsys battery output.
        expected: Expected level.
    """
    monkeypatch.setattr(mod, "run_adb", lambda args, serial=None: output)
    assert mod.read_level() == expected


def test_state_round_trip(state_path: Path) -> None:
    """Saved samples and snapshot time are loaded back unchanged."""
    samples = [Sample(1.0, 50, True), Sample(2.0, 49, False)]
    mod.save_state(samples, 123.0)
    assert state_path.exists()
    assert mod.load_state() == (samples, 123.0)


@pytest.mark.usefixtures("state_path")
def test_load_state_missing_file() -> None:
    """A missing state file gives an empty state."""
    assert mod.load_state() == ([], 0.0)


def test_load_state_corrupt_file(state_path: Path) -> None:
    """A corrupt state file gives an empty state instead of crashing."""
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not json")
    assert mod.load_state() == ([], 0.0)


def _prepare_main(
    monkeypatch: pytest.MonkeyPatch,
    level: int | None,
    previous: list[Sample],
    last_snapshot: float,
) -> SnapshotRecorder:
    """Set up main() with simulated phone access.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        level: Level returned by the simulated read_level.
        previous: Samples stored before the run.
        last_snapshot: Stored time of the last snapshot.

    Returns:
        The recorder that collects saved snapshots.
    """
    recorder = SnapshotRecorder()
    mod.save_state(previous, last_snapshot)
    monkeypatch.setattr(mod, "read_level", lambda: level)
    monkeypatch.setattr(mod, "port_is_powered", lambda: True)
    monkeypatch.setattr(mod, "collect_snapshot", lambda lvl: "SNAP")
    monkeypatch.setattr(mod, "get_snapshot_logger", lambda: recorder)
    return recorder


@pytest.mark.usefixtures("state_path")
def test_main_saves_snapshot_on_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() saves one snapshot when the drain is abnormal."""
    now = time.time()
    recorder = _prepare_main(monkeypatch, 60, [Sample(now - 1400, 65, True)], 0.0)
    assert mod.main() == 0
    assert recorder.messages == ["SNAP"]
    samples, last_snapshot = mod.load_state()
    assert len(samples) == 2
    assert last_snapshot > 0.0


@pytest.mark.usefixtures("state_path")
def test_main_respects_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() does not save a second snapshot right after the first."""
    now = time.time()
    recorder = _prepare_main(monkeypatch, 60, [Sample(now - 1400, 65, True)], now - 60)
    assert mod.main() == 0
    assert recorder.messages == []


@pytest.mark.usefixtures("state_path")
def test_main_no_snapshot_on_small_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() stays quiet when the drop is below the threshold."""
    now = time.time()
    recorder = _prepare_main(monkeypatch, 64, [Sample(now - 1400, 65, True)], 0.0)
    assert mod.main() == 0
    assert recorder.messages == []


def test_main_unreadable_level(
    monkeypatch: pytest.MonkeyPatch, state_path: Path
) -> None:
    """main() skips the run when the level cannot be read."""
    monkeypatch.setattr(mod, "read_level", lambda: None)
    assert mod.main() == 0
    assert not state_path.exists()
