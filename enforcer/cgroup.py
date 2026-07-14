"""
enforcer/cgroup.py
──────────────────
CPU throttle enforcer using Linux cgroups v2.

How it works:
  When the policy engine decides to throttle a process, this module:
    1. Ensures the powerlayer cgroup subtree exists
    2. Moves the target PID into that cgroup (writes to cgroup.procs)
    3. Lowers cpu.weight from 100 (default) to the configured value (e.g. 50)
       → The kernel's CFS scheduler gives the process proportionally less CPU time

  When a process is un-throttled (allowed again):
    1. Move its PID back to the root/user cgroup
    2. Optionally reset cpu.weight to 100

Init system detection:
  - systemd: /run/systemd/system exists → use the Delegate=yes path
    The daemon's own service unit must have Delegate=yes so that it owns
    /sys/fs/cgroup/<our-slice>/ and can write there without root.
  - Non-systemd (OpenRC, Runit, s6): no /run/systemd/system
    Fall back to writing directly to /sys/fs/cgroup/powerlayer/
    This requires either: (a) the script runs as root, (b) a NOPASSWD
    sudo rule, or (c) a small setuid helper binary.
    In production, document this clearly. For now, attempt the write and
    log a warning if it fails — always safe-fail (never crash).

cgroup.procs vs cgroup.threads:
  We write to cgroup.procs (process-level) not cgroup.threads.
  This moves all threads of the process together, which is correct for
  whole-application throttling.

Design constraint:
  All file writes are non-blocking. We catch ALL exceptions and log them.
  The enforcer NEVER raises to the caller — a failed throttle is always
  logged and silently skipped, not crashed.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

_CGROUP_ROOT          = Path("/sys/fs/cgroup")
_POWERLAYER_CGROUP    = _CGROUP_ROOT / "powerlayer"
_DEFAULT_CPU_WEIGHT   = 100    # kernel default
_THROTTLED_CPU_WEIGHT = 50     # half share when throttled


# ── Init system detection ─────────────────────────────────────────────────────

def _is_systemd() -> bool:
    """Return True if systemd is the running init system."""
    return Path("/run/systemd/system").exists()


def _find_user_cgroup() -> Path | None:
    """
    Locate the user's own systemd cgroup slice so we can re-parent PIDs
    back there when releasing them.  Reads /proc/self/cgroup.
    """
    try:
        text = Path("/proc/self/cgroup").read_text()
        for line in text.splitlines():
            # cgroup v2 has a single line: "0::/user.slice/..."
            if line.startswith("0::"):
                return _CGROUP_ROOT / line.split("::", 1)[1].lstrip("/")
    except Exception:
        pass
    return None


# ── Cgroup setup ──────────────────────────────────────────────────────────────

def ensure_cgroup(cgroup_path: Path | None = None) -> Path | None:
    """
    Create the powerlayer cgroup and enable the cpu controller on it.

    Returns the path if successful, None if creation failed (non-fatal).
    """
    target = cgroup_path or _POWERLAYER_CGROUP
    try:
        target.mkdir(parents=True, exist_ok=True)

        # Enable cpu controller if not already enabled
        subtree = target.parent / "cgroup.subtree_control"
        if subtree.exists():
            current = subtree.read_text().strip()
            if "cpu" not in current:
                subtree.write_text("+cpu\n")

        logger.info("Cgroup ready: %s", target)
        return target

    except PermissionError:
        if _is_systemd():
            logger.warning(
                "Cannot write to %s — the systemd service unit needs "
                "Delegate=yes in [Service] to get cgroup ownership. "
                "CPU throttling disabled until service is deployed.",
                target,
            )
        else:
            logger.warning(
                "Cannot write to %s — non-systemd init detected. "
                "Either run as root, add a NOPASSWD sudo rule for "
                "/sys/fs/cgroup/powerlayer/, or use a setuid helper. "
                "CPU throttling disabled.",
                target,
            )
        return None
    except Exception as exc:
        logger.error("Cgroup setup failed: %s", exc)
        return None


# ── Core enforcer ─────────────────────────────────────────────────────────────

class CgroupEnforcer:
    """
    CPU throttle enforcer.  Lifecycle:
      1. Call setup() once at daemon startup.
      2. Call throttle(pid) when policy engine decides to throttle.
      3. Call release(pid) when the app becomes active again.
      4. Call teardown() at shutdown to move all PIDs back and remove cgroup.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        cfg = config.get("enforcer", config)
        raw_path = cfg.get("cgroup_root", str(_POWERLAYER_CGROUP))
        self._cgroup_path: Path      = Path(raw_path)
        self._cpu_weight:  int       = int(cfg.get("cpu_throttle_weight", _THROTTLED_CPU_WEIGHT))
        self._ready:       bool      = False
        self._throttled:   set[int]  = set()      # PIDs currently throttled
        self._user_cgroup: Path | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def setup(self) -> bool:
        """
        Create cgroup and validate write access.
        Returns True if the enforcer is operational, False if it must be
        skipped (permission denied — always safe, never crashes).
        """
        self._user_cgroup = _find_user_cgroup()
        result = ensure_cgroup(self._cgroup_path)
        self._ready = result is not None
        if self._ready:
            # Pre-set cpu.weight on the cgroup
            self._write_cpu_weight(_DEFAULT_CPU_WEIGHT)
        return self._ready

    def teardown(self) -> None:
        """Release all throttled PIDs and attempt to clean up the cgroup."""
        for pid in list(self._throttled):
            self.release(pid)
        # Remove cgroup (only works if it's empty)
        try:
            self._cgroup_path.rmdir()
            logger.info("Cgroup removed: %s", self._cgroup_path)
        except Exception:
            pass   # non-fatal — might have procs still in it

    # ── Throttle / release ────────────────────────────────────────────────────

    def throttle(self, pid: int, app_name: str = "") -> str | None:
        """
        Move PID into powerlayer cgroup and lower its cpu.weight.

        Returns the enforcer command string (for action_log.enforcer_cmd),
        or None if the throttle could not be applied.
        """
        if not self._ready:
            logger.debug("CgroupEnforcer not ready — skipping throttle for PID %d", pid)
            return None

        if not _pid_exists(pid):
            logger.debug("PID %d no longer exists — skipping", pid)
            return None

        cmd = f"cgroup.procs ← {pid}  cpu.weight ← {self._cpu_weight}"
        try:
            # 1. Move PID into our cgroup
            (self._cgroup_path / "cgroup.procs").write_text(str(pid))
            # 2. Lower CPU weight
            self._write_cpu_weight(self._cpu_weight)
            self._throttled.add(pid)
            logger.info("Throttled PID %d (%s)  cpu.weight=%d", pid, app_name, self._cpu_weight)
            return cmd
        except ProcessLookupError:
            logger.debug("PID %d vanished before throttle completed", pid)
            return None
        except PermissionError as exc:
            logger.warning("Throttle PID %d failed (permission): %s", pid, exc)
            return None
        except Exception as exc:
            logger.warning("Throttle PID %d failed: %s", pid, exc)
            return None

    def release(self, pid: int, app_name: str = "") -> bool:
        """
        Move PID back to user cgroup and restore default cpu.weight.
        Returns True if successful.
        """
        if not self._ready or pid not in self._throttled:
            return False

        if not _pid_exists(pid):
            self._throttled.discard(pid)
            return False

        try:
            # Move back to user's own cgroup (or root cgroup as fallback)
            dest = self._user_cgroup or (_CGROUP_ROOT / "cgroup.procs")
            if dest.is_dir():
                dest = dest / "cgroup.procs"
            dest.write_text(str(pid))
            self._throttled.discard(pid)
            # Restore cpu.weight to default if no more throttled PIDs
            if not self._throttled:
                self._write_cpu_weight(_DEFAULT_CPU_WEIGHT)
            logger.info("Released PID %d (%s)", pid, app_name)
            return True
        except Exception as exc:
            logger.warning("Release PID %d failed: %s", pid, exc)
            self._throttled.discard(pid)
            return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _write_cpu_weight(self, weight: int) -> None:
        """Write cpu.weight to the powerlayer cgroup file."""
        try:
            (self._cgroup_path / "cpu.weight").write_text(str(weight))
        except Exception as exc:
            logger.debug("cpu.weight write failed: %s", exc)

    @property
    def throttled_pids(self) -> frozenset[int]:
        return frozenset(self._throttled)

    @property
    def is_ready(self) -> bool:
        return self._ready

    def status(self) -> dict:
        return {
            "ready":          self._ready,
            "cgroup_path":    str(self._cgroup_path),
            "throttled_pids": list(self._throttled),
            "cpu_weight":     self._cpu_weight,
            "systemd":        _is_systemd(),
        }


# ── Utilities ─────────────────────────────────────────────────────────────────

def _pid_exists(pid: int) -> bool:
    """Check if a PID is still alive without sending a signal."""
    return Path(f"/proc/{pid}").exists()


def build_systemd_unit_snippet() -> str:
    """
    Return the [Service] snippet that must be added to powerlayer.service
    so that systemd delegates cgroup ownership to our daemon process.
    This allows the daemon to write to /sys/fs/cgroup/powerlayer/ without root.
    """
    return """\
[Service]
# cgroup v2 delegation — lets PowerLayer write to its own cgroup subtree
# without requiring root privileges.
Delegate=yes
DelegateSubgroup=powerlayer

# Restrict what the service can do even with cgroup access
ProtectSystem=strict
PrivateTmp=yes
NoNewPrivileges=yes
"""
