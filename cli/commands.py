"""
cli/commands.py
───────────────
Implementations for all four PowerLayer CLI commands.

Commands:
  status   — live dashboard: running processes, throttle state, model confidence
  explain  — "why did you throttle X?" — reads action_log with full reasoning
  override — "always allow spotify" / "reset dropbox" — writes correction_factor
  report   — battery savings summary over a time window
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# ── Project imports ────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from cli.display import (
    header, section, kv, table, bar, rule,
    green, yellow, red, blue, cyan, bold, dim, magenta,
    fmt_time, fmt_datetime, fmt_age, terminal_width,
)
from storage.db import get_connection
from storage import aggregator


# ── Helpers ───────────────────────────────────────────────────────────────────

def _open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        print(red(f"  ✗ Database not found: {db_path}"))
        print(dim("  Run the daemon first: python tools/powerlayer_daemon.py --live"))
        sys.exit(1)
    return get_connection(str(db_path))


# ─────────────────────────────────────────────────────────────────────────────
# status
# ─────────────────────────────────────────────────────────────────────────────

def cmd_status(db_path: Path, limit: int = 20) -> None:
    """
    Show a live snapshot of recent process events, model decisions, and
    system health.

    powerlayer status [--limit N]
    """
    conn = _open_db(db_path)
    now  = int(time.time())

    header("Orbit  —  Status", f"DB: {db_path}  |  {fmt_datetime(now)}")

    # ── Storage stats ─────────────────────────────────────────────────────────
    section("Storage")
    stats = aggregator.get_stats(conn)
    kv("Raw events",        str(stats.get("raw_events", 0)))
    kv("Hourly buckets",    str(stats.get("hourly_buckets", 0)))
    kv("Action log entries",str(stats.get("action_log_entries", 0)))
    kv("Apps personalised", str(stats.get("apps_with_personalization", 0)))

    # ── Shadow mode indicator ─────────────────────────────────────────────────
    section("Mode")
    try:
        import yaml
        cfg_path = _ROOT / "config.yaml"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        shadow = cfg.get("shadow_mode", True)
    except Exception:
        shadow = True
    shadow_str = (yellow("SHADOW  (decisions logged, nothing enforced)")
                  if shadow else green("LIVE  (enforcement active)"))
    kv("shadow_mode", shadow_str)

    # ── Recent events ─────────────────────────────────────────────────────────
    section("Recent Collector Events")
    now_ts  = int(time.time())
    rows = []
    try:
        cutoff = now_ts - 300   # last 5 minutes
        raw = conn.execute(
            """
            SELECT timestamp, app_name, cpu_pct, battery_pct, event_type
            FROM   events
            WHERE  timestamp > ?
            ORDER  BY timestamp DESC
            LIMIT  ?
            """,
            (cutoff, limit),
        ).fetchall()
        rows = [
            {
                "timestamp":   fmt_age(int(r[0])),
                "app_name":    r[1] or "",
                "cpu_pct":     f"{r[2]:.1f}" if r[2] else "0.0",
                "battery_pct": f"{r[3]:.1f}" if r[3] else "—",
                "event_type":  r[4] or "",
            }
            for r in raw
        ]
    except Exception:
        pass

    if not rows:
        print(dim("  (no events in last 5 minutes — is the collector running?)"))
    else:
        cols = [
            ("app_name",    "APP",      18),
            ("cpu_pct",     "CPU%",      6),
            ("battery_pct", "BAT%",      6),
            ("event_type",  "EVENT",    10),
            ("timestamp",   "AGO",       8),
        ]
        table(rows, cols)

    # ── Recent policy decisions ───────────────────────────────────────────────
    section("Recent Policy Decisions")
    try:
        decision_rows = conn.execute(
            """
            SELECT timestamp, app_name, predicted_label, confidence,
                   action_taken, reason
            FROM   action_log
            ORDER  BY timestamp DESC
            LIMIT  ?
            """,
            (limit,),
        ).fetchall()
    except Exception:
        decision_rows = []

    if not decision_rows:
        print(dim("  (no decisions yet)"))
    else:
        dcols = [
            ("time",       "TIME",   8),
            ("app_name",   "APP",   16),
            ("label",      "LABEL", 20),
            ("conf",       "CONF",   6),
            ("action",     "ACTION", 9),
        ]
        drows = []
        for r in decision_rows:
            drows.append({
                "time":     fmt_time(r[0]),
                "app_name": r[1] or "",
                "label":    r[2] or "",
                "conf":     f"{r[3]:.2f}" if r[3] else "—",
                "action":   r[4] or "",
                "reason":   r[5] or "",
            })
        table(drows, dcols)

    # ── App leaderboard (top CPU consumers last hour) ─────────────────────────
    section("Top Apps  (last hour by avg CPU)")
    try:
        hour_ago = now - 3600
        top = conn.execute(
            """
            SELECT app_name, AVG(cpu_pct) as avg_cpu, COUNT(*) as events,
                   AVG(battery_pct) as avg_bat
            FROM   events
            WHERE  timestamp > ?
            GROUP  BY app_name
            ORDER  BY avg_cpu DESC
            LIMIT  10
            """,
            (hour_ago,),
        ).fetchall()
    except Exception:
        top = []

    if not top:
        print(dim("  (no data — run collector for a while first)"))
    else:
        for row in top:
            name, cpu, evts, bat = row
            cpu_bar = bar(min(cpu / 100.0, 1.0), width=16,
                          color_fn=red if cpu > 50 else yellow if cpu > 20 else green)
            print(f"  {bold(f'{name:<20}')}  {cpu_bar}  {cpu:5.1f}%  "
                  f"{dim(f'({int(evts)} events)')}")

    print()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# explain
# ─────────────────────────────────────────────────────────────────────────────

def cmd_explain(db_path: Path, app_name: str, limit: int = 10) -> None:
    """
    Show the reasoning behind every decision made for a specific app.

    powerlayer explain <app_name> [--limit N]
    """
    conn = _open_db(db_path)

    header(f"Explain  —  {app_name}",
           "Full reasoning for every policy decision on this app")

    # ── Decision history ──────────────────────────────────────────────────────
    try:
        rows = conn.execute(
            """
            SELECT timestamp, pid, predicted_label, confidence,
                   action_taken, reason, shadow_mode, enforcer_cmd
            FROM   action_log
            WHERE  app_name = ?
            ORDER  BY timestamp DESC
            LIMIT  ?
            """,
            (app_name, limit),
        ).fetchall()
    except Exception as exc:
        print(red(f"  Query failed: {exc}"))
        conn.close()
        return

    if not rows:
        print(yellow(f"\n  No decisions found for '{app_name}'."))
        print(dim("  Either the app hasn't been seen yet, or the collector isn't running."))
        conn.close()
        return

    section(f"Decision history for  {bold(app_name)}  (last {len(rows)})")
    for r in rows:
        ts, pid, label, conf, action, reason, shadow, enforcer_cmd = r
        conf = conf or 0.0

        # Action badge
        if action == "throttle":   badge = red(f"[THROTTLE]")
        elif action == "allow":    badge = green(f"[ALLOW   ]")
        else:                      badge = dim(f"[SKIP    ]")

        shadow_note = dim(" [shadow]") if shadow else ""

        print(f"\n  {bold(fmt_datetime(ts))}  {badge}{shadow_note}")
        print(f"  {dim('PID:')}      {pid or '—'}")
        print(f"  {dim('Label:')}    {label or '—'}", end="")
        if conf:
            conf_bar = bar(conf, width=12,
                           color_fn=green if conf > 0.9 else yellow if conf > 0.7 else red)
            print(f"    {conf_bar}  {conf:.3f}", end="")
        print()
        print(f"  {dim('Reason:')}   {reason or '—'}")
        if enforcer_cmd:
            print(f"  {dim('Cmd:')}      {cyan(enforcer_cmd)}")

    # ── Historical resource usage ─────────────────────────────────────────────
    section(f"Historical usage  (hourly aggregates)")
    try:
        hist = conn.execute(
            """
            SELECT hour_bucket, avg_cpu_pct, total_net_bytes, event_count
            FROM   events_hourly
            WHERE  app_name = ?
            ORDER  BY hour_bucket DESC
            LIMIT  24
            """,
            (app_name,),
        ).fetchall()
    except Exception:
        hist = []

    if not hist:
        print(dim("  (no hourly data yet)"))
    else:
        print(f"  {'TIME':<16}  {'AVG CPU':>8}  {'NET (KB)':>10}  {'EVENTS':>7}")
        print(dim("  " + "─" * 50))
        for bucket, cpu, net, evts in hist:
            t  = fmt_datetime(bucket)
            mb = f"{(net or 0) / 1024:.1f}"
            print(f"  {t:<16}  {cpu:>7.1f}%  {mb:>10}  {evts:>7}")

    # ── Correction factor ─────────────────────────────────────────────────────
    section("User corrections")
    try:
        corr = conn.execute(
            "SELECT correction_factor, observation_count, last_updated_ts "
            "FROM user_corrections WHERE app_name = ?",
            (app_name,),
        ).fetchone()
    except Exception:
        corr = None

    if corr:
        factor, obs, ts = corr
        factor_desc = (
            green("Protected (harder to throttle)")    if factor > 1.2 else
            red("Aggressive (easier to throttle)") if factor < 0.8 else
            dim("Default (no override)")
        )
        kv("correction_factor", f"{factor:.2f}  {factor_desc}")
        kv("observations",      str(obs))
        kv("last_updated",      fmt_datetime(ts) if ts else "—")
    else:
        print(dim("  No corrections set. Use:"))
        print(dim(f"    powerlayer override {app_name} --always-allow"))
        print(dim(f"    powerlayer override {app_name} --always-throttle"))
        print(dim(f"    powerlayer override {app_name} --reset"))

    print()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# override
# ─────────────────────────────────────────────────────────────────────────────

_FACTOR_ALWAYS_ALLOW    = 3.0   # boost active_needed probability
_FACTOR_ALWAYS_THROTTLE = 0.1   # suppress active_needed probability
_FACTOR_RESET           = 1.0   # back to default


def cmd_override(
    db_path: Path,
    app_name: str,
    mode: str,   # "always-allow" | "always-throttle" | "reset"
) -> None:
    """
    Set a per-app user correction.

    powerlayer override <app>  --always-allow      # protect from throttling
    powerlayer override <app>  --always-throttle   # be aggressive
    powerlayer override <app>  --reset             # back to model default
    """
    conn = _open_db(db_path)

    factor_map = {
        "always-allow":    (_FACTOR_ALWAYS_ALLOW,    green("Protected"), "↑ harder to throttle"),
        "always-throttle": (_FACTOR_ALWAYS_THROTTLE, red("Aggressive"),  "↓ easier to throttle"),
        "reset":           (_FACTOR_RESET,            dim("Default"),    "model decides"),
    }

    if mode not in factor_map:
        print(red(f"  Unknown mode '{mode}'. Use: always-allow | always-throttle | reset"))
        conn.close()
        sys.exit(1)

    factor, label_str, desc = factor_map[mode]

    try:
        conn.execute(
            """
            INSERT INTO user_corrections (app_name, correction_factor, observation_count,
                                          last_updated_ts)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(app_name) DO UPDATE SET
                correction_factor = excluded.correction_factor,
                observation_count = observation_count + 1,
                last_updated_ts   = excluded.last_updated_ts
            """,
            (app_name, factor, int(time.time())),
        )
        conn.commit()
        print()
        print(green("  ✓") + bold(f"  Override saved for  {app_name}"))
        kv("Mode",   f"{label_str}  ({desc})")
        kv("Factor", f"{factor}")
        print()
        print(dim("  Takes effect on next collector cycle (no restart needed)."))
        print(dim(f"  To view: powerlayer explain {app_name}"))

    except Exception as exc:
        print(red(f"  ✗ Failed to save override: {exc}"))
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# report
# ─────────────────────────────────────────────────────────────────────────────

def cmd_report(db_path: Path, hours: int = 24) -> None:
    """
    Battery savings and throttle activity report.

    powerlayer report [--hours N]
    """
    conn = _open_db(db_path)
    now  = int(time.time())
    since = now - hours * 3600

    header("Orbit  —  Report", f"Last {hours} hours  |  {fmt_datetime(since)} → now")

    # ── Throttle activity ─────────────────────────────────────────────────────
    section("Throttle Activity")
    try:
        totals = conn.execute(
            """
            SELECT
                COUNT(*)                                           AS total,
                SUM(CASE WHEN action_taken='throttle' THEN 1 ELSE 0 END) AS throttled,
                SUM(CASE WHEN action_taken='allow'    THEN 1 ELSE 0 END) AS allowed,
                SUM(CASE WHEN action_taken='skip'     THEN 1 ELSE 0 END) AS skipped,
                SUM(CASE WHEN shadow_mode=1           THEN 1 ELSE 0 END) AS shadow
            FROM action_log
            WHERE timestamp > ?
            """,
            (since,),
        ).fetchone()
    except Exception:
        totals = (0, 0, 0, 0, 0)

    total, throttled, allowed, skipped, shadow = totals
    total = total or 0
    kv("Total decisions",   str(total))
    kv("Throttled",         yellow(str(throttled or 0)))
    kv("Allowed",           green(str(allowed or 0)))
    kv("Skipped (low conf)",dim(str(skipped or 0)))
    kv("In shadow mode",    dim(str(shadow or 0)))

    if total > 0:
        throttle_rate = (throttled or 0) / total
        print(f"\n  Throttle rate:  {bar(throttle_rate, 24, yellow)}  {throttle_rate*100:.1f}%")

    # ── Most throttled apps ───────────────────────────────────────────────────
    section("Most Throttled Apps")
    try:
        top_throttled = conn.execute(
            """
            SELECT app_name, COUNT(*) as cnt
            FROM   action_log
            WHERE  timestamp > ? AND action_taken = 'throttle'
            GROUP  BY app_name
            ORDER  BY cnt DESC
            LIMIT  10
            """,
            (since,),
        ).fetchall()
    except Exception:
        top_throttled = []

    if not top_throttled:
        print(dim("  (no throttle actions in this window)"))
    else:
        max_cnt = top_throttled[0][1] if top_throttled else 1
        for app, cnt in top_throttled:
            b = bar(cnt / max_cnt, width=14, color_fn=yellow)
            print(f"  {bold(f'{app:<20}')}  {b}  {cnt} times")

    # ── Battery drain estimate ────────────────────────────────────────────────
    section("Battery Drain Estimate")
    try:
        b_start_row = conn.execute(
            "SELECT battery_pct FROM events WHERE timestamp > ? AND battery_pct IS NOT NULL ORDER BY timestamp ASC LIMIT 1",
            (since,)
        ).fetchone()
        b_end_row = conn.execute(
            "SELECT battery_pct FROM events WHERE timestamp > ? AND battery_pct IS NOT NULL ORDER BY timestamp DESC LIMIT 1",
            (since,)
        ).fetchone()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM events WHERE timestamp > ?",
            (since,)
        ).fetchone()[0]

        b_start = b_start_row[0] if b_start_row else None
        b_end = b_end_row[0] if b_end_row else None
    except Exception:
        b_start, b_end, cnt = None, None, 0

    if b_start is not None and b_end is not None and cnt > 0:
        drain = b_start - b_end
        rate  = drain / hours if hours > 0 else 0
        kv("Starting battery",  f"{b_start:.1f}%")
        kv("Ending battery",    f"{b_end:.1f}%")
        if drain > 0:
            kv("Total drain",    yellow(f"{drain:.1f}%"))
            kv("Avg drain/hour", f"{rate:.2f}%/h")
        else:
            kv("Total drain",    green("0% (charging/stable)"))
            kv("Avg drain/hour", "0.00%/h")
        kv("Events recorded",   str(cnt))
    else:
        print(dim("  (not enough battery data in this window)"))

    # ── Top resource hogs ─────────────────────────────────────────────────────
    section("Top CPU Consumers (avg, last window)")
    try:
        cpu_top = conn.execute(
            """
            SELECT app_name, AVG(cpu_pct) AS avg_cpu, COUNT(*) AS evts
            FROM   events
            WHERE  timestamp > ?
            GROUP  BY app_name
            ORDER  BY avg_cpu DESC
            LIMIT  8
            """,
            (since,),
        ).fetchall()
    except Exception:
        cpu_top = []

    if not cpu_top:
        print(dim("  (no data)"))
    else:
        for app, cpu, evts in cpu_top:
            b = bar(min(cpu / 100.0, 1.0), 16,
                    red if cpu > 60 else yellow if cpu > 25 else green)
            print(f"  {bold(f'{app:<20}')}  {b}  {cpu:5.1f}%  "
                  f"{dim(f'({int(evts)} samples)')}")

    # ── User corrections active ───────────────────────────────────────────────
    section("Active User Overrides")
    try:
        overrides = conn.execute(
            """
            SELECT app_name, correction_factor, observation_count, last_updated_ts
            FROM   user_corrections
            WHERE  observation_count > 0
            ORDER  BY last_updated_ts DESC
            """,
        ).fetchall()
    except Exception:
        overrides = []

    if not overrides:
        print(dim("  (no overrides set)"))
    else:
        for app, factor, obs, ts in overrides:
            badge = (green("protected") if factor > 1.2 else
                     red("aggressive") if factor < 0.8 else
                     dim("default"))
            print(f"  {bold(f'{app:<20}')}  factor={factor:.2f}  [{badge}]  "
                  f"{dim(fmt_age(ts) if ts else '')}")

    print()
    conn.close()
