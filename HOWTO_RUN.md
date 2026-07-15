# PowerLayer — How to Run Everything

> **All commands are run from inside the `powerlayer/` directory.**
> ```bash
> cd /home/hemanth/Ccp_CSBS/powerlayer
> ```

---

## Step 0 — Install dependencies (once only)

```bash
pip install -r requirements.txt
```

Takes ~30–60 seconds on first run. You only need to do this once.

---

## Step 1 — Run the tests

### Storage tests (9 tests)
```bash
python tests/test_storage.py
```
**What it tests:** SQLite connection, BatchedWriter flushing 1000 rows in <100ms,
aggregation moving old rows to hourly buckets, idempotency, weighted merging.

**Expected output:**
```
═══════════════════════════════════════
  PowerLayer — Storage Tests
═══════════════════════════════════════
▶ test_batched_writer_single_flush
  ✓ 1000 rows flushed in 43ms
...
  Results: 9 passed, 0 failed
```
**How long:** ~5 seconds. No input needed, just run and read.

---

### Collector tests (15 tests)
```bash
python tests/test_collector.py
```
**What it tests:** Live process scanning (psutil), PSI pressure readings
from `/sys/fs/cgroup/`, battery detection from `/sys/class/power_supply/`,
network interface detection, idle time detection.

**Expected output (your machine's real values):**
```
▶ test_detect_battery_path
  ✓ Battery found: BAT0  capacity=100.0%
▶ test_detect_network_interface
  ✓ Network interface: wlo1
▶ test_psi_returns_none_or_dict
  ✓ CPU PSI: avg10=0.72  avg60=0.34
...
  Results: 15 passed, 0 failed
```
**How long:** ~5–10 seconds. No input needed.

---

## Step 2 — Demo dashboard

There are **two modes**. Pick one depending on what you want to show.

---

### Mode A: Simulation (fake data, shows pipeline)

```bash
python tools/demo_live.py --sim --fast
```

**What it shows:** Fake app names (firefox, spotify, slack, zoom…) with random
CPU/battery values flowing through the pipeline. The point is to see the
**buffer → flush → SQLite** pipeline in action, fast.

**What to look for, step by step:**

| Time | What you see |
|------|-------------|
| 0s   | Dashboard appears. Buffer starts at 0/20. |
| 2–4s | Buffer fills: 5→10→15→20 rows (watch the `█` bar grow) |
| ~4s  | **AUTO-FLUSH fires** — buffer hits 20 rows → written to SQLite in one transaction |
| 4s+  | `Flush History` shows `wrote 20 rows → DB now has 20 raw events` |
| 6–8s | Buffer refills, another flush at 20 rows |
| Any time | Recent Events table populates with fake app data |

**How long to run:** 20–30 seconds is enough to see 2–3 flushes happen.
Press **Ctrl+C** to stop.

---

### Mode B: Real collector (your actual system)

```bash
python tools/demo_live.py
```

**What it shows:** Your real running processes (kworker, python3, code, pipewire…),
actual battery %, actual PSI (CPU pressure). The collector scans ~374 processes
every 7 seconds and buffers the active ones.

**What to look for, step by step:**

| Time | What you see |
|------|-------------|
| 0s   | Dashboard starts. **Collector Stats** shows Cycles=0. |
| 3–5s | Cycles=1, Active procs=370+, Battery=100%, PSI shows a value |
| 5–10s | Buffer starts filling (you can see `12/100`, `35/100`…) |
| ~30s | **First flush fires** — 60–80 rows written to `sandbox.db` |
| 30s+ | `Flush History` shows `wrote 64 rows → DB now has 164 raw events` |
| 30s+ | Recent Events table shows your actual processes with real CPU% |

> ⚠️ **Why does it say "Buffer filling — not flushed to DB yet" for the first ~30s?**
>
> The real collector flushes every **30 seconds** OR every **100 rows**, whichever
> comes first. Your system is not that busy, so it usually hits the 30s timer first.
> Just wait — the flush will happen.

> ⚠️ **Why does DB Size show 4,096 bytes even after flushing?**
>
> SQLite allocates pages in 4KB blocks. After the first flush it will jump to
> the next page size. The actual row count in `events (raw, <48h)` is the real indicator.

**How long to run:** Run for at least **45–60 seconds** to see the full cycle:
buffer filling → flush → recent events appearing. Press **Ctrl+C** to stop.

**DB location:** `data/runtime/sandbox.db` — this persists between runs. You can
inspect it directly:
```bash
sqlite3 data/runtime/sandbox.db "SELECT app_name, cpu_pct, event_type FROM events ORDER BY timestamp DESC LIMIT 10;"
```

---

## Quick reference — all commands

```bash
# ── Tests ────────────────────────────────────────────────────────
python tests/test_storage.py       # 9 tests, ~5s
python tests/test_collector.py     # 15 tests, ~5s

# ── Demo dashboard ───────────────────────────────────────────────
python tools/demo_live.py --sim --fast   # fake data, fast flushes, good for pipeline demo
python tools/demo_live.py --sim          # fake data, slower (30s flush timer)
python tools/demo_live.py                # real system data (wait 30s for first flush)

# ── Collector (standalone, runs until Ctrl+C) ────────────────────
python -m collector.monitor              # runs the full collector with live logging
python -m collector.monitor --debug      # verbose DEBUG output

# ── Inspect the database directly ───────────────────────────────
sqlite3 data/runtime/sandbox.db ".tables"
sqlite3 data/runtime/sandbox.db "SELECT COUNT(*) FROM events;"
sqlite3 data/runtime/sandbox.db "SELECT app_name, cpu_pct, event_type, battery_pct FROM events ORDER BY timestamp DESC LIMIT 15;"
```

---

## What each dashboard section means

```
[Collector] ─→ [Buffer] ─→ [SQLite WAL] ─→ [events_hourly]
```

| Section | What it is |
|---------|-----------|
| **[Collector]** | `monitor.py` scanning processes every 7s (active) or 45s (idle) |
| **[Buffer]** | In-memory Python list inside `BatchedWriter`. Never hits disk until flush. |
| **[SQLite WAL]** | The `events` table in `sandbox.db`. Written in one atomic transaction per flush. |
| **[events_hourly]** | Where rows go after 48h — raw data deleted, hourly averages kept. Keeps DB bounded. |

| Dashboard field | Meaning |
|----------------|---------|
| **Buffer level** | How full the in-memory buffer is (0→100 rows) |
| **Last flush** | When the last batch write to SQLite happened |
| **Next flush** | Countdown to the next scheduled flush (30s cadence) |
| **Flush count** | How many batch writes have happened total |
| **events (raw)** | Rows in the `events` table (grows after each flush) |
| **events_hourly** | Aggregated hourly buckets (starts empty — runs after 48h) |
| **PSI cpu avg10** | CPU pressure stall % over last 10s (from `/sys/fs/cgroup/cpu.pressure`) |

---

## Files built so far

```
powerlayer/
├── storage/
│   ├── schema.sql          # 4 tables: events, events_hourly, user_corrections, action_log
│   ├── db.py               # SQLite connection (WAL mode) + BatchedWriter
│   └── aggregator.py       # Rolls up events >48h old into hourly buckets
├── collector/
│   ├── proc_reader.py      # psutil process snapshots + idle detection
│   ├── psi_reader.py       # /sys/fs/cgroup pressure readings
│   └── monitor.py          # Main adaptive polling loop + watchdog FS events
├── tests/
│   ├── test_storage.py     # 9 tests
│   └── test_collector.py   # 15 tests
├── tools/
│   └── demo_live.py        # Live dashboard (this guide)
└── config.yaml             # All tunable settings (shadow_mode, intervals, etc.)
```

---

## Step 5 — Install as a system service (permanent, auto-starts on boot)

Run the one-command installer:
```bash
./install.sh
```

What it does (cross-distro: Fedora, Ubuntu, Arch, Debian):
1. Detects Python 3.9+
2. Installs Python dependencies (`requirements.txt`)
3. Trains the ML model if not already done
4. Registers two **user** systemd services:
   - `powerlayer` — background collector + policy enforcement daemon
   - `powerlayer-tray` — desktop tray icon indicator
5. Grants passwordless `sudo tc` for network throttling
6. Creates the global `powerlayer` CLI command at `/usr/local/bin/powerlayer`

After install, the daemon starts immediately and auto-starts on every login.

---

## Step 6 — System tray icon (desktop integration)

After installing, a **⚡ PowerLayer** icon appears in your system tray:
- **GNOME** — requires the AppIndicator extension:
  https://extensions.gnome.org/extension/615/appindicator-support/
- **KDE Plasma** — works natively
- **XFCE / MATE / Cinnamon** — works natively

The tray icon shows:
- 🔋 Current battery %
- 🚦 Apps being throttled right now
- Quick actions: Status dashboard, Report, Shadow mode toggle, Restart daemon

Run the tray manually (for testing):
```bash
python cli/tray.py --db data/runtime/sandbox.db
```

---

## Step 7 — Service management

```bash
# Check if daemon is running
systemctl --user status powerlayer

# Live logs
journalctl --user -u powerlayer -f

# Stop / restart
systemctl --user stop powerlayer
systemctl --user restart powerlayer

# Disable auto-start
systemctl --user disable powerlayer powerlayer-tray
```

---

## Step 8 — Battery benchmark (Phases A/B)

Measure actual battery savings before vs. after PowerLayer:
```bash
# Full A/B benchmark (10 minutes per phase, unplug charger first)
python evaluation/benchmark.py

# Shorter test (5-minute phases)
python evaluation/benchmark.py --duration 5

# Skip baseline, only measure active phase
python evaluation/benchmark.py --skip-baseline

# Re-generate report from last saved run
python evaluation/benchmark.py --report-only
```

Results are saved to:
- `data/runtime/benchmark_results.json`   — raw data
- `data/runtime/evaluation_report.md`     — formatted Markdown report

---

## Project structure (final)

```
powerlayer/
├── assets/icons/            # Tray icon PNGs (active/idle/throttle)
├── cli/
│   ├── __init__.py          # status / report / explain / override commands
│   └── tray.py              # GTK3 AppIndicator desktop tray icon
├── collector/               # proc_reader + psi_reader + monitor
├── enforcer/                # cgroup CPU throttle + tc network throttle
├── evaluation/
│   └── benchmark.py         # A/B battery drain test + Markdown report
├── model/                   # Random Forest + correction layer
├── policy/                  # PolicyEngine (decide + whitelist)
├── scripts/
│   ├── powerlayer.service       # systemd user unit (daemon)
│   ├── powerlayer-tray.service  # systemd user unit (tray)
│   └── train_base_model.py      # offline model training
├── storage/                 # schema.sql + BatchedWriter + aggregator
├── tests/                   # full test suite (111 tests)
├── tools/
│   ├── demo_live.py             # simulation dashboard
│   ├── powerlayer_daemon.py  # production daemon (ML + enforcement)
│   └── seed_demo_db.py          # seed demo data
├── config.yaml              # all tunable settings
├── install.sh               # cross-distro installer ← run this
└── HOWTO_RUN.md             # this file
```

