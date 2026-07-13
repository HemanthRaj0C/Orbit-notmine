-- PowerLayer SQLite Schema
-- All tables use INTEGER timestamps (Unix epoch seconds).
-- This file is executed once at startup by db.py if tables don't exist.

-- ─────────────────────────────────────────────────────────────────────────────
-- Raw per-process events (high-frequency, retained max 48 hours)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   INTEGER NOT NULL,       -- Unix epoch seconds
    app_name    TEXT    NOT NULL,       -- process name (e.g. "firefox")
    pid         INTEGER,                -- PID at time of snapshot
    event_type  TEXT    NOT NULL,       -- 'foreground' | 'sync' | 'network' | 'wake' | 'idle'
    cpu_pct     REAL    DEFAULT 0.0,    -- cpu_percent() from psutil
    net_bytes   INTEGER DEFAULT 0,      -- total io bytes (rx+tx delta) in this interval
    battery_pct REAL    DEFAULT NULL    -- system battery % at snapshot time (NULL on AC-only)
);

CREATE INDEX IF NOT EXISTS idx_events_time    ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_app     ON events(app_name);
CREATE INDEX IF NOT EXISTS idx_events_app_ts  ON events(app_name, timestamp);

-- ─────────────────────────────────────────────────────────────────────────────
-- Hourly aggregated data (permanent long-term store, replaces raw events >48h)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events_hourly (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    hour_bucket         INTEGER NOT NULL,   -- Unix epoch of the hour floor (e.g. 1720000000)
    app_name            TEXT    NOT NULL,
    avg_cpu_pct         REAL    DEFAULT 0.0,
    total_net_bytes     INTEGER DEFAULT 0,
    avg_battery_drain   REAL    DEFAULT NULL,
    event_count         INTEGER DEFAULT 0,
    UNIQUE(hour_bucket, app_name)           -- prevent duplicate aggregation runs
);

CREATE INDEX IF NOT EXISTS idx_hourly_app  ON events_hourly(app_name);
CREATE INDEX IF NOT EXISTS idx_hourly_time ON events_hourly(hour_bucket);

-- ─────────────────────────────────────────────────────────────────────────────
-- Per-app user correction bias (the personalization layer — no retraining)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_corrections (
    app_name            TEXT    PRIMARY KEY,
    user_bias           REAL    DEFAULT 0.0,    -- added to base model confidence, clamped [0,1]
    observation_count   INTEGER DEFAULT 0,      -- how many EWM updates have been applied
    last_updated        INTEGER                  -- Unix epoch of last update
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Full audit log of every policy decision (powers `powerlayer explain <app>`)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS action_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       INTEGER NOT NULL,
    app_name        TEXT    NOT NULL,
    prediction      TEXT,               -- 'idle_likely' | 'background_unused' | 'active_needed'
    action          TEXT    NOT NULL,   -- 'throttle_sync' | 'batch_notifications' | 'no_action'
    confidence      REAL,               -- corrected confidence score at decision time
    shadow_mode     INTEGER DEFAULT 1,  -- 1 = was in shadow mode (enforce not called), 0 = enforced
    battery_before  REAL,               -- battery % before action
    battery_after   REAL,               -- battery % after next measurement cycle
    reverted        INTEGER DEFAULT 0,  -- 1 if user called `powerlayer override <app>`
    enforcer_cmd    TEXT                -- exact tc/cgroup command that was run (for explain)
);

CREATE INDEX IF NOT EXISTS idx_action_app  ON action_log(app_name);
CREATE INDEX IF NOT EXISTS idx_action_time ON action_log(timestamp);
