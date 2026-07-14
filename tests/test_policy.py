"""
tests/test_policy.py
────────────────────
Tests for the PolicyEngine:
  - Whitelist bypass
  - Shadow mode (logs but never throttles)
  - Confidence gate (fresh start vs personalized threshold)
  - Cooldown guard (no re-throttle within window)
  - Correction layer integration (factor flips decision)
  - action_log writes
  - Snapshot batch processing
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from policy.engine import PolicyEngine, Decision
from model.features import LABEL_CLASSES


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with the action_log table."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE action_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       INTEGER NOT NULL,
            app_name        TEXT    NOT NULL,
            pid             INTEGER,
            predicted_label TEXT,
            confidence      REAL,
            action_taken    TEXT    NOT NULL,
            reason          TEXT,
            shadow_mode     INTEGER DEFAULT 1,
            battery_before  REAL,
            battery_after   REAL,
            reverted        INTEGER DEFAULT 0,
            enforcer_cmd    TEXT
        )
    """)
    conn.commit()
    return conn


def _make_model(label: str = "idle_likely", conf: float = 0.95) -> MagicMock:
    """Stub model that always returns a fixed (label, conf)."""
    model = MagicMock()
    model.predict.return_value = (label, conf)
    model.is_ready = True
    return model


def _make_corrector(factor: float = 1.0) -> MagicMock:
    """Stub corrector that passes through or applies a fixed factor."""
    corrector = MagicMock()
    def apply(app, label, conf):
        if factor == 1.0:
            return (label, conf)
        # Boost active_needed if factor > 1
        if factor > 1.0:
            return ("active_needed", min(conf * factor, 0.99))
        return ("background_unused", conf)
    corrector.apply.side_effect = apply
    return corrector


def _policy(
    shadow: bool = True,
    label: str = "idle_likely",
    conf: float = 0.95,
    factor: float = 1.0,
    extra_cfg: dict | None = None,
) -> tuple[PolicyEngine, sqlite3.Connection]:
    cfg = {
        "whitelist": ["systemd", "pipewire", "sshd"],
        "confidence_threshold": 0.90,
        "fresh_start_confidence_threshold": 0.97,
        "min_observations_for_personalization": 20,
        "cooldown_seconds": 60,
        **(extra_cfg or {}),
    }
    conn = _make_db()
    engine = PolicyEngine(
        config=cfg,
        shadow=shadow,
        db_conn=conn,
        model=_make_model(label, conf),
        corrector=_make_corrector(factor),
    )
    return engine, conn


def _event(app: str = "dropbox", pid: int = 1234) -> dict:
    return {
        "app_name": app, "pid": pid,
        "cpu_pct": 12.5, "battery_pct": 78.0,
        "time_since_last_foreground": 7200,
        "sync_freq_ratio": 1.2,
        "within_typical_active_hours": False,
        "cpu_pct_relative": 0.3,
        "app_category": "cloud_sync",
    }


# ── Test runner ───────────────────────────────────────────────────────────────

_results: list[tuple[str, bool, str]] = []


def test(name: str):
    def decorator(fn):
        try:
            fn()
            _results.append((name, True, ""))
            print(f"  ✓ {name}")
        except Exception as exc:
            _results.append((name, False, str(exc)))
            print(f"  ✗ {name}")
            print(f"      {exc}")
        return fn
    return decorator


# ── Whitelist tests ───────────────────────────────────────────────────────────

print("\n── Whitelist ────────────────────────────────────────")

@test("whitelist: systemd is never throttled")
def _():
    engine, _ = _policy(shadow=False, label="background_unused", conf=0.99)
    d = engine.decide(_event("systemd", 1))
    assert d.action == "skip", f"Expected skip, got {d.action}"
    assert "whitelisted" in d.reason


@test("whitelist: pipewire is never throttled")
def _():
    engine, _ = _policy(shadow=False, label="idle_likely", conf=0.99)
    d = engine.decide(_event("pipewire", 2))
    assert d.action == "skip"


@test("whitelist: non-whitelisted app is NOT protected")
def _():
    engine, _ = _policy(shadow=False, label="idle_likely", conf=0.99)
    d = engine.decide(_event("dropbox", 100), history_count=30)
    # Should throttle (not whitelisted, high conf, not shadow, enough history)
    assert d.action == "throttle", f"Expected throttle, got {d.action}"


# ── Shadow mode tests ─────────────────────────────────────────────────────────

print("\n── Shadow Mode ──────────────────────────────────────")

@test("shadow mode: action is always 'skip' even with high confidence")
def _():
    engine, _ = _policy(shadow=True, label="background_unused", conf=0.99)
    d = engine.decide(_event("dropbox", 100), history_count=50)
    assert d.action == "skip"
    assert d.shadow_mode is True


@test("shadow mode: decision is still logged to action_log")
def _():
    engine, conn = _policy(shadow=True, label="idle_likely", conf=0.98)
    engine.decide(_event("dropbox", 200), history_count=50)
    count = conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
    assert count == 1, f"Expected 1 log entry, got {count}"


@test("shadow=False: throttle action IS returned")
def _():
    engine, _ = _policy(shadow=False, label="idle_likely", conf=0.99)
    d = engine.decide(_event("dropbox", 300), history_count=30)
    assert d.action == "throttle"
    assert d.shadow_mode is False


# ── Confidence gate tests ─────────────────────────────────────────────────────

print("\n── Confidence Gate ──────────────────────────────────")

@test("fresh-start: conf=0.93 below fresh threshold (0.97) → skip")
def _():
    engine, _ = _policy(shadow=False, label="idle_likely", conf=0.93)
    # history_count=5 < min_observations=20 → fresh threshold applies
    d = engine.decide(_event("dropbox", 400), history_count=5)
    assert d.action == "skip", f"Expected skip, got {d.action}"
    assert "confidence too low" in d.reason


@test("fresh-start: conf=0.98 above fresh threshold → throttle")
def _():
    engine, _ = _policy(shadow=False, label="idle_likely", conf=0.98)
    d = engine.decide(_event("dropbox", 401), history_count=5)
    assert d.action == "throttle", f"Expected throttle, got {d.action}"


@test("personalized: conf=0.91 above relaxed threshold (0.90) → throttle")
def _():
    engine, _ = _policy(shadow=False, label="idle_likely", conf=0.91)
    # history_count=25 >= 20 → relaxed threshold applies
    d = engine.decide(_event("dropbox", 402), history_count=25)
    assert d.action == "throttle", f"Expected throttle, got {d.action}"


@test("personalized: conf=0.88 below relaxed threshold → skip")
def _():
    engine, _ = _policy(shadow=False, label="idle_likely", conf=0.88)
    d = engine.decide(_event("dropbox", 403), history_count=25)
    assert d.action == "skip"


# ── Active bypass ─────────────────────────────────────────────────────────────

print("\n── Active Bypass ────────────────────────────────────")

@test("active_needed: always allowed, never throttled")
def _():
    engine, _ = _policy(shadow=False, label="active_needed", conf=0.99)
    d = engine.decide(_event("firefox", 500), history_count=100)
    assert d.action == "allow", f"Expected allow, got {d.action}"


@test("active_needed: allowed even in shadow mode")
def _():
    engine, _ = _policy(shadow=True, label="active_needed", conf=0.99)
    d = engine.decide(_event("firefox", 501), history_count=0)
    assert d.action == "allow"


# ── Cooldown guard tests ──────────────────────────────────────────────────────

print("\n── Cooldown Guard ───────────────────────────────────")

@test("cooldown: second throttle on same PID within window → skip")
def _():
    engine, _ = _policy(
        shadow=False, label="idle_likely", conf=0.99,
        extra_cfg={"cooldown_seconds": 120}
    )
    pid = 600
    d1 = engine.decide(_event("dropbox", pid), history_count=30)
    assert d1.action == "throttle"
    d2 = engine.decide(_event("dropbox", pid), history_count=30)
    assert d2.action == "skip", f"Expected skip on cooldown, got {d2.action}"
    assert "cooldown" in d2.reason


@test("cooldown: different PID throttles independently")
def _():
    engine, _ = _policy(shadow=False, label="idle_likely", conf=0.99)
    engine.decide(_event("dropbox", 700), history_count=30)  # throttle PID 700
    d = engine.decide(_event("dropbox", 701), history_count=30)  # different PID
    assert d.action == "throttle", f"Expected throttle for different PID, got {d.action}"


# ── Correction layer integration ──────────────────────────────────────────────

print("\n── Correction Integration ───────────────────────────")

@test("correction: high factor flips idle→active, prevents throttle")
def _():
    # factor=3 → corrector returns active_needed
    engine, _ = _policy(shadow=False, label="idle_likely", conf=0.92, factor=3.0)
    d = engine.decide(_event("spotify", 800), history_count=30)
    assert d.action == "allow", f"Expected allow after correction, got {d.action}"


# ── action_log tests ──────────────────────────────────────────────────────────

print("\n── Action Log ───────────────────────────────────────")

@test("action_log: every decision is written")
def _():
    engine, conn = _policy(shadow=False, label="background_unused", conf=0.99)
    for i in range(5):
        engine.decide(_event("dropbox", 900 + i), history_count=30)
    count = conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
    assert count == 5, f"Expected 5 entries, got {count}"


@test("action_log: throttle row has correct action_taken column")
def _():
    engine, conn = _policy(shadow=False, label="background_unused", conf=0.99)
    d = engine.decide(_event("updater", 950), history_count=30)
    assert d.action == "throttle"
    row = conn.execute(
        "SELECT action_taken FROM action_log WHERE app_name='updater'"
    ).fetchone()
    assert row and row[0] == "throttle"


@test("get_recent_decisions: returns list of dicts")
def _():
    engine, _ = _policy(shadow=True, label="idle_likely", conf=0.95)
    for i in range(3):
        engine.decide(_event("app", 1000 + i), history_count=30)
    recent = engine.get_recent_decisions(limit=10)
    assert len(recent) == 3
    assert all("action" in r for r in recent)


@test("stats: returns correct throttle/allow/skip counts")
def _():
    engine, _ = _policy(shadow=False, label="background_unused", conf=0.99)
    # PID 1100 throttles, then cooldown skip on second call
    engine.decide(_event("dropbox", 1100), history_count=30)
    engine.decide(_event("dropbox", 1100), history_count=30)  # cooldown
    # Different PID, active → allow
    engine2, _ = _policy(shadow=False, label="active_needed", conf=0.95)
    engine2.decide(_event("firefox", 1200), history_count=30)

    s = engine.stats()
    assert s["throttle"] == 1
    assert s["skip"] >= 1


# ── Snapshot batch processing ─────────────────────────────────────────────────

print("\n── Snapshot Batch ───────────────────────────────────")

@test("process_snapshot: returns one Decision per event")
def _():
    engine, _ = _policy(shadow=True, label="idle_likely", conf=0.92)
    events = [_event(f"app{i}", 2000 + i) for i in range(5)]
    decisions = engine.process_snapshot(events)
    assert len(decisions) == 5
    assert all(isinstance(d, Decision) for d in decisions)


@test("process_snapshot: whitelisted apps get skip even in batch")
def _():
    engine, _ = _policy(shadow=False, label="idle_likely", conf=0.99)
    events = [
        _event("systemd", 3000),
        _event("dropbox", 3001),
    ]
    decisions = engine.process_snapshot(events, app_histories={"dropbox": (None, 30)})
    actions = {d.app_name: d.action for d in decisions}
    assert actions["systemd"] == "skip"
    assert actions["dropbox"] == "throttle"


# ── Summary ───────────────────────────────────────────────────────────────────

passed = sum(1 for _, ok, _ in _results if ok)
failed = sum(1 for _, ok, _ in _results if not ok)

print(f"\n{'═'*43}")
print(f"  Results: {passed} passed, {failed} failed")
print(f"{'═'*43}\n")

if failed:
    sys.exit(1)
