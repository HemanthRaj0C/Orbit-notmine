"""
storage/aggregator.py
─────────────────────
Rolling aggregation job — keeps the `events` table bounded.

How it works
────────────
Every call to `run()`:
  1. Finds all rows in `events` older than `retention_hours` hours.
  2. Groups them by (hour_bucket, app_name).
  3. Upserts the aggregate into `events_hourly` (adds to existing bucket if
     it already exists — handles re-runs safely via UNIQUE constraint +
     INSERT OR REPLACE with weighted merge logic).
  4. Deletes the raw rows that were just aggregated.

This is designed to be called:
  - From monitor.py's main loop every hour, OR
  - From a systemd timer (simple cron-style).

It is idempotent: running it twice on the same data produces the same result.
"""

from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

# Default raw-data retention window (hours).
# Raw rows older than this are aggregated → deleted.
DEFAULT_RETENTION_HOURS: int = 48

# How long to keep hourly aggregate buckets (days).
# After this, hourly buckets are deleted (model training only needs ~30 days).
DEFAULT_HOURLY_RETENTION_DAYS: int = 30

# How long to keep action_log entries (days).
# Long enough for the report/explain CLI, short enough to avoid bloat.
DEFAULT_ACTION_LOG_RETENTION_DAYS: int = 90


def run(
    conn: sqlite3.Connection,
    retention_hours: int = DEFAULT_RETENTION_HOURS,
    hourly_retention_days: int = DEFAULT_HOURLY_RETENTION_DAYS,
    action_log_retention_days: int = DEFAULT_ACTION_LOG_RETENTION_DAYS,
) -> dict[str, int]:
    """
    Aggregate raw events older than `retention_hours` hours into
    `events_hourly`, then delete them from `events`.

    Also prunes old rows from:
      - events_hourly : rows older than `hourly_retention_days` days
      - action_log    : rows older than `action_log_retention_days` days

    Returns
    -------
    dict with keys:
        "buckets_written"  : number of (hour, app) groups aggregated
        "rows_deleted"     : number of raw event rows removed
        "hourly_pruned"    : number of old hourly bucket rows deleted
        "action_log_pruned": number of old action_log rows deleted
    """
    cutoff_ts = int(time.time()) - retention_hours * 3600

    # ── Step 1: Compute per-(hour_bucket, app_name) aggregates ───────────────
    # hour_bucket = Unix timestamp of the floor of the hour containing the event
    aggregate_sql = """
        SELECT
            (timestamp / 3600) * 3600   AS hour_bucket,
            app_name,
            AVG(cpu_pct)                AS avg_cpu_pct,
            SUM(net_bytes)              AS total_net_bytes,
            AVG(battery_pct)            AS avg_battery_drain,
            COUNT(*)                    AS event_count
        FROM  events
        WHERE timestamp < ?
        GROUP BY hour_bucket, app_name
    """
    cursor = conn.execute(aggregate_sql, (cutoff_ts,))
    new_buckets = cursor.fetchall()

    if not new_buckets:
        logger.debug("Aggregator: nothing to aggregate (no events older than %dh).",
                     retention_hours)
        # Still prune old hourly + action_log rows even if no new raw events
        hourly_cutoff = int(time.time()) - hourly_retention_days * 86400
        action_cutoff = int(time.time()) - action_log_retention_days * 86400
        hourly_pruned  = conn.execute(
            "DELETE FROM events_hourly WHERE hour_bucket < ?", (hourly_cutoff,)
        ).rowcount
        action_pruned  = conn.execute(
            "DELETE FROM action_log WHERE timestamp < ?", (action_cutoff,)
        ).rowcount
        conn.commit()
        return {
            "buckets_written": 0, "rows_deleted": 0,
            "hourly_pruned": hourly_pruned, "action_log_pruned": action_pruned,
        }

    # ── Step 2: Upsert into events_hourly ────────────────────────────────────
    # We use INSERT OR REPLACE but we must merge with existing rows if the
    # bucket already exists (e.g. aggregator ran twice in the same hour).
    # Strategy: read existing row, compute weighted average, then replace.

    buckets_written = 0
    with conn:  # single transaction for the whole aggregation run
        for row in new_buckets:
            hour_bucket, app_name, avg_cpu, total_net, avg_batt, event_count = row

            # Check if this (hour_bucket, app_name) bucket already exists
            existing = conn.execute(
                """
                SELECT avg_cpu_pct, total_net_bytes, avg_battery_drain, event_count
                FROM   events_hourly
                WHERE  hour_bucket = ? AND app_name = ?
                """,
                (hour_bucket, app_name),
            ).fetchone()

            if existing is not None:
                # Merge: weighted average for cpu/battery, sum for net_bytes
                ex_avg_cpu, ex_total_net, ex_avg_batt, ex_count = existing
                total_events = ex_count + event_count
                merged_cpu = (
                    (ex_avg_cpu * ex_count + avg_cpu * event_count) / total_events
                    if total_events > 0 else 0.0
                )
                merged_net = (ex_total_net or 0) + (total_net or 0)
                merged_batt: float | None
                if ex_avg_batt is not None and avg_batt is not None:
                    merged_batt = (
                        (ex_avg_batt * ex_count + avg_batt * event_count) / total_events
                    )
                else:
                    merged_batt = ex_avg_batt if ex_avg_batt is not None else avg_batt

                conn.execute(
                    """
                    UPDATE events_hourly
                    SET    avg_cpu_pct       = ?,
                           total_net_bytes   = ?,
                           avg_battery_drain = ?,
                           event_count       = ?
                    WHERE  hour_bucket = ? AND app_name = ?
                    """,
                    (merged_cpu, merged_net, merged_batt, total_events,
                     hour_bucket, app_name),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO events_hourly
                        (hour_bucket, app_name, avg_cpu_pct, total_net_bytes,
                         avg_battery_drain, event_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (hour_bucket, app_name, avg_cpu, total_net, avg_batt, event_count),
                )

            buckets_written += 1

        # ── Step 3: Delete the raw rows we just aggregated ───────────────────
        result = conn.execute(
            "DELETE FROM events WHERE timestamp < ?",
            (cutoff_ts,),
        )
        rows_deleted = result.rowcount

    logger.info(
        "Aggregator: wrote %d hour-buckets, deleted %d raw rows (>%dh old).",
        buckets_written, rows_deleted, retention_hours,
    )

    # ── Step 5: Prune old events_hourly rows ─────────────────────────────────
    # Keep the last N days of hourly aggregates; older ones are not needed
    # for model training or the report CLI.
    hourly_cutoff = int(time.time()) - hourly_retention_days * 86400
    hourly_prune = conn.execute(
        "DELETE FROM events_hourly WHERE hour_bucket < ?",
        (hourly_cutoff,),
    )
    hourly_pruned = hourly_prune.rowcount
    if hourly_pruned:
        logger.info(
            "Aggregator: pruned %d hourly buckets older than %d days.",
            hourly_pruned, hourly_retention_days,
        )

    # ── Step 6: Prune old action_log rows ────────────────────────────────────
    # Keep 90 days of decisions — enough for the report CLI and explain command.
    action_cutoff = int(time.time()) - action_log_retention_days * 86400
    action_prune = conn.execute(
        "DELETE FROM action_log WHERE timestamp < ?",
        (action_cutoff,),
    )
    action_pruned = action_prune.rowcount
    if action_pruned:
        logger.info(
            "Aggregator: pruned %d action_log rows older than %d days.",
            action_pruned, action_log_retention_days,
        )

    conn.commit()

    # ── Step 7: WAL checkpoint ────────────────────────────────────────────────────
    # Without periodic checkpointing, the .db-wal file can grow very large
    # under continuous writes.  PASSIVE mode flushes WAL pages back into the
    # main DB file without blocking any active readers or writers.
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
        logger.debug("Aggregator: WAL checkpoint completed.")
    except Exception as exc:
        logger.warning("Aggregator: WAL checkpoint failed: %s", exc)

    return {
        "buckets_written":   buckets_written,
        "rows_deleted":      rows_deleted,
        "hourly_pruned":     hourly_pruned,
        "action_log_pruned": action_pruned,
    }


def get_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """
    Return basic counts — useful for CLI `status` command and health checks.
    """
    raw_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    hourly_count = conn.execute("SELECT COUNT(*) FROM events_hourly").fetchone()[0]
    action_count = conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
    correction_count = conn.execute(
        "SELECT COUNT(*) FROM user_corrections WHERE observation_count > 0"
    ).fetchone()[0]

    return {
        "raw_events": raw_count,
        "hourly_buckets": hourly_count,
        "action_log_entries": action_count,
        "apps_with_personalization": correction_count,
    }
