"""
policy/engine.py
────────────────
The Policy Engine — the brain that turns a model prediction into a decision.

Pipeline position:
    Collector → Model → [ Policy Engine ] → Enforcer → SQLite action_log

Responsibilities:
  1. Whitelist check  — system-critical apps are NEVER throttled
  2. Shadow mode      — log decisions but call NO enforcer actions (safe default)
  3. Confidence gate  — only act when the model is sufficiently certain
     - Stricter threshold when there is little per-user history (cold-start)
     - Relaxed threshold once enough observations have been collected
  4. Correction layer — apply per-app user overrides before final decision
  5. Decision logging — every decision (throttle / allow / skip) goes to action_log
  6. Cooldown guard   — don't re-throttle an already-throttled app within N seconds

Decision outcomes:
  "allow"    → model says active_needed; do nothing
  "throttle" → model says idle/background with enough confidence; call enforcer
  "skip"     → confidence too low, whitelisted, or shadow mode — take no action
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Decision dataclass ────────────────────────────────────────────────────────

@dataclass
class Decision:
    """Result of the policy engine for one process snapshot."""
    app_name:    str
    pid:         int
    label:       str          # corrected model label
    confidence:  float        # corrected model confidence
    action:      str          # "allow" | "throttle" | "skip"
    reason:      str          # human-readable explanation for CLI
    timestamp:   int = field(default_factory=lambda: int(time.time()))
    shadow_mode: bool = False


# ── Policy Engine ─────────────────────────────────────────────────────────────

class PolicyEngine:
    """
    Stateful policy engine.  One instance per daemon run.

    Parameters
    ----------
    config   : the 'policy' section of config.yaml (dict)
    shadow   : global shadow_mode flag from config root
    db_conn  : open SQLite connection for action_log writes
    model    : PowerLayerModel instance (already loaded)
    corrector: CorrectionLayer instance
    """

    def __init__(
        self,
        config:    dict[str, Any],
        shadow:    bool,
        db_conn:   sqlite3.Connection,
        model:     Any,    # PowerLayerModel — avoid circular import
        corrector: Any,    # CorrectionLayer
    ) -> None:
        self._cfg       = config
        self._shadow    = shadow
        self._conn      = db_conn
        self._model     = model
        self._corrector = corrector

        # Whitelist: never throttle these
        self._whitelist: frozenset[str] = frozenset(
            config.get("whitelist", [])
        )

        # Confidence thresholds
        self._conf_threshold       = float(config.get("confidence_threshold", 0.90))
        self._fresh_conf_threshold = float(config.get("fresh_start_confidence_threshold", 0.97))
        self._min_obs              = int(config.get("min_observations_for_personalization", 20))

        # Cooldown: don't re-throttle same PID within N seconds
        self._cooldown_seconds = int(config.get("cooldown_seconds", 60))
        self._last_throttled:  dict[int, float] = {}   # pid → timestamp

        logger.info(
            "PolicyEngine ready. shadow=%s  whitelist=%d apps  "
            "conf_threshold=%.2f  fresh_threshold=%.2f",
            shadow, len(self._whitelist),
            self._conf_threshold, self._fresh_conf_threshold,
        )

    # ── Public interface ──────────────────────────────────────────────────────

    def decide(
        self,
        event:         dict[str, Any],
        cpu_history:   list[float] | None = None,
        history_count: int = 0,
    ) -> Decision:
        """
        Make and log a policy decision for one process event.

        Parameters
        ----------
        event         : collector event dict (app_name, pid, cpu_pct, …)
        cpu_history   : last ≤3 cpu_pct_relative values for trend feature
        history_count : total events seen for this app in the DB

        Returns
        -------
        Decision dataclass with action ∈ {"allow", "throttle", "skip"}
        """
        app  = str(event.get("app_name", "unknown"))
        pid  = int(event.get("pid", 0))

        # ── 1. Whitelist check ────────────────────────────────────────────────
        if app in self._whitelist:
            return self._make(app, pid, "idle_likely", 1.0, "skip",
                              f"whitelisted: {app} is always protected")

        # ── 2. Get model prediction ───────────────────────────────────────────
        raw_label, raw_conf = self._model.predict(
            event, cpu_history=cpu_history, history_count=history_count
        )

        # ── 3. Apply user correction ──────────────────────────────────────────
        label, conf = self._corrector.apply(app, raw_label, raw_conf)

        # ── 4. If model says active → always allow ────────────────────────────
        if label == "active_needed":
            return self._make(app, pid, label, conf, "allow",
                              f"model: active_needed  conf={conf:.2f}")

        # ── 5. Confidence gate ────────────────────────────────────────────────
        threshold = (
            self._conf_threshold
            if history_count >= self._min_obs
            else self._fresh_conf_threshold
        )
        if conf < threshold:
            return self._make(
                app, pid, label, conf, "skip",
                f"confidence too low ({conf:.2f} < {threshold:.2f}); "
                f"history={history_count}/{self._min_obs} obs"
            )

        # ── 6. Cooldown guard ─────────────────────────────────────────────────
        last = self._last_throttled.get(pid, 0.0)
        if time.time() - last < self._cooldown_seconds:
            return self._make(app, pid, label, conf, "skip",
                              f"cooldown: last throttled {int(time.time()-last)}s ago")

        # ── 7. Shadow mode ────────────────────────────────────────────────────
        if self._shadow:
            d = Decision(
                app_name=app, pid=pid,
                label=label, confidence=conf,
                action="skip",
                reason=f"shadow_mode: would throttle  label={label}  conf={conf:.2f}",
                shadow_mode=True,
            )
            # _log is called inside _make; build manually here to avoid double-write
            self._log(d)
            return d

        # ── 8. Throttle ───────────────────────────────────────────────────────
        self._last_throttled[pid] = time.time()
        return self._make(app, pid, label, conf, "throttle",
                          f"label={label}  conf={conf:.2f}  history={history_count}")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make(
        self,
        app:    str,
        pid:    int,
        label:  str,
        conf:   float,
        action: str,
        reason: str,
    ) -> Decision:
        """Create a Decision and log it to action_log."""
        d = Decision(
            app_name=app, pid=pid,
            label=label, confidence=conf,
            action=action, reason=reason,
            shadow_mode=self._shadow,
        )
        self._log(d)
        return d

    def _log(self, d: Decision) -> None:
        """Write a Decision to the action_log SQLite table."""
        try:
            self._conn.execute(
                """
                INSERT INTO action_log
                    (timestamp, app_name, pid, predicted_label,
                     confidence, action_taken, reason, shadow_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (d.timestamp, d.app_name, d.pid,
                 d.label, d.confidence,
                 d.action, d.reason,
                 int(d.shadow_mode)),
            )
            self._conn.commit()
        except Exception as exc:
            logger.warning("action_log write failed: %s", exc)

    # ── Bulk processing ───────────────────────────────────────────────────────

    def process_snapshot(
        self,
        events: list[dict[str, Any]],
        app_histories: dict[str, tuple[list[float], int]] | None = None,
    ) -> list[Decision]:
        """
        Process a full collector snapshot (list of process events).

        Parameters
        ----------
        events        : list of event dicts from the collector
        app_histories : optional dict of app_name → (cpu_history, history_count)
                        from the DB.  If None, all apps treated as cold-start.

        Returns
        -------
        List of Decision objects, one per event.
        """
        app_histories = app_histories or {}
        decisions = []
        throttle_count = 0

        for event in events:
            app = str(event.get("app_name", ""))
            history = app_histories.get(app, (None, 0))
            cpu_hist, hist_count = history

            d = self.decide(event, cpu_history=cpu_hist, history_count=hist_count)
            decisions.append(d)

            if d.action == "throttle":
                throttle_count += 1

        if events:
            logger.debug(
                "Snapshot: %d events → %d throttle / %d allow / %d skip",
                len(events),
                sum(1 for d in decisions if d.action == "throttle"),
                sum(1 for d in decisions if d.action == "allow"),
                sum(1 for d in decisions if d.action == "skip"),
            )

        return decisions

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def get_recent_decisions(self, limit: int = 20) -> list[dict]:
        """Return the most recent action_log entries (for CLI display)."""
        try:
            rows = self._conn.execute(
                """
                SELECT timestamp, app_name, pid, predicted_label,
                       confidence, action_taken, reason, shadow_mode
                FROM   action_log
                ORDER  BY timestamp DESC
                LIMIT  ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    "timestamp":   r[0],
                    "app_name":    r[1],
                    "pid":         r[2],
                    "label":       r[3],
                    "confidence":  r[4],
                    "action":      r[5],
                    "reason":      r[6],
                    "shadow_mode": bool(r[7]),
                }
                for r in rows
            ]
        except Exception as exc:
            logger.warning("action_log read failed: %s", exc)
            return []

    def stats(self) -> dict[str, int]:
        """Return summary counts from action_log."""
        try:
            row = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN action_taken='throttle' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN action_taken='allow'    THEN 1 ELSE 0 END),
                    SUM(CASE WHEN action_taken='skip'     THEN 1 ELSE 0 END),
                    SUM(CASE WHEN shadow_mode=1           THEN 1 ELSE 0 END)
                FROM action_log
                """
            ).fetchone()
            return {
                "total":    row[0] or 0,
                "throttle": row[1] or 0,
                "allow":    row[2] or 0,
                "skip":     row[3] or 0,
                "shadow":   row[4] or 0,
            }
        except Exception as exc:
            logger.warning("action_log stats failed: %s", exc)
            return {}
