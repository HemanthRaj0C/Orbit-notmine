"""
tests/test_cli.py
─────────────────
Tests for the CLI layer.

All tests use an in-memory or temp SQLite DB — no real DB required.
Tests verify argument parsing, command routing, and output correctness.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from cli import build_parser, main
from cli.commands import cmd_status, cmd_explain, cmd_override, cmd_report
from cli.display  import bar, fmt_age, fmt_time, fmt_datetime


# ── DB fixture ────────────────────────────────────────────────────────────────

def _make_db(tmp_dir: str) -> Path:
    """Create a minimal test database with schema + sample data."""
    db_path = Path(tmp_dir) / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            app_name TEXT NOT NULL,
            pid INTEGER,
            cpu_pct REAL DEFAULT 0,
            net_bytes INTEGER DEFAULT 0,
            battery_pct REAL,
            event_type TEXT DEFAULT 'snapshot',
            is_foreground INTEGER DEFAULT 0
        );
        CREATE TABLE events_hourly (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hour_bucket INTEGER NOT NULL,
            app_name TEXT NOT NULL,
            avg_cpu_pct REAL,
            total_net_bytes INTEGER,
            avg_battery_drain REAL,
            event_count INTEGER DEFAULT 0
        );
        CREATE TABLE action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            app_name TEXT NOT NULL,
            pid INTEGER,
            predicted_label TEXT,
            confidence REAL,
            action_taken TEXT NOT NULL,
            reason TEXT,
            shadow_mode INTEGER DEFAULT 1,
            battery_before REAL,
            battery_after REAL,
            reverted INTEGER DEFAULT 0,
            enforcer_cmd TEXT
        );
        CREATE TABLE user_corrections (
            app_name TEXT PRIMARY KEY,
            correction_factor REAL NOT NULL DEFAULT 1.0,
            observation_count INTEGER NOT NULL DEFAULT 0,
            last_updated_ts INTEGER
        );
    """)
    now = int(time.time())
    # Insert sample events
    for i, app in enumerate(["dropbox", "chrome", "code", "spotify"]):
        for j in range(5):
            conn.execute(
                "INSERT INTO events (timestamp, app_name, cpu_pct, battery_pct, event_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (now - j*60, app, 10.0 + i*5, 80.0 - j, "snapshot"),
            )
    # Insert sample decisions
    for app, label, action in [
        ("dropbox", "idle_likely",       "throttle"),
        ("chrome",  "active_needed",     "allow"),
        ("spotify", "background_unused", "skip"),
    ]:
        conn.execute(
            "INSERT INTO action_log "
            "(timestamp, app_name, pid, predicted_label, confidence, action_taken, reason, shadow_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now - 30, app, 1000, label, 0.92, action, f"test reason for {app}", 0),
        )
    conn.commit()
    conn.close()
    return db_path


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


# ── Display tests ─────────────────────────────────────────────────────────────

print("\n── Display Helpers ──────────────────────────────────")


@test("bar: full value returns all filled chars")
def _():
    b = bar(1.0, width=10)
    assert "█" * 10 in b


@test("bar: zero value returns all empty chars")
def _():
    b = bar(0.0, width=10)
    assert "░" * 10 in b


@test("bar: 50% value returns half filled")
def _():
    b = bar(0.5, width=10)
    # Has both filled and empty parts
    assert "█" in b and "░" in b


@test("fmt_age: recent timestamp returns seconds")
def _():
    ts = int(time.time()) - 30
    result = fmt_age(ts)
    assert "s ago" in result


@test("fmt_age: old timestamp returns hours")
def _():
    ts = int(time.time()) - 7200
    result = fmt_age(ts)
    assert "h ago" in result


@test("fmt_time: returns HH:MM:SS format")
def _():
    ts = int(time.time())
    result = fmt_time(ts)
    parts = result.split(":")
    assert len(parts) == 3


# ── Parser tests ──────────────────────────────────────────────────────────────

print("\n── Argument Parser ──────────────────────────────────")


@test("parser: status command is parsed correctly")
def _():
    parser = build_parser()
    args = parser.parse_args(["status"])
    assert args.command == "status"
    assert args.limit == 15


@test("parser: explain requires an app name")
def _():
    parser = build_parser()
    args = parser.parse_args(["explain", "dropbox"])
    assert args.command == "explain"
    assert args.app == "dropbox"


@test("parser: override --always-allow sets mode correctly")
def _():
    parser = build_parser()
    args = parser.parse_args(["override", "spotify", "--always-allow"])
    assert args.command == "override"
    assert args.app == "spotify"
    assert args.mode == "always-allow"


@test("parser: override --always-throttle sets mode correctly")
def _():
    parser = build_parser()
    args = parser.parse_args(["override", "updater", "--always-throttle"])
    assert args.mode == "always-throttle"


@test("parser: override --reset sets mode correctly")
def _():
    parser = build_parser()
    args = parser.parse_args(["override", "dropbox", "--reset"])
    assert args.mode == "reset"


@test("parser: report --hours is parsed correctly")
def _():
    parser = build_parser()
    args = parser.parse_args(["report", "--hours", "48"])
    assert args.command == "report"
    assert args.hours == 48


@test("parser: --db flag changes db path")
def _():
    parser = build_parser()
    args = parser.parse_args(["--db", "/tmp/test.db", "status"])
    assert str(args.db) == "/tmp/test.db"


@test("parser: override requires exactly one mode flag")
def _():
    parser = build_parser()
    raised = False
    try:
        parser.parse_args(["override", "app"])  # no mode → error
    except SystemExit:
        raised = True
    assert raised, "Expected SystemExit when no mode given"


# ── Command tests ─────────────────────────────────────────────────────────────

print("\n── Commands ─────────────────────────────────────────")


@test("cmd_status: runs without error on sample DB")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        out = StringIO()
        with patch("sys.stdout", out):
            cmd_status(db, limit=5)
        output = out.getvalue()
        assert "PowerLayer" in output or len(output) > 0


@test("cmd_explain: shows decisions for known app")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        out = StringIO()
        with patch("sys.stdout", out):
            cmd_explain(db, "dropbox", limit=5)
        output = out.getvalue()
        assert "dropbox" in output.lower() or "Explain" in output


@test("cmd_explain: graceful output for unknown app")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        out = StringIO()
        with patch("sys.stdout", out):
            cmd_explain(db, "nonexistent_app_xyz", limit=5)
        output = out.getvalue()
        assert "No decisions found" in output or "nonexistent" in output.lower()


@test("cmd_override: always-allow writes factor to DB")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        cmd_override(db, "spotify", "always-allow")
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT correction_factor FROM user_corrections WHERE app_name='spotify'"
        ).fetchone()
        conn.close()
        assert row is not None, "Override row not found"
        assert row[0] == 3.0, f"Expected factor=3.0, got {row[0]}"


@test("cmd_override: always-throttle writes low factor to DB")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        cmd_override(db, "updater", "always-throttle")
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT correction_factor FROM user_corrections WHERE app_name='updater'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 0.1, f"Expected factor=0.1, got {row[0]}"


@test("cmd_override: reset writes factor=1.0")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        # First set, then reset
        cmd_override(db, "dropbox", "always-allow")
        cmd_override(db, "dropbox", "reset")
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT correction_factor FROM user_corrections WHERE app_name='dropbox'"
        ).fetchone()
        conn.close()
        assert row[0] == 1.0, f"Expected 1.0 after reset, got {row[0]}"


@test("cmd_override: invalid mode exits cleanly")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        exited = False
        try:
            cmd_override(db, "app", "invalid-mode")
        except SystemExit:
            exited = True
        assert exited, "Expected SystemExit for invalid mode"


@test("cmd_report: runs without error on sample DB")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        out = StringIO()
        with patch("sys.stdout", out):
            cmd_report(db, hours=24)
        output = out.getvalue()
        assert len(output) > 0


@test("cmd_report: shows override section when correction exists")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        cmd_override(db, "spotify", "always-allow")
        out = StringIO()
        with patch("sys.stdout", out):
            cmd_report(db, hours=24)
        output = out.getvalue()
        assert "spotify" in output.lower() or "Override" in output


# ── End-to-end main() test ────────────────────────────────────────────────────

print("\n── End-to-End ───────────────────────────────────────")


@test("main(): override command runs via entry point")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        out = StringIO()
        with patch("sys.stdout", out):
            main(["--db", str(db), "override", "testapp", "--always-allow"])


@test("main(): report command runs via entry point")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        out = StringIO()
        with patch("sys.stdout", out):
            main(["--db", str(db), "report", "--hours", "1"])


# ── Summary ───────────────────────────────────────────────────────────────────

passed = sum(1 for _, ok, _ in _results if ok)
failed = sum(1 for _, ok, _ in _results if not ok)

print(f"\n{'═'*43}")
print(f"  Results: {passed} passed, {failed} failed")
print(f"{'═'*43}\n")

if failed:
    sys.exit(1)
