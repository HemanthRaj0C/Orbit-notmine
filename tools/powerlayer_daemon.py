"""
tools/powerlayer_daemon.py
──────────────────────────
PowerLayer — Main production daemon.

Runs the full ML pipeline on actual system processes:
  - Collects live process snapshots (CPU, network, battery)
  - Engineers relative/temporal features per app
  - Runs Random Forest predictions (3-class: active/idle/background)
  - Applies per-user correction layer (personalization)
  - Runs PolicyEngine → produces throttle / allow / skip decisions
  - Enforces via cgroups v2 cpu.weight + tc network shaping (when --live)
  - Sends desktop notifications on throttle/release transitions

This is the file that systemd runs. The service unit calls it with --live.
In shadow mode (default) it logs decisions but never actually throttles.

Usage:
  python tools/powerlayer_daemon.py                  # shadow mode (safe, log only)
  python tools/powerlayer_daemon.py --live           # live enforcement (real throttling)
  python tools/powerlayer_daemon.py --interval 5     # poll every 5s (default: 7s)

Writes to:
  DB:  data/runtime/sandbox.db
  Log: data/runtime/sandbox.log
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

# ── Make project importable ───────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from collector.proc_reader import snapshot_processes, get_active_window_pid, is_user_active
from collector.psi_reader import read_all_pressure
from model.base_model import PowerLayerModel
from model.correction_layer import CorrectionLayer
from policy.engine import PolicyEngine, Decision
from enforcer import Enforcer
from collector.monitor import detect_battery_path, read_battery_pct

# ── Logging setup ─────────────────────────────────────────────────────────────
DB_PATH = _ROOT / "data" / "runtime" / "sandbox.db"
LOG_PATH = _ROOT / "data" / "runtime" / "sandbox.log"

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("powerlayer_daemon")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout)
    ]
)

# ── Helper: Map process name to category ──────────────────────────────────────
def get_app_category(app_name: str) -> str:
    app_lower = app_name.lower()
    if any(b in app_lower for b in ["chrome", "firefox", "zen", "chromium", "opera", "safari", "browser"]):
        return "browser"
    if any(s in app_lower for s in ["dropbox", "rclone", "nextcloud", "megasync", "sync"]):
        return "cloud_sync"
    if any(c in app_lower for c in ["zoom", "slack", "discord", "teams", "telegram", "wechat", "signal"]):
        return "communication"
    if any(d in app_lower for d in ["code", "python", "git", "make", "gcc", "g++", "clang", "rustc", "cargo", "bash", "zsh", "fish"]):
        return "development_tool"
    if any(m in app_lower for m in ["spotify", "vlc", "mpv", "mpd", "rhythmbox", "audacious", "youtube"]):
        return "streaming"
    return "system_utility"

# ── Core Loop ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Desktop notification helper
# ─────────────────────────────────────────────────────────────────────────────

def _notify(title: str, body: str, urgency: str = "normal") -> None:
    """
    Send a desktop notification if notify-send is available.
    Falls back silently if not installed or no display is set.
    """
    if not shutil.which("notify-send"):
        return
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    try:
        subprocess.run(
            ["notify-send", "--urgency", urgency, "--icon", "battery-caution",
             "--app-name", "PowerLayer", title, body],
            timeout=3, check=False,
        )
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-time PowerLayer Pipeline Daemon")
    parser.add_argument("--live", action="store_true", help="Run with active enforcement (non-shadow)")
    parser.add_argument("--interval", type=int, default=7, help="Poll interval in seconds (default: 7)")
    parser.add_argument("--db", default=None, help="Path to SQLite database")
    args = parser.parse_args()

    # Load db path from config if not specified via CLI
    db_path_str = args.db
    if not db_path_str:
        # Load from config.yaml
        config_path = _ROOT / "config.yaml"
        if config_path.exists():
            import yaml
            try:
                with open(config_path) as f:
                    cfg = yaml.safe_load(f)
                    db_path_str = cfg.get("storage", {}).get("db_path", "data/runtime/powerlayer.db")
            except Exception:
                db_path_str = "data/runtime/powerlayer.db"
        else:
            db_path_str = "data/runtime/powerlayer.db"

    db_path = Path(db_path_str) if Path(db_path_str).is_absolute() else _ROOT / db_path_str
    shadow_mode = not args.live

    print("\n" + "="*70)
    print("⚡ POWERLAYER — Real-time Live Prediction Daemon")
    print(f"   DB Path     : {db_path}")
    print(f"   Log Path    : {LOG_PATH}")
    print(f"   Shadow Mode : {shadow_mode} (use --live to enforce)")
    print(f"   Interval    : {args.interval}s")
    print("="*70 + "\n")

    # 1. Initialize schema
    schema_path = _ROOT / "storage" / "schema.sql"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema_path.read_text())
    conn.commit()

    # 2. Load Model
    model_path = _ROOT / "model" / "artifacts" / "base_model.joblib"
    if not model_path.exists():
        print(f"Error: Model not found at {model_path}. Run training script first.")
        sys.exit(1)
    model = PowerLayerModel(model_path)
    model.load()

    # 3. Personalization & Policy Engine
    corrector = CorrectionLayer(str(db_path))
    policy_cfg = {
        "whitelist": ["systemd", "sshd", "pipewire", "pulseaudio", "Xorg", "Xwayland", "dbus-daemon", "NetworkManager"],
        "confidence_threshold": 0.85,
        "fresh_start_confidence_threshold": 0.95,
        "min_observations_for_personalization": 15,
        "cooldown_seconds": 30,
    }
    engine = PolicyEngine(
        config=policy_cfg,
        shadow=shadow_mode,
        db_conn=conn,
        model=model,
        corrector=corrector
    )

    # 4. Enforcer setup
    enf_config = {
        "shadow_mode": shadow_mode,
        "enforcer": {
            "cgroup_root": f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/powerlayer_real",
            "cpu_throttle_weight": 20,
            "network_throttle_rate": "300kbit",
        },
        "collector": {
            "network_interface": "auto"
        }
    }
    enforcer = Enforcer(enf_config)
    enforcer.setup()

    # Keep track of active window & network stats
    last_net_ts = time.time()
    last_net_rx, last_net_tx = 0, 0

    # Detect battery path
    battery_dir = detect_battery_path()
    if battery_dir:
        print(f"   Battery Dir : {battery_dir}")
    else:
        print("   Battery Dir : Not detected (fallback to 100%)")

    print("Pipeline started successfully. Listening for active processes...")
    print("Press Ctrl+C to exit.\n")

    try:
        cycle = 0
        _throttle_state: dict[str, str] = {}   # app_name → last action for notification transitions
        while True:
            cycle += 1
            now = int(time.time())
            fg_pid = get_active_window_pid()
            procs = snapshot_processes(foreground_pid=fg_pid, min_cpu_pct=0.0)

            # Get current real battery pct
            battery_pct = 100.0
            if battery_dir:
                pct = read_battery_pct(battery_dir)
                if pct is not None:
                    battery_pct = pct

            # Clean active processes (filter out short-lived / kernel tasks)
            active_procs = [p for p in procs if p["cpu_pct"] >= 0.1 or p["event_type"] == "foreground"]

            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Cycle #{cycle} — Scanned {len(procs)} procs, {len(active_procs)} active:")
            print(f"  {'PID':<7} | {'APP NAME':<20} | {'CATEGORY':<16} | {'CPU%':<6} | {'LABEL':<12} | {'ACTION':<9} | {'CONF'}")
            print("  " + "─"*80)

            for p in active_procs:
                app_name = p["name"]
                pid = p["pid"]
                category = get_app_category(app_name)
                cpu_pct = p["cpu_pct"]

                # ── History Queries from DB ───────────────────────────────────
                # 1. History count
                hist_count = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE app_name = ?", (app_name,)
                ).fetchone()[0]

                # 2. Avg CPU
                avg_cpu = conn.execute(
                    "SELECT AVG(cpu_pct) FROM events WHERE app_name = ?", (app_name,)
                ).fetchone()[0] or 1.0

                # 3. CPU history trend (last 3 relative values)
                db_hist = conn.execute(
                    """SELECT cpu_pct FROM events WHERE app_name = ?
                       ORDER BY timestamp DESC LIMIT 3""", (app_name,)
                ).fetchall()
                cpu_history = [r[0] / avg_cpu for r in reversed(db_hist)] if db_hist else []

                # 4. Foreground tracking
                last_fg = conn.execute(
                    """SELECT MAX(timestamp) FROM events
                       WHERE app_name = ? AND event_type = 'foreground'""", (app_name,)
                ).fetchone()[0]

                time_since_last_fg = 7200.0
                if p["event_type"] == "foreground":
                    time_since_last_fg = 0.0
                elif last_fg:
                    time_since_last_fg = float(now - last_fg)

                # Ratios
                cpu_pct_relative = cpu_pct / avg_cpu if hist_count >= 10 else 1.0
                sync_freq_ratio = 1.0 # default simplified

                # active hours
                hour = datetime.now().hour
                within_active_hours = 9 <= hour <= 18

                event = {
                    "app_name": app_name,
                    "pid": pid,
                    "time_since_last_foreground": time_since_last_fg,
                    "sync_freq_ratio": sync_freq_ratio,
                    "within_typical_active_hours": within_active_hours,
                    "cpu_pct_relative": cpu_pct_relative,
                    "app_category": category,
                }

                # Save raw event to DB immediately so it acts as history for next cycles
                conn.execute(
                    """INSERT INTO events (timestamp, app_name, pid, event_type, cpu_pct, net_bytes, battery_pct)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (now, app_name, pid, p["event_type"], cpu_pct, 0, battery_pct)
                )
                conn.commit()

                # Get policy decision
                decision = engine.decide(event, cpu_history=cpu_history, history_count=hist_count)

                # Colors for console formatting
                act_col = "\033[93m" if decision.action == "throttle" else "\033[92m" if decision.action == "allow" else "\033[2m"
                label_col = "\033[92m" if decision.label == "active_needed" else "\033[91m" if decision.label == "background_unused" else "\033[93m"
                reset = "\033[0m"

                print(f"  {pid:<7} | {app_name[:20]:<20} | {category:<16} | {cpu_pct:<6.1f} | "
                      f"{label_col}{decision.label:<12}{reset} | {act_col}{decision.action:<9}{reset} | {decision.confidence:.2f}")

                # Enforce the action if it's throttle, else release
                # Track per-app state to fire notifications only on transitions.
                prev = _throttle_state.get(app_name)
                if decision.action == "throttle":
                    enforcer.enforce(decision)
                    if prev != "throttle":
                        _notify(
                            f"PowerLayer: Throttling {app_name}",
                            f"Detected as background/unused ({decision.confidence:.0%} conf). "
                            "CPU & network rate-limited.",
                            urgency="normal",
                        )
                    _throttle_state[app_name] = "throttle"
                else:
                    enforcer.release(pid, app_name)
                    if prev == "throttle":
                        _notify(
                            f"PowerLayer: Released {app_name}",
                            "App is now active — throttling removed.",
                            urgency="low",
                        )
                    _throttle_state[app_name] = decision.action

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nExiting pipeline cleanly...")
    finally:
        enforcer.teardown()
        conn.close()
        print("Real-time pipeline shut down.")

if __name__ == "__main__":
    main()
