"""
enforcer/__init__.py
────────────────────
Unified enforcer facade.

The PolicyEngine calls enforce(decision) once per throttle decision.
This module coordinates the two sub-enforcers:
  - CgroupEnforcer  → CPU weight via /sys/fs/cgroup/powerlayer/
  - NetworkEnforcer → Outbound rate via tc

Both enforcers are individually optional — if either fails setup (e.g.
no cgroup write access, no tc installed), the other still operates.
Failures are always logged, never raised.

Usage:
    from enforcer import Enforcer

    enf = Enforcer(config)
    enf.setup()
    enf.enforce(decision)   # called by PolicyEngine on throttle decisions
    enf.release(pid)        # called when policy flips back to active_needed
    enf.teardown()          # called on daemon shutdown
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from enforcer.cgroup  import CgroupEnforcer
from enforcer.network import NetworkEnforcer

if TYPE_CHECKING:
    from policy.engine import Decision

logger = logging.getLogger(__name__)


class Enforcer:
    """
    Top-level enforcer. Coordinates CPU + Network throttling.

    Parameters
    ----------
    config : full config dict from config.yaml
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config  = config
        self._shadow  = config.get("shadow_mode", True)
        self._cpu     = CgroupEnforcer(config)
        self._net     = NetworkEnforcer(config)
        self._ready   = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def setup(self, net_iface: str | None = None) -> dict[str, bool]:
        """
        Initialise both sub-enforcers.

        Returns
        -------
        dict with 'cpu_ready' and 'net_ready' booleans.
        """
        cpu_ok = self._cpu.setup()
        net_ok = self._net.setup(iface=net_iface)
        self._ready = True   # facade is always "ready" even if subs failed

        if self._shadow:
            logger.info(
                "Enforcer running in SHADOW MODE — decisions are logged "
                "but no throttling actions are executed."
            )
        else:
            logger.info(
                "Enforcer active. CPU throttle=%s  Network throttle=%s",
                "✓" if cpu_ok else "✗ (skipped)",
                "✓" if net_ok else "✗ (skipped)",
            )
        return {"cpu_ready": cpu_ok, "net_ready": net_ok}

    def teardown(self) -> None:
        """Release all throttled resources and clean up."""
        self._cpu.teardown()
        self._net.teardown()
        self._ready = False
        logger.info("Enforcer teardown complete.")

    # ── Core interface ────────────────────────────────────────────────────────

    def enforce(self, decision: "Decision") -> str | None:
        """
        Execute a throttle decision from the PolicyEngine.

        Only called when decision.action == "throttle" and shadow_mode is False.
        Returns a combined enforcer_cmd string for the action_log, or None.

        Parameters
        ----------
        decision : Decision from PolicyEngine with action="throttle"
        """
        if self._shadow:
            logger.debug(
                "[SHADOW] Would throttle PID %d (%s)", decision.pid, decision.app_name
            )
            return f"[shadow] throttle pid={decision.pid} app={decision.app_name}"

        if not self._ready:
            logger.warning("Enforcer not set up — call setup() first.")
            return None

        parts: list[str] = []

        # CPU throttle
        cpu_cmd = self._cpu.throttle(decision.pid, decision.app_name)
        if cpu_cmd:
            parts.append(f"cpu:{cpu_cmd}")

        # Network throttle
        net_cmd = self._net.throttle(decision.pid, decision.app_name)
        if net_cmd:
            parts.append(f"net:{net_cmd}")

        enforcer_cmd = " | ".join(parts) if parts else None
        if not parts:
            logger.debug(
                "Throttle decision for PID %d — both enforcers skipped (no write access).",
                decision.pid,
            )
        return enforcer_cmd

    def release(self, pid: int, app_name: str = "") -> None:
        """
        Release throttle on a PID (called when policy flips to active_needed).
        """
        if self._shadow:
            logger.debug("[SHADOW] Would release PID %d", pid)
            return
        self._cpu.release(pid, app_name)
        self._net.release(pid, app_name)

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Return status of both sub-enforcers (for CLI `status` command)."""
        return {
            "shadow_mode": self._shadow,
            "cpu":         self._cpu.status(),
            "network":     self._net.status(),
        }
