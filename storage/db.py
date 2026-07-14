"""
storage/db.py
─────────────
SQLite connection manager + BatchedWriter for PowerLayer.

Design decisions:
- A single persistent connection per process (thread-safe via check_same_thread=False
  + a threading.Lock around all writes).
- WAL mode: readers never block writers, writers never block readers.
- BatchedWriter buffers rows in-memory and flushes as ONE transaction, either
  every `flush_interval_seconds` seconds OR when `flush_batch_size` rows
  accumulate — whichever comes first. This keeps write overhead near zero.
- Schema is applied at connection time (idempotent CREATE TABLE IF NOT EXISTS).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Resolved at runtime by load_config() in the main entry point;
# db.py itself is config-agnostic so it can be used in unit tests with any path.
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "powerlayer.db"
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


# ─────────────────────────────────────────────────────────────────────────────
# Connection factory
# ─────────────────────────────────────────────────────────────────────────────

def get_connection(db_path: str | Path = _DEFAULT_DB_PATH) -> sqlite3.Connection:
    """
    Open (or create) the SQLite database, apply the schema, and return the
    connection.  Enables WAL mode and sensible PRAGMAs.

    Call once at startup; reuse the returned connection everywhere.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,   # we guard writes with a Lock ourselves
        isolation_level=None,       # autocommit — we manage transactions manually
    )

    # Performance / safety PRAGMAs
    conn.execute("PRAGMA journal_mode=WAL;")         # concurrent reads during writes
    conn.execute("PRAGMA synchronous=NORMAL;")        # safe + fast (not FULL)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA cache_size=-8000;")          # 8 MB page cache

    # Apply schema (all statements are idempotent)
    schema_sql = _SCHEMA_PATH.read_text()
    conn.executescript(schema_sql)

    logger.info("SQLite connection opened: %s (WAL mode)", db_path)
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# BatchedWriter
# ─────────────────────────────────────────────────────────────────────────────

class BatchedWriter:
    """
    Buffers event rows in a Python list and flushes them to SQLite in a single
    transaction — either on a time cadence or when the buffer hits a size limit.

    Thread-safe: `add()` and `flush()` can be called from any thread.

    Usage
    -----
        writer = BatchedWriter(conn, flush_interval=30, batch_size=100)
        writer.start()          # start background flush timer thread
        writer.add(row_dict)    # called from the collector loop
        writer.flush()          # can also be called manually at shutdown
        writer.stop()           # stop the timer thread
    """

    _INSERT_EVENT = """
        INSERT INTO events (timestamp, app_name, pid, event_type,
                            cpu_pct, net_bytes, battery_pct)
        VALUES (:timestamp, :app_name, :pid, :event_type,
                :cpu_pct, :net_bytes, :battery_pct)
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        flush_interval: float = 30.0,
        batch_size: int = 100,
    ) -> None:
        self._conn = conn
        self._flush_interval = flush_interval
        self._batch_size = batch_size

        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._flush_thread: threading.Thread | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background flush timer thread."""
        if self._flush_thread and self._flush_thread.is_alive():
            return
        self._stop_event.clear()
        self._flush_thread = threading.Thread(
            target=self._timer_loop, name="powerlayer-writer", daemon=True
        )
        self._flush_thread.start()
        logger.debug("BatchedWriter timer thread started (interval=%ss, batch=%s)",
                     self._flush_interval, self._batch_size)

    def stop(self) -> None:
        """Signal the timer thread to stop and do a final flush."""
        self._stop_event.set()
        if self._flush_thread:
            self._flush_thread.join(timeout=self._flush_interval + 5)
        self.flush()   # drain whatever is left
        logger.info("BatchedWriter stopped; buffer drained.")

    def add(self, row: dict[str, Any]) -> None:
        """
        Add a single event row to the in-memory buffer.

        Expected keys (all optional except timestamp/app_name/event_type):
            timestamp   : int   — Unix epoch seconds
            app_name    : str
            pid         : int   — process ID
            event_type  : str   — 'foreground'|'sync'|'network'|'wake'|'idle'
            cpu_pct     : float
            net_bytes   : int
            battery_pct : float | None
        """
        # Fill in defaults so the INSERT never fails on missing keys
        row.setdefault("timestamp", int(time.time()))
        row.setdefault("pid", None)
        row.setdefault("cpu_pct", 0.0)
        row.setdefault("net_bytes", 0)
        row.setdefault("battery_pct", None)

        with self._lock:
            self._buffer.append(row)
            should_flush = len(self._buffer) >= self._batch_size

        if should_flush:
            logger.debug("Buffer hit batch_size=%s — flushing early.", self._batch_size)
            self.flush()

    def flush(self) -> int:
        """
        Write all buffered rows to SQLite in a single transaction.
        Returns the number of rows written.
        Thread-safe.
        """
        with self._lock:
            if not self._buffer:
                return 0
            rows, self._buffer = self._buffer, []

        try:
            with self._conn:   # context manager = BEGIN … COMMIT / ROLLBACK
                self._conn.executemany(self._INSERT_EVENT, rows)
            logger.debug("Flushed %d rows to events table.", len(rows))
            return len(rows)
        except sqlite3.Error as exc:
            logger.error("Flush failed (%s) — %d rows lost.", exc, len(rows))
            return 0

    # ── Timer loop (background thread) ────────────────────────────────────────

    def _timer_loop(self) -> None:
        while not self._stop_event.wait(timeout=self._flush_interval):
            self.flush()
        # One last flush after stop is signalled (stop() calls flush() too,
        # but this handles the race where stop() is called just after a flush)


# ─────────────────────────────────────────────────────────────────────────────
# Query helpers (used by features.py and the CLI)
# ─────────────────────────────────────────────────────────────────────────────

def get_recent(
    app_name: str,
    conn: sqlite3.Connection,
    hours: int = 24,
) -> list[dict[str, Any]]:
    """
    Return raw event rows for `app_name` from the last `hours` hours.
    Includes both the raw `events` table and (if the window spans >48h)
    the hourly aggregate.  For feature engineering, 24h of raw data is enough.
    """
    cutoff = int(time.time()) - hours * 3600
    cursor = conn.execute(
        """
        SELECT timestamp, app_name, pid, event_type, cpu_pct, net_bytes, battery_pct
        FROM   events
        WHERE  app_name = ?
          AND  timestamp >= ?
        ORDER  BY timestamp ASC
        """,
        (app_name, cutoff),
    )
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def get_app_history(
    app_name: str,
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """
    Return the full hourly aggregate history for `app_name`.
    Used by features.py to compute an app's own historical baseline.
    """
    cursor = conn.execute(
        """
        SELECT hour_bucket, app_name, avg_cpu_pct, total_net_bytes,
               avg_battery_drain, event_count
        FROM   events_hourly
        WHERE  app_name = ?
        ORDER  BY hour_bucket ASC
        """,
        (app_name,),
    )
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def log_action(
    conn: sqlite3.Connection,
    *,
    app_name: str,
    pid: int | None = None,
    predicted_label: str | None = None,
    action_taken: str,
    reason: str | None = None,
    confidence: float | None = None,
    shadow_mode: bool = True,
    battery_before: float | None = None,
    battery_after: float | None = None,
    reverted: bool = False,
    enforcer_cmd: str | None = None,
) -> None:
    """
    Insert a row into action_log.  Called by the policy engine after every
    decision — regardless of whether shadow_mode is on.
    """
    conn.execute(
        """
        INSERT INTO action_log
            (timestamp, app_name, pid, predicted_label, action_taken, reason, confidence,
             shadow_mode, battery_before, battery_after, reverted, enforcer_cmd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(time.time()),
            app_name,
            pid,
            predicted_label,
            action_taken,
            reason,
            confidence,
            int(shadow_mode),
            battery_before,
            battery_after,
            int(reverted),
            enforcer_cmd,
        ),
    )
