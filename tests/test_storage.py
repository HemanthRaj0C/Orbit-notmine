"""
tests/test_storage.py
─────────────────────
Acceptance tests for storage/db.py and storage/aggregator.py.

Run with:  python -m pytest tests/test_storage.py -v
Or simply: python tests/test_storage.py
"""

from __future__ import annotations

import sys
import time
import sqlite3
import tempfile
from pathlib import Path

# Make powerlayer importable from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import get_connection, BatchedWriter, get_recent, get_app_history, log_action
from storage import aggregator


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def fresh_db() -> sqlite3.Connection:
    """Return an in-memory (`:memory:`) connection with the full schema applied."""
    # We need a temp file because :memory: doesn't support WAL mode with
    # check_same_thread=False on all SQLite builds.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = get_connection(tmp.name)
    return conn


def make_row(app: str, ts: int | None = None, cpu: float = 5.0) -> dict:
    return {
        "timestamp": ts or int(time.time()),
        "app_name": app,
        "pid": 1234,
        "event_type": "foreground",
        "cpu_pct": cpu,
        "net_bytes": 1024,
        "battery_pct": 80.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Test: BatchedWriter flush behaviour
# ─────────────────────────────────────────────────────────────────────────────

def test_batched_writer_single_flush():
    """1000 buffered events should insert in one transaction well under 100ms."""
    conn = fresh_db()
    writer = BatchedWriter(conn, flush_interval=999, batch_size=999_999)

    N = 1000
    for i in range(N):
        writer.add(make_row("firefox", cpu=float(i % 100)))

    start = time.perf_counter()
    written = writer.flush()
    elapsed_ms = (time.perf_counter() - start) * 1000

    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert written == N, f"Expected {N} rows written, got {written}"
    assert count == N, f"Expected {N} rows in DB, got {count}"
    assert elapsed_ms < 100, f"Flush took {elapsed_ms:.1f}ms — should be <100ms"
    print(f"  ✓ {N} rows flushed in {elapsed_ms:.1f}ms")
    conn.close()


def test_batched_writer_auto_flush_on_size():
    """Writer should auto-flush when buffer hits batch_size."""
    conn = fresh_db()
    writer = BatchedWriter(conn, flush_interval=9999, batch_size=10)

    for i in range(10):
        writer.add(make_row("code", cpu=1.0))

    # Give the synchronous auto-flush (triggered inside add()) time to commit
    time.sleep(0.05)
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 10, f"Expected 10 rows after auto-flush, got {count}"
    print(f"  ✓ Auto-flush on batch_size=10 works (got {count} rows)")
    conn.close()


def test_batched_writer_defaults():
    """add() should not crash on rows with missing optional fields."""
    conn = fresh_db()
    writer = BatchedWriter(conn)
    writer.add({"app_name": "minimal_app", "event_type": "idle"})
    writer.flush()
    row = conn.execute("SELECT * FROM events").fetchone()
    assert row is not None, "Row should have been inserted"
    print("  ✓ Minimal row with defaults inserts cleanly")
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test: get_recent and get_app_history
# ─────────────────────────────────────────────────────────────────────────────

def test_get_recent():
    conn = fresh_db()
    now = int(time.time())
    writer = BatchedWriter(conn, flush_interval=9999, batch_size=9999)

    # 5 recent rows + 2 old rows
    for _ in range(5):
        writer.add(make_row("vlc", ts=now - 3600))   # 1h ago
    for _ in range(2):
        writer.add(make_row("vlc", ts=now - 200_000))  # ~55h ago — outside 24h window
    writer.flush()

    recent = get_recent("vlc", conn, hours=24)
    assert len(recent) == 5, f"Expected 5 recent rows, got {len(recent)}"
    print(f"  ✓ get_recent() returns only rows within the time window")
    conn.close()


def test_get_app_history():
    conn = fresh_db()
    # Insert a fake hourly aggregate row directly
    conn.execute(
        """INSERT INTO events_hourly
           (hour_bucket, app_name, avg_cpu_pct, total_net_bytes, avg_battery_drain, event_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (1720000000, "spotify", 12.5, 50000, 79.0, 30),
    )
    conn.commit()

    history = get_app_history("spotify", conn)
    assert len(history) == 1
    assert history[0]["avg_cpu_pct"] == 12.5
    print("  ✓ get_app_history() returns hourly aggregate rows correctly")
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test: Aggregator
# ─────────────────────────────────────────────────────────────────────────────

def test_aggregator_moves_old_rows():
    """Rows older than retention_hours must be moved to events_hourly and deleted."""
    conn = fresh_db()
    now = int(time.time())

    writer = BatchedWriter(conn, flush_interval=9999, batch_size=9999)

    # 10 "old" rows (55h ago)
    old_ts = now - 55 * 3600
    for i in range(10):
        writer.add(make_row("slack", ts=old_ts + i, cpu=10.0))

    # 5 "recent" rows (1h ago) — should NOT be touched
    for i in range(5):
        writer.add(make_row("slack", ts=now - 3600 + i, cpu=20.0))

    writer.flush()

    raw_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert raw_before == 15, f"Expected 15 raw rows before aggregation, got {raw_before}"

    result = aggregator.run(conn, retention_hours=48)

    raw_after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    hourly_count = conn.execute("SELECT COUNT(*) FROM events_hourly").fetchone()[0]

    assert raw_after == 5, f"Expected 5 recent rows to remain, got {raw_after}"
    assert result["rows_deleted"] == 10, f"Expected 10 deleted, got {result['rows_deleted']}"
    assert hourly_count >= 1, "Expected at least 1 hourly bucket created"
    print(f"  ✓ Aggregator moved 10 old rows → {hourly_count} bucket(s), left 5 recent rows")
    conn.close()


def test_aggregator_idempotent():
    """Running aggregator twice on the same data should not double-count."""
    conn = fresh_db()
    now = int(time.time())
    # Pin all rows to the exact same timestamp so they land in ONE hour bucket.
    old_ts = now - 55 * 3600

    writer = BatchedWriter(conn, flush_interval=9999, batch_size=9999)
    for i in range(6):
        writer.add(make_row("chrome", ts=old_ts, cpu=15.0))  # same second → same bucket
    writer.flush()

    result1 = aggregator.run(conn, retention_hours=48)
    assert result1["rows_deleted"] == 6

    # Second run: no raw rows remain — should aggregate nothing
    result2 = aggregator.run(conn, retention_hours=48)
    assert result2["buckets_written"] == 0, (
        f"Expected 0 buckets on 2nd run, got {result2['buckets_written']}"
    )
    assert result2["rows_deleted"] == 0

    hourly_count = conn.execute("SELECT COUNT(*) FROM events_hourly").fetchone()[0]
    # Exactly 1 bucket (all 6 rows share the same hour)
    assert hourly_count == 1, f"Expected 1 hourly bucket, got {hourly_count}"
    print("  ✓ Aggregator is idempotent on repeated runs")
    conn.close()


def test_aggregator_weighted_merge():
    """Re-running after partial aggregation should correctly merge bucket counts."""
    conn = fresh_db()
    now = int(time.time())
    # Pin all raw rows to the same second inside the target hour bucket.
    old_ts = now - 55 * 3600
    hour_bucket = (old_ts // 3600) * 3600

    # Pre-populate an existing hourly row with 10 events at avg_cpu=20
    conn.execute(
        """INSERT INTO events_hourly
           (hour_bucket, app_name, avg_cpu_pct, total_net_bytes, avg_battery_drain, event_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (hour_bucket, "zoom", 20.0, 10000, 75.0, 10),
    )
    conn.commit()

    # Add 10 more raw rows pinned to the SAME second (same hour_bucket guaranteed)
    writer = BatchedWriter(conn, flush_interval=9999, batch_size=9999)
    for _ in range(10):
        writer.add(make_row("zoom", ts=old_ts, cpu=40.0))  # all same ts → same bucket
    writer.flush()

    aggregator.run(conn, retention_hours=48)

    row = conn.execute(
        "SELECT avg_cpu_pct, event_count FROM events_hourly WHERE app_name='zoom'"
    ).fetchone()
    assert row is not None
    merged_cpu, total_count = row
    assert total_count == 20, f"Expected 20 events merged, got {total_count}"
    # Weighted average: (20.0*10 + 40.0*10) / 20 = 30.0
    assert abs(merged_cpu - 30.0) < 0.01, f"Expected merged cpu=30.0, got {merged_cpu}"
    print(f"  ✓ Weighted merge correct: avg_cpu={merged_cpu:.1f}, count={total_count}")
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test: log_action
# ─────────────────────────────────────────────────────────────────────────────

def test_log_action():
    conn = fresh_db()
    log_action(
        conn,
        app_name="dropbox",
        prediction="idle_likely",
        action="throttle_sync",
        confidence=0.94,
        shadow_mode=True,
        battery_before=82.0,
    )
    conn.commit()
    row = conn.execute("SELECT * FROM action_log").fetchone()
    assert row is not None
    # action is column index 4 (0-indexed: id, timestamp, app_name, prediction, action, ...)
    print(f"  ✓ log_action() inserts correctly → action='{row[4]}'")
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_batched_writer_single_flush,
        test_batched_writer_auto_flush_on_size,
        test_batched_writer_defaults,
        test_get_recent,
        test_get_app_history,
        test_aggregator_moves_old_rows,
        test_aggregator_idempotent,
        test_aggregator_weighted_merge,
        test_log_action,
    ]

    print("\n═══════════════════════════════════════")
    print("  PowerLayer — Storage Tests")
    print("═══════════════════════════════════════\n")

    passed = 0
    failed = 0
    for test in tests:
        name = test.__name__
        print(f"▶ {name}")
        try:
            test()
            passed += 1
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")
            import traceback
            traceback.print_exc()
            failed += 1
        print()

    print("═══════════════════════════════════════")
    print(f"  Results: {passed} passed, {failed} failed")
    print("═══════════════════════════════════════\n")
    sys.exit(0 if failed == 0 else 1)
