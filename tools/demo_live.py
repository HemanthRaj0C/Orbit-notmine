"""
tools/demo_live.py
──────────────────
Live terminal dashboard for your project mentor.

Shows — in real time — exactly what PowerLayer is doing:
  • The collector scanning processes
  • Rows accumulating in the in-memory buffer
  • The BatchedWriter flushing to SQLite
  • DB growing (events table row count)
  • Battery & PSI readings

Run from the powerlayer/ directory:
    python tools/demo_live.py             # real collector (live system data)
    python tools/demo_live.py --sim       # simulation using YOUR real proc names
    python tools/demo_live.py --sim --fast # super-fast for a quick demo

Press Ctrl+C to exit cleanly.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import threading
import time
from pathlib import Path
from datetime import datetime

# ── Make project importable ───────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from storage.db import get_connection, BatchedWriter
from storage.aggregator import get_stats

# ── ANSI colour helpers ───────────────────────────────────────────────────────

RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
RED     = "\033[31m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
BLUE    = "\033[34m"
MAGENTA = "\033[35m"
CYAN    = "\033[36m"


def c(text: str, *codes: str) -> str:
    return "".join(codes) + str(text) + RESET


def clear() -> None:
    os.system("clear")


def bar(value: int, maximum: int, width: int = 30,
        fill: str = "█", empty: str = "░") -> str:
    pct = min(1.0, value / maximum) if maximum else 0.0
    filled = int(pct * width)
    colour = GREEN if pct < 0.6 else YELLOW if pct < 0.85 else RED
    return c(fill * filled + empty * (width - filled), colour) + f" {value}/{maximum}"


def ago(ts: float) -> str:
    if ts == 0:
        return "—"
    delta = int(time.time() - ts)
    return f"{delta}s ago" if delta < 60 else f"{delta//60}m {delta%60}s ago"


def seconds_until(ts: float, interval: float) -> str:
    if ts == 0:
        return "—"
    return f"in {max(0, int(interval - (time.time() - ts)))}s"


# ─────────────────────────────────────────────────────────────────────────────
# Simulation helpers  (intentionally fake — shows pipeline mechanics)
# ─────────────────────────────────────────────────────────────────────────────

# Hardcoded demo app names — clearly simulated, not your real processes.
# This is intentional: sim mode shows the PIPELINE, real mode shows YOUR system.
FAKE_APPS = [
    "firefox", "code", "python3", "spotify", "slack",
    "chrome", "terminal", "dropbox", "zoom", "vlc",
    "electron", "node", "git",
]

FAKE_EVENTS = ["foreground", "idle", "sync", "network"]


def _sim_worker(writer: BatchedWriter, state: dict,
                stop: threading.Event, fast: bool) -> None:
    """Generate obviously fake events to demonstrate the pipeline mechanics."""
    interval = 0.3 if fast else 1.2
    while not stop.is_set():
        app   = random.choice(FAKE_APPS)
        event = random.choice(FAKE_EVENTS)
        cpu   = round(random.uniform(0.1, 45.0), 1)
        batt  = round(random.uniform(60.0, 95.0), 1)
        writer.add({
            "timestamp":   int(time.time()),
            "app_name":    app,
            "pid":         random.randint(1000, 9999),
            "event_type":  event,
            "cpu_pct":     cpu,
            "net_bytes":   random.randint(0, 50000),
            "battery_pct": batt,
        })
        state.update({"last_app": app, "last_event": event,
                      "last_cpu": cpu, "last_batt": batt})
        state["sim_rows"] = state.get("sim_rows", 0) + 1
        stop.wait(timeout=interval)


# ─────────────────────────────────────────────────────────────────────────────
# TrackedBatchedWriter — sim mode only (wraps flush to log events)
# ─────────────────────────────────────────────────────────────────────────────

class TrackedBatchedWriter(BatchedWriter):
    def __init__(self, conn, flush_interval, batch_size,
                 state, flush_log, conn_ref):
        super().__init__(conn, flush_interval=flush_interval, batch_size=batch_size)
        self._state     = state
        self._flush_log = flush_log
        self._conn_ref  = conn_ref

    def flush(self) -> int:
        n = super().flush()
        if n > 0:
            self._state["total_flushed"] = self._state.get("total_flushed", 0) + n
            self._state["flush_count"]   = self._state.get("flush_count",   0) + 1
            self._state["last_flush_ts"] = time.time()
            try:
                db_total = self._conn_ref.execute(
                    "SELECT COUNT(*) FROM events"
                ).fetchone()[0]
            except Exception:
                db_total = "?"
            self._flush_log.append({
                "time":     datetime.now().strftime("%H:%M:%S"),
                "rows":     n,
                "db_total": db_total,
            })
        return n


# ─────────────────────────────────────────────────────────────────────────────
# Real collector starter
# ─────────────────────────────────────────────────────────────────────────────

def _real_worker(monitor_ref: dict) -> None:
    """Start the real Monitor in a daemon thread."""
    from collector.monitor import Monitor, _setup_logging
    _setup_logging("WARNING", None)   # keep demo output clean

    mon = Monitor.from_config_file(_ROOT / "config.yaml")
    monitor_ref["monitor"] = mon

    def _run():
        try:
            mon.start()
        except Exception:
            import traceback
            traceback.print_exc()

    t = threading.Thread(target=_run, name="powerlayer-monitor", daemon=True)
    t.start()
    monitor_ref["thread"] = t


def _flush_poll_worker(conn, state: dict, flush_log: list,
                       stop: threading.Event) -> None:
    """
    Detects flushes in real mode by polling the events table row count.
    When count grows, a flush happened.
    """
    prev = 0
    while not stop.wait(timeout=2.0):
        try:
            cur = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            if cur > prev and prev > 0:
                delta = cur - prev
                state["total_flushed"] = state.get("total_flushed", 0) + delta
                state["flush_count"]   = state.get("flush_count",   0) + 1
                state["last_flush_ts"] = time.time()
                flush_log.append({
                    "time":     datetime.now().strftime("%H:%M:%S"),
                    "rows":     delta,
                    "db_total": cur,
                })
            prev = cur
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard renderer (one frame)
# ─────────────────────────────────────────────────────────────────────────────

def _render(*, mode: str, writer: BatchedWriter, conn,
            state: dict, flush_log: list, monitor,
            batch_size: int, flush_interval: float, db_path: Path) -> None:
    clear()
    W = 72

    # Header
    print(c("═" * W, CYAN))
    title = "⚡  POWERLAYER — Live Pipeline Demo"
    print(c(" " * ((W - len(title)) // 2) + title, BOLD, CYAN))
    print(f"   Mode: {c(mode.upper(), YELLOW, BOLD)}  │  {datetime.now().strftime('%H:%M:%S')}")
    print(c("═" * W, CYAN))

    # DB file info
    db_size = db_path.stat().st_size if db_path.exists() else 0
    db_size_str = (f"{db_size:,} bytes" if db_size < 1024 * 1024
                   else f"{db_size / 1024:.1f} KB")
    print()
    print(f"  {c('📁 DB File:', BOLD)}  {c(str(db_path), DIM)}")
    print(f"  {c('💾 DB Size:', BOLD)}  {c(db_size_str, GREEN)}")

    # Pipeline diagram
    print()
    print(c("  ── PIPELINE ─────────────────────────────────────────────────────", CYAN))
    print()

    with writer._lock:
        buf_size = len(writer._buffer)
    buf_pct   = min(1.0, buf_size / batch_size) if batch_size else 0
    buf_color = GREEN if buf_pct < 0.6 else YELLOW if buf_pct < 0.9 else RED

    print(
        f"  {c('[Collector]', BOLD, MAGENTA)}  ─→  "
        f"{c('[Buffer]', BOLD, buf_color)}  ─→  "
        f"{c('[SQLite WAL]', BOLD, GREEN)}  ─→  "
        f"{c('[events_hourly]', BOLD, BLUE)}"
    )
    print(f"              {c(f'{buf_size} rows', buf_color)}"
          f"         raw events          aggregated")
    print(f"       {c(f'(auto-flush >{batch_size} rows  or every {int(flush_interval)}s)', DIM)}")

    # Buffer status
    print()
    print(c("  ── BUFFER STATUS ────────────────────────────────────────────────", CYAN))
    print()
    print(f"  Buffer level:  {bar(buf_size, batch_size, width=28)}")
    last_flush   = state.get("last_flush_ts", 0)
    rows_flushed = state.get("total_flushed", 0)
    print(f"  Last flush:    {c(ago(last_flush), YELLOW)}   "
          f"│  Total flushed: {c(str(rows_flushed), GREEN)} rows")
    print(f"  Next flush:    {c(seconds_until(last_flush, flush_interval), CYAN)}   "
          f"│  Flush count:   {c(str(state.get('flush_count', 0)), GREEN)}")

    # Flush history
    print()
    print(c("  ── FLUSH HISTORY (last 4) ───────────────────────────────────────", CYAN))
    if not flush_log:
        print(f"  {c('  Waiting for first flush…', DIM)}")
    else:
        for entry in flush_log[-4:]:
            print(f"  {c(entry['time'], DIM)}  "
                  f"wrote {c(str(entry['rows']), GREEN)} rows  "
                  f"→  DB now has {c(str(entry['db_total']), CYAN)} raw events")

    # DB stats
    print()
    print(c("  ── DATABASE STATS ───────────────────────────────────────────────", CYAN))
    print()
    try:
        raw     = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        hourly  = conn.execute("SELECT COUNT(*) FROM events_hourly").fetchone()[0]
        actions = conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
        corrs   = conn.execute(
            "SELECT COUNT(*) FROM user_corrections WHERE observation_count > 0"
        ).fetchone()[0]
    except Exception:
        raw = hourly = actions = corrs = "?"

    print(f"  {c('events (raw, <48h):', BOLD):40s}  {c(str(raw), CYAN)}")
    print(f"  {c('events_hourly (aggregated):', BOLD):40s}  {c(str(hourly), BLUE)}")
    print(f"  {c('action_log (decisions):', BOLD):40s}  {c(str(actions), MAGENTA)}")
    print(f"  {c('user_corrections (apps learned):', BOLD):40s}  {c(str(corrs), GREEN)}")

    # Recent events
    print()
    print(c("  ── RECENT EVENTS (from DB) ──────────────────────────────────────", CYAN))
    print()
    print(f"  {c('TIME    ', BOLD)}"
          f"{c('APP              ', BOLD)}"
          f"{c('CPU%  ', BOLD)}"
          f"{c('EVENT       ', BOLD)}"
          f"{c('BATTERY', BOLD)}")
    print(c("  " + "─" * 58, DIM))
    try:
        rows = conn.execute(
            "SELECT timestamp, app_name, cpu_pct, event_type, battery_pct "
            "FROM events ORDER BY timestamp DESC, id DESC LIMIT 7"
        ).fetchall()
    except Exception:
        rows = []

    if not rows:
        print(f"  {c('  Buffer filling — not flushed to DB yet…', DIM)}")
    else:
        for ts, app, cpu, evt, batt in rows:
            t_str   = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            app_str = (app or "?")[:16].ljust(16)
            cpu_str = f"{cpu:5.1f}%"
            evt_col = MAGENTA if evt == "foreground" else CYAN if evt == "sync" else DIM
            bat_str = f"{batt:.1f}%" if batt is not None else "AC"
            print(f"  {c(t_str, DIM)}  {app_str}  {c(cpu_str, GREEN)}  "
                  f"{c((evt or '?')[:10].ljust(10), evt_col)}  {bat_str}")

    # Collector stats (real mode) or sim info
    if monitor is not None:
        s = monitor.stats
        print()
        print(c("  ── COLLECTOR STATS ──────────────────────────────────────────────", CYAN))
        print(f"  Cycles run:      {c(str(s.get('cycles', 0)), GREEN)}")
        print(f"  Active procs:    {c(str(s.get('active_procs', 0)), GREEN)}")
        interval_val = s.get("current_interval", "?")
        print(f"  Poll interval:   {c(str(interval_val) + 's', YELLOW)}")
        batt = s.get("battery_pct")
        print(f"  Battery:         {c(f'{batt:.1f}%' if batt else 'AC / N/A', CYAN)}")
        psi  = s.get("psi_cpu_avg10")
        print(f"  PSI cpu avg10:   {c(f'{psi:.2f}' if psi is not None else 'N/A', CYAN)}")
    else:
        print()
        print(c("  ── SIMULATION STATUS ────────────────────────────────────────────", CYAN))
        la = state.get("last_app", "—")
        le = state.get("last_event", "—")
        lc = state.get("last_cpu", 0)
        print(f"  Sim rows generated:  {c(str(state.get('sim_rows', 0)), GREEN)}")
        print(f"  Last event:          {c(la, MAGENTA)}  [{le}]  cpu={lc}%")

    print()
    print(c("─" * W, DIM))
    print(c("  Press Ctrl+C to stop", DIM))


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="PowerLayer live demo dashboard."
    )
    parser.add_argument("--sim",  action="store_true",
                        help="Simulate with real proc names + fake metrics")
    parser.add_argument("--fast", action="store_true",
                        help="Faster simulation cadence")
    parser.add_argument("--refresh", type=float, default=2.0,
                        help="Dashboard refresh interval in seconds (default: 2)")
    args = parser.parse_args()

    stop_event = threading.Event()
    state:     dict = {"sim_rows": 0, "last_flush_ts": 0.0}
    flush_log: list = []
    monitor = None

    # ── SIM MODE ──────────────────────────────────────────────────────────────
    if args.sim:
        FLUSH_INTERVAL = 15.0 if args.fast else 30.0
        BATCH_SIZE     = 20   if args.fast else 100
        DB_PATH        = _ROOT / "data" / "demo.db"
        mode           = "simulation  (fake apps, fake metrics)"

        # Fresh DB every run
        for suffix in ("", "-shm", "-wal"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = get_connection(DB_PATH)

        writer = TrackedBatchedWriter(
            conn, FLUSH_INTERVAL, BATCH_SIZE, state, flush_log, conn
        )
        writer.start()

        threading.Thread(
            target=_sim_worker,
            args=(writer, state, stop_event, args.fast),
            daemon=True,
        ).start()

    # ── REAL COLLECTOR MODE ────────────────────────────────────────────────────
    else:
        mode = "real collector  (live system data)"
        monitor_ref: dict = {}
        try:
            _real_worker(monitor_ref)
            # Wait up to 3s for the monitor to open its DB connection
            for _ in range(30):
                time.sleep(0.1)
                mon = monitor_ref.get("monitor")
                if mon and mon._conn and mon._writer:
                    break
        except ImportError as e:
            print(f"\n⚠  Could not import collector: {e}")
            print("   Try: python tools/demo_live.py --sim\n")
            sys.exit(1)

        monitor = monitor_ref.get("monitor")
        if not monitor or not monitor._conn:
            print("\n⚠  Monitor failed to initialize. Try --sim mode.\n")
            sys.exit(1)

        # !! Key fix: use the monitor's own connection + writer !!
        conn           = monitor._conn
        writer         = monitor._writer
        BATCH_SIZE     = int(monitor._scfg.get("flush_batch_size", 100))
        FLUSH_INTERVAL = float(monitor._scfg.get("flush_interval_seconds", 30))

        db_cfg = monitor._scfg.get("db_path", "data/powerlayer.db")
        DB_PATH = (Path(db_cfg) if Path(db_cfg).is_absolute()
                   else _ROOT / db_cfg)

        # Poll thread detects flushes by watching DB row count delta
        threading.Thread(
            target=_flush_poll_worker,
            args=(conn, state, flush_log, stop_event),
            daemon=True,
        ).start()

    # ── Dashboard loop ─────────────────────────────────────────────────────────
    print(f"\n  Starting PowerLayer demo ({mode.split('(')[0].strip()})…\n")
    time.sleep(0.5)

    try:
        while True:
            _render(
                mode=mode, writer=writer, conn=conn,
                state=state, flush_log=flush_log, monitor=monitor,
                batch_size=BATCH_SIZE, flush_interval=FLUSH_INTERVAL,
                db_path=DB_PATH,
            )
            time.sleep(args.refresh)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\n{c('  Stopping…', YELLOW)}")
        stop_event.set()
        if monitor is not None:
            monitor.stop()
        elif args.sim:
            writer.stop()

        try:
            stats = get_stats(conn)
            print()
            print(c("  ── FINAL SUMMARY ────────────────────────────────────────", CYAN))
            print(f"  Raw events in DB:    {c(str(stats['raw_events']), GREEN)}")
            print(f"  Hourly buckets:      {c(str(stats['hourly_buckets']), BLUE)}")
            print(f"  Total flushed:       {c(str(state.get('total_flushed', 0)), GREEN)}")
            print(f"  Flush count:         {c(str(state.get('flush_count', 0)), GREEN)}")
            print(f"  DB location:         {c(str(DB_PATH), DIM)}")
        except Exception:
            pass
        if args.sim:
            conn.close()
        print(c("\n  Demo ended cleanly. ✓\n", GREEN))


if __name__ == "__main__":
    main()
