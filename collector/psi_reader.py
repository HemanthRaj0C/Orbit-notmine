"""
collector/psi_reader.py
───────────────────────
Read Linux Pressure Stall Information (PSI) from cgroup v2 sysfs files.

PSI is available on Linux 4.20+ with CONFIG_PSI=y.
cgroup v2 PSI files live at:
    /sys/fs/cgroup/cpu.pressure
    /sys/fs/cgroup/memory.pressure
    /sys/fs/cgroup/io.pressure

File format (one example line):
    some avg10=0.12 avg60=0.05 avg300=0.01 total=123456789
    full avg10=0.00 avg60=0.00 avg300=0.00 total=0

"some" = at least one task stalled
"full" = all tasks stalled (more severe)

We read only "some" lines for the collector; "full" is logged but not used
for feature engineering in Phase 1.

Graceful degradation: if PSI files are missing (VM, older kernel, or
a distro that compiled without PSI), all functions return None — the
collector continues normally without PSI data.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_CPU_PRESSURE_PATH    = _CGROUP_ROOT / "cpu.pressure"
_MEMORY_PRESSURE_PATH = _CGROUP_ROOT / "memory.pressure"
_IO_PRESSURE_PATH     = _CGROUP_ROOT / "io.pressure"

# Regex to parse one PSI line
_PSI_LINE_RE = re.compile(
    r"(?P<type>some|full)\s+"
    r"avg10=(?P<avg10>[\d.]+)\s+"
    r"avg60=(?P<avg60>[\d.]+)\s+"
    r"avg300=(?P<avg300>[\d.]+)\s+"
    r"total=(?P<total>\d+)"
)

# Warn only once per missing file (avoids log spam on VMs)
_warned: set[str] = set()


# ── Core parser ───────────────────────────────────────────────────────────────

def _parse_psi_file(path: Path) -> dict[str, dict[str, float]] | None:
    """
    Parse a PSI file into:
        {
          "some": {"avg10": 0.12, "avg60": 0.05, "avg300": 0.01, "total": 123456789},
          "full": {"avg10": 0.00, "avg60": 0.00, "avg300": 0.00, "total": 0},
        }
    Returns None if the file doesn't exist or can't be read.
    """
    if not path.exists():
        if str(path) not in _warned:
            logger.warning(
                "PSI file not found: %s — PSI metrics will be unavailable. "
                "(Normal on VMs or kernels compiled without CONFIG_PSI.)", path
            )
            _warned.add(str(path))
        return None

    try:
        content = path.read_text()
    except PermissionError:
        if str(path) not in _warned:
            logger.warning("Cannot read PSI file (permission denied): %s", path)
            _warned.add(str(path))
        return None
    except OSError as exc:
        logger.debug("PSI read error %s: %s", path, exc)
        return None

    result: dict[str, dict[str, float]] = {}
    for line in content.strip().splitlines():
        m = _PSI_LINE_RE.match(line.strip())
        if m:
            result[m.group("type")] = {
                "avg10":  float(m.group("avg10")),
                "avg60":  float(m.group("avg60")),
                "avg300": float(m.group("avg300")),
                "total":  float(m.group("total")),
            }
    return result if result else None


# ── Public API ────────────────────────────────────────────────────────────────

def read_cpu_pressure() -> dict[str, Any] | None:
    """
    Return CPU pressure stall information.

    Returns dict with keys: avg10, avg60, avg300, total  (from "some" line)
    or None if PSI is unavailable.
    """
    data = _parse_psi_file(_CPU_PRESSURE_PATH)
    if data is None:
        return None
    return data.get("some")


def read_memory_pressure() -> dict[str, Any] | None:
    """Return memory pressure stall information (from "some" line)."""
    data = _parse_psi_file(_MEMORY_PRESSURE_PATH)
    if data is None:
        return None
    return data.get("some")


def read_io_pressure() -> dict[str, Any] | None:
    """Return I/O pressure stall information (from "some" line)."""
    data = _parse_psi_file(_IO_PRESSURE_PATH)
    if data is None:
        return None
    return data.get("some")


def read_all_pressure() -> dict[str, dict[str, Any] | None]:
    """
    Convenience function: return all three PSI readings in one call.

    Returns:
        {
          "cpu":    {"avg10": ..., "avg60": ..., "avg300": ..., "total": ...} | None,
          "memory": {...} | None,
          "io":     {...} | None,
        }
    """
    return {
        "cpu":    read_cpu_pressure(),
        "memory": read_memory_pressure(),
        "io":     read_io_pressure(),
    }


def psi_available() -> bool:
    """Return True if at least cpu.pressure is readable on this system."""
    return _CPU_PRESSURE_PATH.exists() and _CPU_PRESSURE_PATH.is_file()


def check_cgroup_v2() -> bool:
    """
    Verify that the cgroup v2 unified hierarchy is mounted.
    Checks for 'cgroup2' filesystem type via /proc/mounts.
    Works on all distros (no assumption about mount point).
    """
    try:
        mounts = Path("/proc/mounts").read_text()
        for line in mounts.splitlines():
            parts = line.split()
            # fields: device mountpoint fstype options dump pass
            if len(parts) >= 3 and parts[2] == "cgroup2":
                logger.debug("cgroup v2 mounted at: %s", parts[1])
                return True
    except OSError as exc:
        logger.error("Cannot read /proc/mounts: %s", exc)
    return False
