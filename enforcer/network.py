"""
enforcer/network.py
───────────────────
Network throttle enforcer using Linux tc (traffic control).

Strategy:
  We use tc's Token Bucket Filter (TBF) qdisc to rate-limit outbound traffic
  on a per-cgroup basis. Two-step approach:

  Step 1 — Mark packets from the powerlayer cgroup using net_cls:
    Each process in our cgroup gets a classid written to net_cls.classid.
    iptables/tc can then match on this class ID.

  Step 2 — tc filter: apply a rate-limit to that class:
    tc qdisc add dev <iface> root handle 1: htb default 10
    tc class add dev <iface> parent 1: classid 1:10 htb rate <full_rate>
    tc class add dev <iface> parent 1: classid 1:20 htb rate <throttle_rate>
    tc filter add dev <iface> parent 1: handle 0x1 cgroup

Cross-distro notes:
  - tc is part of iproute2, available on all major Linux distros.
  - If tc is not available, network throttling is silently skipped.
  - The net_cls cgroup controller is optional — if it's not in
    /sys/fs/cgroup/cgroup.controllers, we fall back to interface-level
    TBF (less precise: limits ALL outbound traffic on the interface).
  - All commands are built as strings and returned for logging to
    action_log.enforcer_cmd before being executed, so CLI `explain`
    can show exactly what ran.

Shadow mode:
  When shadow_mode=True, commands are built and logged but NOT executed.
  This is the safe default and matches the policy engine's shadow mode.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_DEFAULT_RATE    = "500kbit"     # outbound rate when throttled
_BURST           = "16kb"        # token bucket burst size
_LATENCY         = "50ms"        # max latency before packets are dropped
_HANDLE_ROOT     = "1:"
_CLASS_NORMAL    = "1:10"
_CLASS_THROTTLE  = "1:20"


# ── Availability check ────────────────────────────────────────────────────────

def _tc_available() -> bool:
    return shutil.which("tc") is not None


def _net_cls_available() -> bool:
    """Check if net_cls cgroup controller is available."""
    try:
        controllers = Path("/sys/fs/cgroup/cgroup.controllers").read_text()
        return "net_cls" in controllers
    except Exception:
        return False


# ── Network Enforcer ──────────────────────────────────────────────────────────

class NetworkEnforcer:
    """
    Rate-limits outbound network traffic for throttled processes.

    Parameters
    ----------
    config : the full config dict (reads 'enforcer' and 'collector' keys)
    """

    def __init__(self, config: dict[str, Any]) -> None:
        ecfg = config.get("enforcer", config)
        ccfg = config.get("collector", {})

        self._rate:        str        = ecfg.get("network_throttle_rate", _DEFAULT_RATE)
        self._iface:       str | None = None   # resolved at setup()
        self._iface_cfg:   str        = ccfg.get("network_interface", "auto")
        self._ready:       bool       = False
        self._shadow:      bool       = config.get("shadow_mode", True)
        self._net_cls:     bool       = False
        self._throttled:   set[int]   = set()   # PIDs currently rate-limited

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def setup(self, iface: str | None = None) -> bool:
        """
        Resolve network interface and validate tc availability.
        Returns True if network throttling is operational.
        """
        if not _tc_available():
            logger.warning("tc not found — network throttling disabled.")
            return False

        # Resolve interface
        self._iface = iface or self._iface_cfg
        if self._iface == "auto":
            self._iface = _detect_default_iface()
        if not self._iface:
            logger.warning("No active network interface detected — network throttling disabled.")
            return False

        self._net_cls = _net_cls_available()
        if not self._net_cls:
            logger.info(
                "net_cls cgroup controller not available. "
                "Network throttling will use interface-level TBF (less precise)."
            )

        # Set up HTB root qdisc on the interface (idempotent)
        ok = self._setup_qdisc()
        self._ready = ok
        return ok

    def teardown(self) -> None:
        """Remove the HTB qdisc from the interface."""
        if not self._ready or not self._iface:
            return
        cmd = f"tc qdisc del dev {self._iface} root"
        self._run(cmd, ignore_error=True)
        self._ready = False
        logger.info("Network qdisc removed from %s", self._iface)

    # ── Throttle / release ────────────────────────────────────────────────────

    def throttle(self, pid: int, app_name: str = "") -> str | None:
        """
        Apply rate-limit to outbound traffic from this PID.

        Returns the tc command string for logging, or None if skipped.
        """
        if not self._ready:
            return None

        if self._net_cls:
            return self._throttle_net_cls(pid, app_name)
        else:
            return self._throttle_tbf(app_name)

    def release(self, pid: int, app_name: str = "") -> bool:
        """Remove rate-limit for this PID."""
        if not self._ready or pid not in self._throttled:
            return False

        self._throttled.discard(pid)

        if self._net_cls:
            # Restore PID's classid to 0 (unclassified = unlimited)
            self._write_net_cls_classid(pid, 0)

        # If no more throttled PIDs, tear down the restricting class
        if not self._throttled:
            cmd = (
                f"tc class change dev {self._iface} parent {_HANDLE_ROOT} "
                f"classid {_CLASS_THROTTLE} htb rate 1gbit"
            )
            self._run(cmd)
            logger.info("Network released for %s (no more throttled PIDs)", app_name)

        return True

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _setup_qdisc(self) -> bool:
        """Create HTB root qdisc with a normal (full-speed) and throttle class."""
        # Delete any existing root qdisc first (idempotent)
        self._run(f"tc qdisc del dev {self._iface} root", ignore_error=True)

        cmds = [
            # Root HTB qdisc
            f"tc qdisc add dev {self._iface} root handle {_HANDLE_ROOT} htb default 10",
            # Default class: full rate (1gbit = effectively unlimited)
            f"tc class add dev {self._iface} parent {_HANDLE_ROOT} classid {_CLASS_NORMAL} htb rate 1gbit",
            # Throttle class: restricted rate
            f"tc class add dev {self._iface} parent {_HANDLE_ROOT} classid {_CLASS_THROTTLE} htb rate {self._rate}",
        ]
        for cmd in cmds:
            ok = self._run(cmd)
            if not ok:
                logger.warning("qdisc setup failed at: %s", cmd)
                return False
        logger.info("Network qdisc ready on %s (throttle rate=%s)", self._iface, self._rate)
        return True

    def _throttle_net_cls(self, pid: int, app_name: str) -> str | None:
        """Throttle via net_cls cgroup classid assignment."""
        classid = int(_CLASS_THROTTLE.replace(":", ""), 16)
        ok = self._write_net_cls_classid(pid, classid)
        if ok:
            self._throttled.add(pid)
            cmd = f"net_cls.classid ← {classid:#010x}  (pid={pid})"
            logger.info("Network throttled PID %d (%s) via net_cls", pid, app_name)
            return cmd
        return None

    def _throttle_tbf(self, app_name: str) -> str | None:
        """
        Fallback: TBF on the entire interface.
        Less precise — limits all outbound traffic, not just the target PID.
        Suitable when net_cls is unavailable.
        """
        cmd = (
            f"tc qdisc replace dev {self._iface} root tbf "
            f"rate {self._rate} burst {_BURST} latency {_LATENCY}"
        )
        ok = self._run(cmd)
        if ok:
            logger.info(
                "Network throttled (interface-level TBF) on %s "
                "rate=%s [imprecise: affects all traffic]",
                self._iface, self._rate
            )
            return cmd
        return None

    def _write_net_cls_classid(self, pid: int, classid: int) -> bool:
        """Write net_cls classid for the process's cgroup."""
        cls_path = Path(f"/sys/fs/cgroup/net_cls/{pid}")
        try:
            if cls_path.exists():
                (cls_path / "net_cls.classid").write_text(str(classid))
                return True
        except Exception as exc:
            logger.debug("net_cls write failed for PID %d: %s", pid, exc)
        return False

    def _run(self, cmd: str, ignore_error: bool = False) -> bool:
        """
        Execute a tc command.
        In shadow mode: log but do NOT execute.
        Returns True on success (or if shadow mode).
        """
        if self._shadow:
            logger.info("[SHADOW] tc: %s", cmd)
            return True
        try:
            result = subprocess.run(
                cmd.split(),
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0 and not ignore_error:
                logger.warning("tc failed: %s\n  stderr: %s", cmd, result.stderr.strip())
                return False
            return True
        except Exception as exc:
            if not ignore_error:
                logger.warning("tc error: %s — %s", cmd, exc)
            return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    def status(self) -> dict:
        return {
            "ready":          self._ready,
            "interface":      self._iface,
            "rate":           self._rate,
            "net_cls":        self._net_cls,
            "throttled_pids": list(self._throttled),
            "shadow_mode":    self._shadow,
        }


# ── Utilities ─────────────────────────────────────────────────────────────────

def _detect_default_iface() -> str | None:
    """
    Find the default route's network interface by reading /proc/net/route.
    More reliable than parsing `ip route` output.
    Returns None if no default route found.
    """
    try:
        with open("/proc/net/route") as f:
            next(f)   # skip header
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "00000000":  # destination = 0.0.0.0
                    return parts[0]   # interface name
    except Exception:
        pass
    # Fallback: scan /sys/class/net for any UP non-loopback interface
    try:
        for iface in sorted(Path("/sys/class/net").iterdir()):
            if iface.name == "lo":
                continue
            operstate = iface / "operstate"
            if operstate.exists() and operstate.read_text().strip() == "up":
                return iface.name
    except Exception:
        pass
    return None
