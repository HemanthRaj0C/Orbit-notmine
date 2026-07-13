"""
collector/monitor.py
────────────────────
Main event-driven collector for PowerLayer.

Architecture:
  ┌──────────────────────────────────────────────────────┐
  │  Main Loop (adaptive interval)                       │
  │    ↓  snapshot_processes()                           │
  │    ↓  read_battery / read_psi                        │
  │    ↓  writer.add(row)  ← in-memory buffer            │
  │    ↓  BatchedWriter.flush() every 30s / 100 rows     │
  │    ↓  SQLite WAL write                               │
  └──────────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────────┐
  │  Watchdog Thread (inotify-based)                     │
  │    watches ~/Dropbox, ~/.config, etc.                │
  │    on FS event → sets _fs_event_flag → poll drops   │
  │    to fast interval immediately                      │
  └──────────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────────┐
  │  Aggregator (every 1h inside main loop)              │
  │    moves events >48h → events_hourly, deletes raw    │
  └──────────────────────────────────────────────────────┘

Cross-distro notes:
  - Battery path: auto-detected from /sys/class/power_supply/BAT*/
  - Network interface: auto-detected from /sys/class/net/ (skip lo)
  - xprintidle: optional (see proc_reader.py for fallback chain)
  - No shell-outs for battery/network (sysfs reads only)

Run directly:
    cd powerlayer/
    python -m collector.monitor
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml

# ── Internal imports ──────────────────────────────────────────────────────────
# Make this module runnable both as `python -m collector.monitor`
# and importable from the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from collector.proc_reader import (
    snapshot_processes,
    get_active_window_pid,
    is_user_active,
    get_system_cpu_pct,
)
from collector.psi_reader import (
    read_all_pressure,
    check_cgroup_v2,
    psi_available,
)
from storage.db import get_connection, BatchedWriter, log_action
from storage.aggregator import run as aggregator_run

logger = logging.getLogger(__name__)

# ── Config defaults (overridden by config.yaml) ───────────────────────────────
_DEFAULTS = {
    "storage": {
        "db_path":              "data/powerlayer.db",
        "flush_interval_seconds": 30,
        "flush_batch_size":     100,
        "raw_retention_hours":  48,
    },
    "collector": {
        "poll_interval_idle":    45,
        "poll_interval_active":   7,
        "battery_path":         "auto",
        "network_interface":    "auto",
        "watch_dirs":           ["~/.config"],
        "xprintidle_available": "auto",
    },
    "shadow_mode": True,
    "logging": {
        "level":    "INFO",
        "log_file": "data/powerlayer.log",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Sysfs helpers (no subprocess — direct file reads only)
# ─────────────────────────────────────────────────────────────────────────────

def detect_battery_path() -> Path | None:
    """
    Scan /sys/class/power_supply/ and return the first BAT* directory
    that has a readable 'capacity' file.  Handles BAT0, BAT1, BATT, etc.
    Works on any Linux distro.
    """
    ps_root = Path("/sys/class/power_supply")
    if not ps_root.exists():
        return None
    for entry in sorted(ps_root.iterdir()):
        # Match BAT0, BAT1, BATT, battery, etc.
        name = entry.name.upper()
        if "BAT" in name or name == "BATTERY":
            cap_file = entry / "capacity"
            if cap_file.exists():
                logger.info("Battery detected: %s", entry)
                return entry
    return None


def detect_network_interface() -> str | None:
    """
    Return the name of the first non-loopback, non-virtual network interface
    that is currently UP.  Reads /sys/class/net/*/operstate — no subprocess.
    """
    net_root = Path("/sys/class/net")
    if not net_root.exists():
        return None

    # Prefer wireless (wlan*, wlp*) then wired (eth*, enp*, eno*)
    preferred_prefixes = ("wlan", "wlp", "eth", "enp", "eno", "em")
    candidates: list[str] = []

    for iface in sorted(net_root.iterdir()):
        name = iface.name
        if name == "lo":
            continue
        operstate = (iface / "operstate")
        if operstate.exists() and operstate.read_text().strip() == "up":
            candidates.append(name)

    for prefix in preferred_prefixes:
        for c in candidates:
            if c.startswith(prefix):
                logger.info("Network interface detected: %s", c)
                return c

    if candidates:
        logger.info("Network interface detected (fallback): %s", candidates[0])
        return candidates[0]
    return None


def read_battery_pct(battery_dir: Path) -> float | None:
    """Read battery capacity (0–100) from sysfs capacity file."""
    try:
        return float((battery_dir / "capacity").read_text().strip())
    except (OSError, ValueError):
        return None


def read_net_bytes(interface: str) -> tuple[int, int]:
    """
    Return (rx_bytes, tx_bytes) for the given network interface.
    Reads /sys/class/net/<iface>/statistics/{rx_bytes,tx_bytes} directly.
    """
    base = Path("/sys/class/net") / interface / "statistics"
    try:
        rx = int((base / "rx_bytes").read_text().strip())
        tx = int((base / "tx_bytes").read_text().strip())
        return rx, tx
    except (OSError, ValueError):
        return 0, 0


# ─────────────────────────────────────────────────────────────────────────────
# Watchdog filesystem event handler
# ─────────────────────────────────────────────────────────────────────────────

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False
    logger.warning("watchdog not installed — filesystem event triggering disabled.")


if _WATCHDOG_AVAILABLE:
    class _SyncEventHandler(FileSystemEventHandler):
        """
        Watchdog handler: sets a threading.Event whenever any filesystem
        change is detected in watched directories.  The main loop checks
        this event and drops to the fast polling interval immediately.
        """
        def __init__(self, event_flag: threading.Event, label: str) -> None:
            super().__init__()
            self._flag = event_flag
            self._label = label

        def on_any_event(self, event) -> None:  # type: ignore[override]
            if not event.is_directory:
                logger.debug("FS event in %s: %s", self._label, event.src_path)
                self._flag.set()


# ─────────────────────────────────────────────────────────────────────────────
# Monitor class
# ─────────────────────────────────────────────────────────────────────────────

class Monitor:
    """
    Main collector: adaptive polling + watchdog FS events.

    Usage:
        mon = Monitor.from_config_file("config.yaml")
        mon.start()   # blocks until SIGINT/SIGTERM
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config
        self._scfg = config.get("storage", _DEFAULTS["storage"])
        self._ccfg = config.get("collector", _DEFAULTS["collector"])
        self._shadow_mode: bool = config.get("shadow_mode", True)

        # Sysfs paths (set during _setup)
        self._battery_dir: Path | None = None
        self._net_iface: str | None = None

        # Network byte delta tracking
        self._last_net_rx: int = 0
        self._last_net_tx: int = 0
        self._last_net_ts: float = 0.0

        # Aggregator: track last run time
        self._last_aggregation_ts: float = 0.0
        self._aggregation_interval: float = 3600.0   # every 1 hour

        # Watchdog
        self._fs_event_flag = threading.Event()
        self._observers: list[Any] = []

        # Storage
        self._conn = None
        self._writer: BatchedWriter | None = None

        # Shutdown
        self._stop_event = threading.Event()

        # Stats (exposed to demo_live.py via public attributes)
        self.stats: dict[str, Any] = {
            "cycles":         0,
            "rows_buffered":  0,
            "rows_flushed":   0,
            "last_flush_ts":  0.0,
            "last_cycle_ts":  0.0,
            "current_interval": self._ccfg.get("poll_interval_idle", 45),
            "battery_pct":    None,
            "psi_cpu_avg10":  None,
            "active_procs":   0,
        }

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_config_file(cls, path: str | Path = "config.yaml") -> "Monitor":
        cfg_path = Path(path)
        if cfg_path.exists():
            with cfg_path.open() as f:
                cfg = yaml.safe_load(f) or {}
        else:
            logger.warning("config.yaml not found at %s — using defaults.", cfg_path)
            cfg = {}
        # Deep-merge with defaults
        merged = {**_DEFAULTS, **cfg}
        return cls(merged)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Set up everything and enter the main loop (blocking)."""
        self._setup()
        self._register_signal_handlers()
        logger.info("PowerLayer collector starting (shadow_mode=%s).", self._shadow_mode)
        try:
            self._main_loop()
        finally:
            self._teardown()

    def stop(self) -> None:
        """Signal the main loop to exit cleanly."""
        self._stop_event.set()

    # ── Setup / teardown ──────────────────────────────────────────────────────

    def _setup(self) -> None:
        """Initialize DB, watchdog, sysfs paths."""
        # DB path relative to project root
        db_path = _PROJECT_ROOT / self._scfg.get("db_path", "data/powerlayer.db")
        self._conn = get_connection(db_path)
        self._writer = BatchedWriter(
            self._conn,
            flush_interval=float(self._scfg.get("flush_interval_seconds", 30)),
            batch_size=int(self._scfg.get("flush_batch_size", 100)),
        )
        self._writer.start()

        # Battery
        bat_cfg = self._ccfg.get("battery_path", "auto")
        if bat_cfg == "auto":
            self._battery_dir = detect_battery_path()
        elif bat_cfg:
            self._battery_dir = Path(bat_cfg)

        if not self._battery_dir:
            logger.info("No battery detected — running on AC power or desktop.")

        # Network
        net_cfg = self._ccfg.get("network_interface", "auto")
        self._net_iface = detect_network_interface() if net_cfg == "auto" else net_cfg
        if self._net_iface:
            self._last_net_rx, self._last_net_tx = read_net_bytes(self._net_iface)
            self._last_net_ts = time.time()

        # cgroup v2 check (informational, not fatal)
        if not check_cgroup_v2():
            logger.warning(
                "cgroup v2 not detected! PSI and cgroup enforcement will be unavailable. "
                "Enable with: sudo grubby --update-kernel=ALL --args='systemd.unified_cgroup_hierarchy=1'"
            )

        if psi_available():
            logger.info("PSI (Pressure Stall Information) available.")
        else:
            logger.info("PSI not available on this kernel/config — skipping PSI reads.")

        # Watchdog observers
        self._start_watchdog()

        logger.info("Collector setup complete. DB: %s", db_path)

    def _teardown(self) -> None:
        """Stop watchdog observers, flush writer, close DB."""
        logger.info("Collector shutting down…")
        for obs in self._observers:
            obs.stop()
            obs.join(timeout=3)
        if self._writer:
            self._writer.stop()
        if self._conn:
            self._conn.close()
        logger.info("Collector stopped cleanly.")

    def _register_signal_handlers(self) -> None:
        """Catch SIGINT and SIGTERM for graceful shutdown.

        signal.signal() only works in the main thread.  When the monitor is
        run inside a daemon thread (e.g. demo_live.py), we skip registration
        and let the caller handle signals instead.
        """
        if threading.current_thread() is not threading.main_thread():
            logger.debug(
                "Not in main thread — skipping signal handler registration. "
                "Caller is responsible for calling monitor.stop()."
            )
            return

        def _handler(sig, frame):  # noqa: ARG001
            logger.info("Signal %s received — stopping collector.", sig)
            self.stop()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    # ── Watchdog ──────────────────────────────────────────────────────────────

    def _start_watchdog(self) -> None:
        if not _WATCHDOG_AVAILABLE:
            return

        watch_dirs: list[str] = self._ccfg.get("watch_dirs", [])
        for raw_dir in watch_dirs:
            expanded = Path(raw_dir).expanduser()
            if not expanded.exists():
                logger.debug("Watch dir does not exist, skipping: %s", expanded)
                continue
            handler = _SyncEventHandler(self._fs_event_flag, str(expanded))
            obs = Observer()
            obs.schedule(handler, str(expanded), recursive=True)
            obs.daemon = True
            obs.start()
            self._observers.append(obs)
            logger.info("Watching for FS events: %s", expanded)

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _main_loop(self) -> None:
        interval_idle:   int = self._ccfg.get("poll_interval_idle", 45)
        interval_active: int = self._ccfg.get("poll_interval_active", 7)
        retention_hours: int = self._scfg.get("raw_retention_hours", 48)

        logger.info(
            "Main loop started. Idle interval: %ds  Active interval: %ds",
            interval_idle, interval_active
        )

        while not self._stop_event.is_set():
            cycle_start = time.perf_counter()

            # ── Take a snapshot ───────────────────────────────────────────────
            rows_this_cycle = self._snapshot()
            self.stats["cycles"] += 1
            self.stats["last_cycle_ts"] = time.time()
            self.stats["rows_buffered"] += rows_this_cycle

            # ── Hourly aggregation ────────────────────────────────────────────
            now = time.time()
            if now - self._last_aggregation_ts >= self._aggregation_interval:
                try:
                    result = aggregator_run(self._conn, retention_hours=retention_hours)
                    logger.info("Aggregation: %s", result)
                except Exception as exc:
                    logger.error("Aggregation error: %s", exc)
                self._last_aggregation_ts = now

            # ── Track flush stats for demo ────────────────────────────────────
            # (BatchedWriter keeps its own lock; we approximate flush timing)
            if self._writer and now - self.stats["last_flush_ts"] >= float(
                self._scfg.get("flush_interval_seconds", 30)
            ):
                self.stats["last_flush_ts"] = now

            # ── Adaptive sleep ────────────────────────────────────────────────
            # Use fast interval if: user is active, OR a FS event fired
            fs_triggered = self._fs_event_flag.is_set()
            if fs_triggered:
                self._fs_event_flag.clear()
                logger.debug("FS event triggered — using fast poll interval.")

            user_active = is_user_active()
            sleep_sec = interval_active if (user_active or fs_triggered) else interval_idle
            self.stats["current_interval"] = sleep_sec

            cycle_elapsed = time.perf_counter() - cycle_start
            actual_sleep = max(0.0, sleep_sec - cycle_elapsed)

            logger.debug(
                "Cycle #%d done in %.2fs — sleeping %.1fs (mode=%s)",
                self.stats["cycles"],
                cycle_elapsed,
                actual_sleep,
                "active" if user_active else "idle",
            )

            self._stop_event.wait(timeout=actual_sleep)

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def _snapshot(self) -> int:
        """
        Collect one round of data: processes + battery + PSI.
        Writes rows to the BatchedWriter buffer.
        Returns the number of rows added to the buffer.
        """
        ts = int(time.time())

        # Battery
        battery_pct = None
        if self._battery_dir:
            battery_pct = read_battery_pct(self._battery_dir)
        self.stats["battery_pct"] = battery_pct

        # PSI
        psi = read_all_pressure()
        cpu_psi = psi.get("cpu")
        if cpu_psi:
            self.stats["psi_cpu_avg10"] = cpu_psi.get("avg10")

        # Network delta
        net_delta_per_proc = 0
        if self._net_iface:
            rx, tx = read_net_bytes(self._net_iface)
            elapsed = time.time() - self._last_net_ts if self._last_net_ts else 1
            net_delta = max(0, (rx - self._last_net_rx) + (tx - self._last_net_tx))
            self._last_net_rx, self._last_net_tx = rx, tx
            self._last_net_ts = time.time()
            net_delta_per_proc = net_delta   # attributed per-snapshot, not per-proc

        # Active window (foreground process)
        fg_pid = get_active_window_pid()

        # Process snapshot
        procs = snapshot_processes(foreground_pid=fg_pid, min_cpu_pct=0.0)
        self.stats["active_procs"] = len(procs)

        rows_added = 0
        for proc in procs:
            # Only write processes with some activity to keep DB lean
            if proc["cpu_pct"] < 0.1 and proc["event_type"] == "idle":
                continue

            row = {
                "timestamp":   ts,
                "app_name":    proc["name"],
                "pid":         proc["pid"],
                "event_type":  proc["event_type"],
                "cpu_pct":     proc["cpu_pct"],
                "net_bytes":   net_delta_per_proc if proc["is_foreground"] else 0,
                "battery_pct": battery_pct,
            }
            self._writer.add(row)
            rows_added += 1

        logger.debug(
            "Snapshot: %d procs scanned, %d rows buffered "
            "(battery=%.1f%%, psi_cpu=%.2f)",
            len(procs),
            rows_added,
            battery_pct if battery_pct is not None else -1,
            cpu_psi.get("avg10", 0) if cpu_psi else 0,
        )

        return rows_added

    # ── Public read-only accessors (for demo_live.py) ─────────────────────────

    def get_db_stats(self) -> dict[str, int]:
        """Query the DB for row counts — safe to call from another thread."""
        if not self._conn:
            return {}
        try:
            return {
                "raw_events":    self._conn.execute(
                    "SELECT COUNT(*) FROM events").fetchone()[0],
                "hourly_buckets": self._conn.execute(
                    "SELECT COUNT(*) FROM events_hourly").fetchone()[0],
                "action_log":    self._conn.execute(
                    "SELECT COUNT(*) FROM action_log").fetchone()[0],
            }
        except Exception:
            return {}

    def get_recent_events(self, n: int = 8) -> list[dict]:
        """Return the N most recent raw event rows for the demo display."""
        if not self._conn:
            return []
        try:
            cur = self._conn.execute(
                """SELECT timestamp, app_name, cpu_pct, event_type, battery_pct
                   FROM events ORDER BY timestamp DESC, id DESC LIMIT ?""",
                (n,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            return []

    def get_buffer_size(self) -> int:
        """Approximate current buffer size (not 100% precise — no lock taken)."""
        if self._writer:
            with self._writer._lock:
                return len(self._writer._buffer)
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def _setup_logging(level: str, log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        lf = _PROJECT_ROOT / log_file
        lf.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(lf))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="PowerLayer data collector")
    parser.add_argument(
        "--config", default=str(_PROJECT_ROOT / "config.yaml"),
        help="Path to config.yaml"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG logging"
    )
    args = parser.parse_args()

    mon = Monitor.from_config_file(args.config)
    log_cfg = mon._cfg.get("logging", _DEFAULTS["logging"])
    level = "DEBUG" if args.debug else log_cfg.get("level", "INFO")
    _setup_logging(level, log_cfg.get("log_file"))

    mon.start()
