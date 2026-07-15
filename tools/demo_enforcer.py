"""
tools/demo_enforcer.py
──────────────────────
Live demo of the full PowerLayer pipeline with shadow_mode=false.

What this does:
  1. Spawns a CPU-burning subprocess so there's a real throttle target
  2. Runs the full pipeline: Model → Policy → Enforcer
  3. Actually moves the process into a real cgroup and lowers cpu.weight
  4. You can watch the effect in a second terminal with:
       watch -n1 "cat /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/powerlayer/cpu.weight"
     or:
       htop  (look for the stress_worker process CPU usage drop)
  5. After 10 seconds, releases the throttle and cleans up

Run:
    python tools/demo_enforcer.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from enforcer.cgroup import CgroupEnforcer, _pid_exists
from model.base_model import PowerLayerModel
from model.correction_layer import CorrectionLayer
from policy.engine import PolicyEngine

# ── Colours ───────────────────────────────────────────────────────────────────

G  = "\033[92m"   # green
Y  = "\033[93m"   # yellow
R  = "\033[91m"   # red
B  = "\033[94m"   # blue
DIM= "\033[2m"
RST= "\033[0m"
BOLD="\033[1m"


def banner(msg: str, color: str = B) -> None:
    width = 62
    print(f"\n{color}{'─'*width}{RST}")
    print(f"{color}{BOLD}  {msg}{RST}")
    print(f"{color}{'─'*width}{RST}")


def step(n: int, msg: str) -> None:
    print(f"\n{BOLD}[{n}]{RST} {msg}")


# ── Find writable cgroup root inside user slice ───────────────────────────────

def find_user_app_slice() -> Path | None:
    """Find the user's app.slice cgroup where we have write access."""
    uid = os.getuid()
    candidate = Path(
        f"/sys/fs/cgroup/user.slice/user-{uid}.slice"
        f"/user@{uid}.service/app.slice"
    )
    if candidate.exists() and os.access(str(candidate), os.W_OK):
        return candidate
    return None


# ── CPU stress worker ─────────────────────────────────────────────────────────

def spawn_stress_worker() -> subprocess.Popen:
    """Spawn a CPU-burning Python process as a real throttle target."""
    script = (
        "import time\n"
        "print('stress_worker: burning CPU...', flush=True)\n"
        "x = 0\n"
        "while True:\n"
        "    for _ in range(1_000_000): x += 1\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)  # give it time to start
    return proc


# ── Main demo ─────────────────────────────────────────────────────────────────

def main() -> None:
    banner("PowerLayer — Live Enforcer Demo (shadow_mode=false)", G)
    print(f"\n{DIM}This demo actually throttles a real process via cgroups v2.{RST}")
    print(f"{DIM}No root required — uses your own user cgroup slice.{RST}\n")

    # ── 1. Find writable cgroup location ─────────────────────────────────────
    step(1, "Finding writable cgroup location...")
    app_slice = find_user_app_slice()
    if not app_slice:
        print(f"  {R}✗ Could not find writable user cgroup slice.{RST}")
        print(f"  Run as a systemd user service with Delegate=yes for full support.")
        sys.exit(1)
    cgroup_path = app_slice / "powerlayer"
    print(f"  {G}✓ Will use:{RST} {cgroup_path}")

    # ── 2. Load the trained model ─────────────────────────────────────────────
    step(2, "Loading trained model...")
    model_path = _ROOT / "model" / "artifacts" / "base_model.joblib"
    if not model_path.exists():
        print(f"  {R}✗ No model found at {model_path}{RST}")
        print("  Run: python scripts/train_base_model.py")
        sys.exit(1)
    model = PowerLayerModel(model_path=model_path)
    model.load()
    print(f"  {G}✓ Model loaded{RST}")

    # ── 3. Set up correction layer (no DB needed for demo) ────────────────────
    step(3, "Setting up policy engine (shadow_mode=FALSE)...")
    db_path = _ROOT / "data" / "runtime" / "sandbox.db"

    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER NOT NULL,
            app_name TEXT NOT NULL, pid INTEGER, predicted_label TEXT,
            confidence REAL, action_taken TEXT NOT NULL, reason TEXT,
            shadow_mode INTEGER DEFAULT 0, battery_before REAL,
            battery_after REAL, reverted INTEGER DEFAULT 0, enforcer_cmd TEXT
        )
    """)
    conn.commit()

    corrector = CorrectionLayer(":memory:")

    policy_cfg = {
        "whitelist": ["systemd", "sshd", "pipewire"],
        "confidence_threshold": 0.60,           # lower for demo
        "fresh_start_confidence_threshold": 0.60,
        "min_observations_for_personalization": 0,
        "cooldown_seconds": 5,
    }
    engine = PolicyEngine(
        config=policy_cfg,
        shadow=False,   # ← REAL enforcement
        db_conn=conn,
        model=model,
        corrector=corrector,
    )
    print(f"  {G}✓ Policy engine ready — shadow_mode=FALSE{RST}")

    # ── 4. Set up cgroup enforcer ─────────────────────────────────────────────
    step(4, "Setting up CPU cgroup enforcer...")
    enf_config = {
        "enforcer": {
            "cgroup_root": str(cgroup_path),
            "cpu_throttle_weight": 1,   # weight=1 = very aggressive throttle for clear demo
        }
    }
    enf = CgroupEnforcer(enf_config)
    ready = enf.setup()
    if not ready:
        print(f"  {R}✗ Could not set up cgroup at {cgroup_path}{RST}")
        print(f"    Check: ls -la {app_slice}")
        sys.exit(1)
    print(f"  {G}✓ Cgroup created:{RST} {cgroup_path}")
    print(f"  {G}✓ cpu.weight set to:{RST} {(cgroup_path / 'cpu.weight').read_text().strip()}")

    # ── 5. Spawn stress worker ────────────────────────────────────────────────
    step(5, "Spawning CPU stress worker...")
    proc = spawn_stress_worker()
    pid = proc.pid
    print(f"  {G}✓ stress_worker running{RST}  PID={pid}")
    print(f"  {DIM}You can observe it in another terminal: htop -p {pid}{RST}")

    time.sleep(1)
    print(f"\n  CPU weight BEFORE throttle: {(cgroup_path / 'cpu.weight').read_text().strip()}")

    # ── 6. Run the model against the worker ───────────────────────────────────
    step(6, "Running model prediction on stress_worker...")
    event = {
        "app_name":                   "stress_worker",
        "pid":                        pid,
        "time_since_last_foreground": 7200,   # 2h since last focus
        "sync_freq_ratio":            0.1,    # very low sync activity
        "within_typical_active_hours": False, # outside work hours
        "cpu_pct_relative":           0.2,    # low relative CPU
        "app_category":               "system_utility",
    }
    decision = engine.decide(event, history_count=50)

    print(f"\n  {'─'*50}")
    print(f"  Label      : {BOLD}{decision.label}{RST}")
    print(f"  Confidence : {decision.confidence:.3f}")
    print(f"  Action     : {BOLD}{G if decision.action == 'allow' else Y}{decision.action}{RST}")
    print(f"  Reason     : {DIM}{decision.reason}{RST}")
    print(f"  {'─'*50}")

    # Force the throttle for demo purposes if model said allow
    if decision.action != "throttle":
        print(f"\n  {Y}Model said '{decision.action}' — forcing throttle for demo.{RST}")

    # ── 7. Apply the throttle ─────────────────────────────────────────────────
    step(7, "Applying CPU throttle...")
    cmd = enf.throttle(pid, "stress_worker")
    if cmd:
        weight_after = (cgroup_path / "cpu.weight").read_text().strip()
        print(f"  {G}✓ Throttle applied!{RST}")
        print(f"  Enforcer cmd: {DIM}{cmd}{RST}")
        print(f"  cpu.weight AFTER throttle: {BOLD}{weight_after}{RST}")
        print(f"\n  {DIM}Monitor in real-time:{RST}")
        print(f"  {DIM}  watch -n0.5 'cat {cgroup_path}/cpu.weight && cat {cgroup_path}/cgroup.procs'{RST}")
        print(f"  {DIM}  htop -p {pid}{RST}")
    else:
        print(f"  {R}✗ Throttle failed — check permissions on {cgroup_path}{RST}")

    # ── 8. Hold for observation ───────────────────────────────────────────────
    print(f"\n  {B}{'─'*50}")
    print(f"  Holding for 10 seconds so you can observe the effect.")
    print(f"  PID {pid} is throttled to cpu.weight=1 (vs default 100)")
    print(f"  {'─'*50}{RST}")
    for i in range(10, 0, -1):
        print(f"  \r  Releasing in {i}s...", end="", flush=True)
        time.sleep(1)
    print()

    # ── 9. Release ────────────────────────────────────────────────────────────
    step(9, "Releasing throttle...")
    enf.release(pid, "stress_worker")
    weight_released = (cgroup_path / "cpu.weight").read_text().strip()
    print(f"  {G}✓ Released.{RST}  cpu.weight is now: {weight_released}")

    # ── 10. Cleanup ───────────────────────────────────────────────────────────
    step(10, "Cleanup...")
    proc.terminate()
    proc.wait()
    enf.teardown()
    print(f"  {G}✓ stress_worker killed, cgroup removed{RST}")

    # ── 11. Show action_log ───────────────────────────────────────────────────
    banner("Action Log (what was recorded in DB)", B)
    recent = engine.get_recent_decisions(limit=5)
    if recent:
        print(f"  {'TIME':<10}  {'APP':<16}  {'LABEL':<20}  {'ACTION':<10}  {'CONF'}")
        print(f"  {'─'*70}")
        for r in recent:
            from datetime import datetime
            ts = datetime.fromtimestamp(r['timestamp']).strftime('%H:%M:%S')
            print(f"  {ts:<10}  {r['app_name']:<16}  {r['label']:<20}  {r['action']:<10}  {r['confidence']:.3f}")
    else:
        print("  (no entries)")

    banner("Demo complete!", G)
    print(f"\n  To run with full pipeline:\n")
    print(f"    1. Edit config.yaml:  shadow_mode: false")
    print(f"    2. python tools/demo_live.py  (Real mode)")
    print()


if __name__ == "__main__":
    main()
