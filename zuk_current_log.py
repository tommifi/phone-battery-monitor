"""Sample the ZUK battery charge counter and append the average current to a CSV.

One run takes one sample. The average current is computed against the previous
CSV row, so a 10 minute timer gives a 10 minute average. Positive mA means the
battery is discharging.
"""

from __future__ import annotations

import csv
import logging
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("zuk_current_log")

ADB_PATH = Path("/usr/bin/adb")
DEVICE = "192.168.100.115:5555"
CSV_PATH = Path.home() / "zuk_current_log.csv"
ADB_TIMEOUT_SECONDS = 20
HEADER = [
    "epoch_s",
    "time",
    "charge_counter_uah",
    "level",
    "status",
    "healthd_cpu_s",
    "phone_uptime_s",
    "avg_ma",
]


def run_adb_shell(args: list[str]) -> str | None:
    """Run an adb shell command on the phone.

    Args:
        args: Command and arguments to run inside the phone shell.

    Returns:
        Stdout without carriage returns, or None if the command failed.
    """
    cmd = [str(ADB_PATH), "-s", DEVICE, "shell", *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=ADB_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as err:
        logger.error("adb command failed (%s): %s", " ".join(args), err)
        return None
    return result.stdout.replace("\r", "")


def parse_battery(text: str) -> dict[str, int]:
    """Extract charge counter, level and status from dumpsys battery output.

    Args:
        text: Output of ``dumpsys battery``.

    Returns:
        Dict with the keys found among ``counter``, ``level`` and ``status``.
    """
    wanted = {"Charge counter": "counter", "level": "level", "status": "status"}
    found: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(":")
        name = wanted.get(key)
        if name is None or name in found:
            continue
        try:
            found[name] = int(value.strip())
        except ValueError:
            logger.warning("Unparseable value for %s: %r", key, value)
    return found


def parse_healthd_seconds(text: str) -> int | None:
    """Extract the cumulative CPU time of healthd from ps output.

    Args:
        text: Output of ``ps -A -o PID,TIME,NAME``.

    Returns:
        CPU seconds, or None if healthd is missing or the time is malformed.
    """
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1] == "healthd":
            try:
                seconds = 0
                for chunk in parts[1].split(":"):
                    seconds = seconds * 60 + int(chunk)
            except ValueError:
                return None
            return seconds
    return None


def parse_uptime_seconds(text: str) -> int | None:
    """Parse the phone uptime from /proc/uptime.

    Args:
        text: Content of ``/proc/uptime``.

    Returns:
        Uptime in whole seconds, or None if malformed.
    """
    try:
        return int(float(text.split()[0]))
    except (IndexError, ValueError):
        return None


def average_ma(
    prev_counter: int, prev_epoch: float, counter: int, epoch: float
) -> float | None:
    """Compute the average current between two charge counter samples.

    Args:
        prev_counter: Previous charge counter in microampere-hours.
        prev_epoch: Previous sample time in epoch seconds.
        counter: Current charge counter in microampere-hours.
        epoch: Current sample time in epoch seconds.

    Returns:
        Average current in mA (positive = discharging), or None if the
        time difference is not positive.
    """
    elapsed = epoch - prev_epoch
    if elapsed <= 0:
        return None
    return (prev_counter - counter) * 3.6 / elapsed


def read_previous(csv_path: Path) -> tuple[float, int] | None:
    """Read epoch and charge counter from the last CSV row.

    Args:
        csv_path: Path of the CSV file.

    Returns:
        Tuple (epoch_s, charge_counter_uah), or None if unavailable.
    """
    if not csv_path.exists():
        return None
    rows = list(csv.reader(csv_path.read_text().splitlines()))
    if len(rows) < 2:
        return None
    try:
        return float(rows[-1][0]), int(rows[-1][2])
    except (IndexError, ValueError):
        logger.warning("Last CSV row is malformed; skipping the average.")
        return None


def build_row(
    epoch: float,
    battery: dict[str, int],
    healthd_s: int | None,
    uptime_s: int | None,
    previous: tuple[float, int] | None,
) -> list[str]:
    """Build one CSV row from a sample.

    Args:
        epoch: Sample time in epoch seconds.
        battery: Parsed dumpsys battery values (must contain ``counter``).
        healthd_s: Cumulative healthd CPU seconds, if known.
        uptime_s: Phone uptime in seconds, if known.
        previous: Previous (epoch, counter) pair, if any.

    Returns:
        List of strings matching HEADER.
    """
    avg = None
    if previous is not None:
        avg = average_ma(previous[1], previous[0], battery["counter"], epoch)

    def fmt(value: float | None, digits: int = 0) -> str:
        """Format an optional number, empty string when missing."""
        return "" if value is None else f"{value:.{digits}f}"

    return [
        f"{epoch:.0f}",
        datetime.fromtimestamp(epoch, tz=UTC)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S"),
        str(battery["counter"]),
        fmt(battery.get("level")),
        fmt(battery.get("status")),
        fmt(healthd_s),
        fmt(uptime_s),
        fmt(avg, 1),
    ]


def append_row(csv_path: Path, row: list[str]) -> None:
    """Append a row to the CSV, writing the header on first use.

    Args:
        csv_path: Path of the CSV file.
        row: Row matching HEADER.
    """
    is_new = not csv_path.exists()
    with csv_path.open("a", newline="") as handle:
        writer = csv.writer(handle)
        if is_new:
            writer.writerow(HEADER)
        writer.writerow(row)


def main() -> int:
    """Take one sample and append it to the CSV.

    Returns:
        0 on success, 1 if the battery could not be read.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    text = run_adb_shell(["dumpsys", "battery"])
    battery = parse_battery(text) if text else {}
    if "counter" not in battery:
        logger.error("Charge counter unavailable; no row written.")
        return 1
    ps_text = run_adb_shell(["ps", "-A", "-o", "PID,TIME,NAME"])
    uptime_text = run_adb_shell(["cat", "/proc/uptime"])
    row = build_row(
        time.time(),
        battery,
        parse_healthd_seconds(ps_text) if ps_text else None,
        parse_uptime_seconds(uptime_text) if uptime_text else None,
        read_previous(CSV_PATH),
    )
    append_row(CSV_PATH, row)
    logger.info("Sample written: %s", ",".join(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
