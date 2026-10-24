#!/usr/bin/env python3
"""Capture a process snapshot of the phone when its battery drains abnormally.

Run periodically (systemd timer). The script keeps a small state file with the
latest battery samples. When the level falls by DROP_THRESHOLD_PCT or more over
about WINDOW_MINUTES while the USB port stays powered, it appends a snapshot
(top CPU processes, foreground app, screen state, wake locks) to a rotating log.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger("anomaly_snapshot")

ADB_PATH = "/usr/bin/adb"
WIFI_SERIAL = "192.168.100.115:5555"
USB_SERIAL = "4a64b8f0"
STATE_PATH = Path.home() / ".local/state/zuk-anomaly/state.json"
SNAPSHOT_PATH = Path.home() / "zuk_anomaly_snapshots.log"
ADB_TIMEOUT_SECONDS = 20
WINDOW_MINUTES = 20
SAMPLE_SLACK_MINUTES = 5
DROP_THRESHOLD_PCT = 2
COOLDOWN_MINUTES = 15
MAX_SAMPLES = 8
TOP_PROCESSES = 15
SNAPSHOT_MAX_BYTES = 2_000_000
SNAPSHOT_BACKUPS = 5
LEVEL_RE = re.compile(r"^\s*level:\s*(\d+)", re.MULTILINE)
SNAPSHOT_COMMANDS: list[tuple[str, str]] = [
    (
        "battery",
        "dumpsys battery | grep -E 'level|temperature|Charge counter|USB powered|status'",
    ),
    ("screen", "dumpsys power | grep -E 'mWakefulness=|Display Power'"),
    ("foreground app", "dumpsys window | grep mCurrentFocus"),
    ("wake locks", "dumpsys power | grep -i 'wake lock'"),
    ("cpu", f"dumpsys cpuinfo | head -{TOP_PROCESSES + 2}"),
]


@dataclass(frozen=True)
class Sample:
    """One battery observation taken by a single run of the script."""

    t: float
    level: int
    powered: bool


def run_adb(args: list[str], serial: str | None = WIFI_SERIAL) -> str | None:
    """Run an adb command and return its stdout, or None on failure.

    Args:
        args: Arguments passed to adb after the optional serial selector.
        serial: Device serial to target, or None for global commands.

    Returns:
        Standard output of the command, or None if it failed or timed out.
    """
    cmd = [ADB_PATH]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=ADB_TIMEOUT_SECONDS,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as err:
        logger.warning("ADB command failed (%s): %s", " ".join(cmd), err)
        return None
    return result.stdout


def read_level() -> int | None:
    """Read the battery level in percent through ADB over Wi-Fi.

    Returns:
        The level, or None if it cannot be read.
    """
    output = run_adb(["shell", "dumpsys", "battery"])
    if output is None:
        return None
    match = LEVEL_RE.search(output)
    return int(match.group(1)) if match else None


def port_is_powered() -> bool:
    """Tell whether the phone is visible on USB, i.e. the Pi port is on.

    Returns:
        True if the USB serial is listed by adb in the device state.
    """
    output = run_adb(["devices"], serial=None)
    if output is None:
        return False
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == USB_SERIAL and parts[1] == "device":
            return True
    return False


def load_state() -> tuple[list[Sample], float]:
    """Load the stored samples and the time of the last snapshot.

    Returns:
        A tuple with the samples and the last snapshot timestamp (0.0 if none).
    """
    try:
        raw = json.loads(STATE_PATH.read_text())
        samples = [
            Sample(float(s["t"]), int(s["level"]), bool(s["powered"]))
            for s in raw["samples"]
        ]
        return samples, float(raw.get("last_snapshot", 0.0))
    except (OSError, ValueError, KeyError, TypeError):
        logger.info("No usable state file, starting fresh.")
        return [], 0.0


def save_state(samples: list[Sample], last_snapshot: float) -> None:
    """Persist the samples and the last snapshot time.

    Args:
        samples: Recent battery samples to keep.
        last_snapshot: Timestamp of the last saved snapshot.
    """
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "samples": [asdict(s) for s in samples],
        "last_snapshot": last_snapshot,
    }
    STATE_PATH.write_text(json.dumps(payload))


def should_trigger(samples: list[Sample], now: float) -> bool:
    """Decide whether the recent samples show an abnormal drain.

    The level must fall by DROP_THRESHOLD_PCT or more over at least
    WINDOW_MINUTES while every sample in the window had a powered port.

    Args:
        samples: Recent samples, oldest first.
        now: Current timestamp in seconds.

    Returns:
        True if a snapshot should be taken.
    """
    horizon = (WINDOW_MINUTES + SAMPLE_SLACK_MINUTES) * 60
    recent = [s for s in samples if now - s.t <= horizon]
    if len(recent) < 2:
        return False
    first, last = recent[0], recent[-1]
    if last.t - first.t < WINDOW_MINUTES * 60:
        return False
    if not all(s.powered for s in recent):
        return False
    return first.level - last.level >= DROP_THRESHOLD_PCT


def collect_snapshot(level: int) -> str:
    """Collect process and power information from the phone.

    Args:
        level: Current battery level, written in the header.

    Returns:
        A multi-line text report.
    """
    lines = [f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} level={level}% ==="]
    for title, command in SNAPSHOT_COMMANDS:
        output = run_adb(["shell", command])
        lines.append(f"--- {title} ---")
        lines.append(output.strip() if output else "(unavailable)")
    return "\n".join(lines)


def get_snapshot_logger() -> logging.Logger:
    """Return the logger that writes snapshots to a rotating file.

    Returns:
        A logger bound to SNAPSHOT_PATH with size-based rotation.
    """
    snap = logging.getLogger("anomaly_snapshot.file")
    if not snap.handlers:
        handler = RotatingFileHandler(
            SNAPSHOT_PATH, maxBytes=SNAPSHOT_MAX_BYTES, backupCount=SNAPSHOT_BACKUPS
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        snap.addHandler(handler)
        snap.setLevel(logging.INFO)
        snap.propagate = False
    return snap


def main() -> int:
    """Run one sampling cycle and save a snapshot if the drain is abnormal.

    Returns:
        Process exit code (always 0, an unreachable phone is not an error).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )
    level = read_level()
    if level is None:
        logger.warning("Battery level unreadable, skipping this run.")
        return 0
    now = time.time()
    samples, last_snapshot = load_state()
    samples.append(Sample(now, level, port_is_powered()))
    samples = samples[-MAX_SAMPLES:]
    cooled_down = now - last_snapshot >= COOLDOWN_MINUTES * 60
    if cooled_down and should_trigger(samples, now):
        get_snapshot_logger().info(collect_snapshot(level))
        logger.warning("Abnormal drain at %d%%, snapshot saved.", level)
        last_snapshot = now
    save_state(samples, last_snapshot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
