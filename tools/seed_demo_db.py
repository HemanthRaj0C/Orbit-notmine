"""
tools/seed_demo_db.py
─────────────────────
Seeds the powerlayer DB with realistic demo data so the CLI commands
show meaningful output immediately.

What it inserts:
  - 200 realistic process events (last 6h)
  - 50 action_log decisions (throttle / allow / skip)
  - 3 user_corrections (one protect, one aggressive, one default)
  - 24 hourly aggregates for 4 common apps

Run once before testing the CLI:
    python tools/seed_demo_db.py
"""

from __future__ import annotations

import random
import sqlite3
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = _ROOT / "data" / "runtime" / "demo.db"

random.seed(42)
now = int(time.time())

APPS = {
    "dropbox":     {"category": "cloud_sync",       "base_cpu": 4.0,  "throttle_bias": 0.7},
    "chrome":      {"category": "browser",           "base_cpu": 22.0, "throttle_bias": 0.1},
    "code":        {"category": "development_tool",  "base_cpu": 18.0, "throttle_bias": 0.0},
    "spotify":     {"category": "streaming",         "base_cpu": 8.0,  "throttle_bias": 0.3},
    "zoom":        {"category": "communication",     "base_cpu": 35.0, "throttle_bias": 0.05},
    "systemd":     {"category": "system_utility",   "base_cpu": 0.5,  "throttle_bias": 0.0},
    "gnome-shell": {"category": "system_utility",   "base_cpu": 3.0,  "throttle_bias": 0.0},
    "updater":     {"category": "system_utility",   "base_cpu": 15.0, "throttle_bias": 0.9},
}


def seed(conn: sqlite3.Connection) -> None:
    print("Seeding demo data into DB...")

    # ── Events ────────────────────────────────────────────────────────────────
    event_rows = []
    for offset in range(0, 6 * 3600, 45):   # every 45s, 6h back → ~480 events
        ts = now - offset
        for app, meta in APPS.items():
            cpu = max(0.0, meta["base_cpu"] + random.gauss(0, meta["base_cpu"] * 0.3))
            bat = max(10.0, min(100.0, 82.0 - offset / 3600 * 4 + random.gauss(0, 0.5)))
            net = int(random.expovariate(1 / 50000)) if meta["category"] == "cloud_sync" else 0
            event_rows.append((ts, app, random.randint(1000, 9999),
                                "snapshot", cpu, net, bat))

    conn.executemany(
        "INSERT OR IGNORE INTO events "
        "(timestamp, app_name, pid, event_type, cpu_pct, net_bytes, battery_pct) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        event_rows,
    )
    print(f"  ✓ Inserted {len(event_rows)} events")

    # ── Hourly aggregates ─────────────────────────────────────────────────────
    hourly_rows = []
    for h in range(24):
        bucket = (now // 3600 - h) * 3600
        for app, meta in APPS.items():
            avg_cpu = meta["base_cpu"] + random.gauss(0, 2.0)
            total_net = random.randint(0, 5_000_000) if "sync" in meta["category"] else 0
            hourly_rows.append((bucket, app, avg_cpu, total_net, 0.12, 48))

    conn.executemany(
        "INSERT OR IGNORE INTO events_hourly "
        "(hour_bucket, app_name, avg_cpu_pct, total_net_bytes, avg_battery_drain, event_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        hourly_rows,
    )
    print(f"  ✓ Inserted {len(hourly_rows)} hourly buckets")

    # ── Action log ────────────────────────────────────────────────────────────
    action_rows = []
    for i in range(60):
        ts = now - i * 180   # one decision every 3 min
        app = random.choice(list(APPS.keys()))
        meta = APPS[app]
        bias = meta["throttle_bias"]
        r = random.random()
        if r < bias:
            action, label, conf = "throttle", "idle_likely", round(random.uniform(0.91, 0.99), 3)
        elif r < bias + 0.3:
            action, label, conf = "allow", "active_needed", round(random.uniform(0.85, 0.99), 3)
        else:
            action, label, conf = "skip", "idle_likely", round(random.uniform(0.55, 0.89), 3)

        action_rows.append((ts, app, label, action, conf, 1, 80.0 - i * 0.1, None, 0, None))

    # Detect schema (old has 'prediction'/'action', new has 'predicted_label'/'action_taken')
    al_cols = [r[1] for r in conn.execute("PRAGMA table_info(action_log)").fetchall()]
    if "predicted_label" in al_cols:
        al_sql = """INSERT OR IGNORE INTO action_log
                    (timestamp, app_name, predicted_label, action_taken, confidence,
                     shadow_mode, battery_before, battery_after, reverted, enforcer_cmd)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    else:
        al_sql = """INSERT OR IGNORE INTO action_log
                    (timestamp, app_name, prediction, action, confidence,
                     shadow_mode, battery_before, battery_after, reverted, enforcer_cmd)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    conn.executemany(al_sql, action_rows)
    print(f"  ✓ Inserted {len(action_rows)} action_log decisions")

    # ── User corrections ──────────────────────────────────────────────────────
    overrides = [
        ("spotify",  3.0, "Protected  — user wants music to never be throttled"),
        ("updater",  0.1, "Aggressive — background updater should always be throttled"),
    ]
    for app, factor, _ in overrides:
        conn.execute(
            """INSERT INTO user_corrections (app_name, correction_factor, observation_count, last_updated_ts)
               VALUES (?, ?, 5, ?)
               ON CONFLICT(app_name) DO UPDATE SET
                 correction_factor = excluded.correction_factor,
                 last_updated_ts   = excluded.last_updated_ts""",
            (app, factor, now - 600),
        )
    print(f"  ✓ Inserted {len(overrides)} user corrections")


    conn.commit()
    print("\n✓ Seed complete. Run the CLI now:")
    print("  python cli/__init__.py status")
    print("  python cli/__init__.py report")
    print("  python cli/__init__.py explain dropbox")
    print("  python cli/__init__.py explain spotify")
    print("  python cli/__init__.py override zoom --always-allow")


if __name__ == "__main__":
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        schema_path = _ROOT / "storage" / "schema.sql"
        conn = sqlite3.connect(str(DB_PATH))
        conn.executescript(schema_path.read_text())
        conn.commit()
        conn.close()
        print(f"Initialized database with schema at {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    seed(conn)
    conn.close()
