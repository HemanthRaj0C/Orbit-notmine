"""
collector/proc_reader.py
────────────────────────
Snapshot per-process resource usage using psutil.
Cross-distro: no apt/dnf assumptions; xprintidle is optional.

Key design choices:
- psutil.process_iter() with explicit `attrs` list avoids re-fetching
  the same data twice (each attr fetched once per process).
- cpu_percent() with interval=None uses delta from last call — must be
  called in a loop (first call always returns 0.0; that's fine here
  because the monitor calls this every poll cycle).
- Foreground detection tries multiple strategies in order:
    1. `xprintidle` / `ydotool` (X11 / Wayland)
    2. /proc/<pid>/status + /proc/<PID>/wchan heuristic
    3. Falls back to "all processes are background" (safest default)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Processes we never want to report as "foreground" resource hogs
_KERNEL_PROCS = frozenset({"kthreadd", "ksoftirqd", "migration", "rcu_sched",
                            "rcu_bh", "watchdog", "kworker", "kdevtmpfs"})

# How long (ms) of idle time counts as "user is away from keyboard"
_IDLE_THRESHOLD_MS = 30_000   # 30 seconds

# Module-level cache: xprintidle / ydotool availability detected once
_xprintidle_available: bool | None = None
_ydotool_available:  bool | None = None


# ── Idle / foreground detection ───────────────────────────────────────────────

def _detect_idle_tool() -> None:
    """Probe for xprintidle (X11) and ydotool (Wayland) once at startup."""
    global _xprintidle_available, _ydotool_available
    _xprintidle_available = shutil.which("xprintidle") is not None
    _ydotool_available    = shutil.which("ydotool") is not None
    if _xprintidle_available:
        logger.debug("Idle detection: xprintidle found (X11 mode).")
    elif _ydotool_available:
        logger.debug("Idle detection: ydotool found (Wayland mode).")
    else:
        logger.debug("Idle detection: no idle tool found; using proc-based fallback.")


def get_idle_ms() -> int:
    """
    Return the number of milliseconds since the last user input event.
    Returns 0 if detection is unavailable (assume user is active — safe default).
    """
    global _xprintidle_available, _ydotool_available
    if _xprintidle_available is None:
        _detect_idle_tool()

    # Strategy 1: xprintidle (X11)
    if _xprintidle_available:
        try:
            out = subprocess.check_output(
                ["xprintidle"], timeout=1, stderr=subprocess.DEVNULL
            )
            return int(out.strip())
        except Exception:
            _xprintidle_available = False   # disable for future calls

    # Strategy 2: ydotool (Wayland) — not all versions support this
    if _ydotool_available:
        try:
            out = subprocess.check_output(
                ["ydotool", "mousemove", "--", "0", "0"],
                timeout=1, stderr=subprocess.DEVNULL
            )
            # ydotool doesn't give idle ms directly; treat success as "active"
            return 0
        except Exception:
            _ydotool_available = False

    # Strategy 3: Check /sys/class/backlight — if brightness changed recently
    # Rough heuristic: if file mtime is within threshold, user is active
    backlight_dirs = list(Path("/sys/class/backlight").glob("*"))
    if backlight_dirs:
        mtime = backlight_dirs[0].stat().st_mtime
        age_ms = int((time.time() - mtime) * 1000)
        if age_ms < _IDLE_THRESHOLD_MS:
            return age_ms

    # Fallback: return 0 (assume active — never wrongly throttle due to bad detection)
    return 0


def is_user_active() -> bool:
    """True if the user has interacted with the system recently."""
    return get_idle_ms() < _IDLE_THRESHOLD_MS


def get_active_window_pid() -> int | None:
    """
    Return the PID of the process that owns the currently focused window.
    Tries xdotool (X11), then wmctrl, then returns None.
    """
    # xdotool getactivewindow getwindowpid
    if shutil.which("xdotool"):
        try:
            win_id = subprocess.check_output(
                ["xdotool", "getactivewindow"], timeout=1, stderr=subprocess.DEVNULL
            ).strip()
            pid_out = subprocess.check_output(
                ["xdotool", "getwindowpid", win_id], timeout=1, stderr=subprocess.DEVNULL
            )
            return int(pid_out.strip())
        except Exception:
            pass

    # wmctrl -lp lists windows with PIDs
    if shutil.which("wmctrl"):
        try:
            lines = subprocess.check_output(
                ["wmctrl", "-lp"], timeout=1, stderr=subprocess.DEVNULL
            ).decode().splitlines()
            # first column = window id, third = pid
            # We can't know which is "active" without more context; skip
        except Exception:
            pass

    return None


# ── Process snapshot ──────────────────────────────────────────────────────────

_PSUTIL_ATTRS = [
    "pid", "name", "status",
    "cpu_percent",          # delta since last call — always call every cycle
    "num_threads",
    "io_counters",
]


def snapshot_processes(
    foreground_pid: int | None = None,
    min_cpu_pct: float = 0.0,
) -> list[dict[str, Any]]:
    """
    Return a list of per-process snapshots for all non-kernel processes.

    Each dict has:
        pid          : int
        name         : str
        status       : str   (psutil status string)
        cpu_pct      : float (% of one CPU core)
        num_threads  : int
        io_read_bytes: int   (cumulative, or 0 if unavailable)
        io_write_bytes: int
        net_bytes    : int   (io_read + io_write delta this cycle — approximation)
        is_foreground: bool  (True if pid matches the active window pid)
        event_type   : str   ('foreground' | 'idle')
    """
    results: list[dict[str, Any]] = []

    for proc in psutil.process_iter(_PSUTIL_ATTRS):
        try:
            info = proc.info
            name: str = info.get("name") or ""

            if name in _KERNEL_PROCS or not name:
                continue

            pid: int = info["pid"]
            cpu: float = info.get("cpu_percent") or 0.0

            # Skip processes with no CPU activity unless they are foreground
            if cpu < min_cpu_pct and pid != foreground_pid:
                continue

            io = info.get("io_counters")
            io_read  = io.read_bytes  if io else 0
            io_write = io.write_bytes if io else 0

            is_fg = (pid == foreground_pid)
            event_type = "foreground" if is_fg else "idle"

            results.append({
                "pid":            pid,
                "name":           name,
                "status":         info.get("status", "unknown"),
                "cpu_pct":        round(cpu, 2),
                "num_threads":    info.get("num_threads") or 1,
                "io_read_bytes":  io_read,
                "io_write_bytes": io_write,
                "net_bytes":      io_read + io_write,   # combined IO as net proxy
                "is_foreground":  is_fg,
                "event_type":     event_type,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    return results


def get_system_cpu_pct() -> float:
    """System-wide CPU % (non-blocking, uses delta from last call)."""
    return psutil.cpu_percent(interval=None)


def get_system_memory() -> dict[str, int]:
    """Return virtual memory stats in bytes."""
    mem = psutil.virtual_memory()
    return {"total": mem.total, "available": mem.available, "used": mem.used}
