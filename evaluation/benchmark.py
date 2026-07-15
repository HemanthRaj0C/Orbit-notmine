"""
evaluation/benchmark.py
─────────────────────────────────────────────────────────────────────────────
PowerLayer A/B Battery Drain Benchmark

Measures the actual battery drain rate with and without PowerLayer active,
then produces a Markdown report with:
  • Drain rate (% per hour) in each phase
  • Estimated extra runtime gained per full charge
  • Throttle / release event counts
  • Most impactful apps (most throttling time saved)

How it works
────────────
Phase A (baseline)  — run for --duration-minutes with PowerLayer STOPPED
                       and collect raw battery readings from sysfs.
Phase B (active)    — restart PowerLayer, run for same duration, collect.

Both phases sample /sys/class/power_supply/BAT*/capacity every
--sample-interval seconds, then compute a linear regression slope
(% per second) to get a stable drain rate estimate free of short-term
noise.

Usage
─────
    python evaluation/benchmark.py                    # 10-min each phase
    python evaluation/benchmark.py --duration 20      # 20-min phases
    python evaluation/benchmark.py --skip-baseline    # only measure active phase
    python evaluation/benchmark.py --report-only      # just re-generate the report

Output
──────
    data/runtime/evaluation_report.md    (written automatically)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Project root ───────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
_REPORT_PATH = _PROJECT_ROOT / "data" / "runtime" / "evaluation_report.md"
_RESULTS_JSON = _PROJECT_ROOT / "data" / "runtime" / "benchmark_results.json"


# ─────────────────────────────────────────────────────────────────────────────
# Battery helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_battery() -> Optional[Path]:
    """Return the sysfs path for the first battery found."""
    base = Path("/sys/class/power_supply")
    for p in sorted(base.iterdir()):
        cap = p / "capacity"
        typ = p / "type"
        if cap.exists():
            try:
                t = typ.read_text().strip()
                if t == "Battery":
                    return p
            except Exception:
                if "BAT" in p.name:
                    return p
    return None


def _read_battery_pct(bat_path: Path) -> Optional[float]:
    try:
        return float((bat_path / "capacity").read_text().strip())
    except Exception:
        return None


def _read_power_now_uw(bat_path: Path) -> Optional[float]:
    """Return instantaneous power draw in µW (if available)."""
    for name in ("power_now", "current_now"):
        p = bat_path / name
        if p.exists():
            try:
                val = float(p.read_text().strip())
                if name == "current_now":
                    # current_now is in µA; need voltage_now in µV to get µW
                    v_path = bat_path / "voltage_now"
                    if v_path.exists():
                        v = float(v_path.read_text().strip())
                        return val * v / 1_000_000   # µA × µV / 1e6 = µW
                    return None
                return val   # power_now already in µW
            except Exception:
                continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Linear regression (least-squares slope)
# ─────────────────────────────────────────────────────────────────────────────

def _linreg_slope(xs: list[float], ys: list[float]) -> float:
    """Return dy/dx via ordinary least squares."""
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Measurement phase
# ─────────────────────────────────────────────────────────────────────────────

def measure_phase(
    phase_name: str,
    duration_s: int,
    sample_interval: int,
    bat_path: Path,
) -> dict:
    """
    Collect battery readings for `duration_s` seconds, every `sample_interval` s.
    Returns a dict with drain_rate_pct_per_hour, samples, etc.
    """
    logger.info("[%s] Measuring for %ds (sample every %ds)…", phase_name, duration_s, sample_interval)

    samples = []       # (elapsed_s, battery_pct)
    power_samples = [] # instantaneous µW readings
    start_ts = time.time()
    deadline = start_ts + duration_s

    while time.time() < deadline:
        elapsed = time.time() - start_ts
        pct = _read_battery_pct(bat_path)
        pw  = _read_power_now_uw(bat_path)
        if pct is not None:
            samples.append((elapsed, pct))
        if pw is not None:
            power_samples.append(pw)

        remaining = deadline - time.time()
        wait = min(sample_interval, max(0, remaining))
        if wait > 0:
            time.sleep(wait)

    if len(samples) < 2:
        logger.warning("[%s] Too few samples (%d) — results unreliable.", phase_name, len(samples))
        return {"phase": phase_name, "drain_rate_pct_per_hour": None, "samples": samples}

    xs = [s[0] for s in samples]
    ys = [s[1] for s in samples]
    slope_per_sec = _linreg_slope(xs, ys)       # %/s  (negative = draining)
    drain_per_hour = -slope_per_sec * 3600       # positive = drain rate

    avg_power_w = (sum(power_samples) / len(power_samples) / 1e6) if power_samples else None

    return {
        "phase":                 phase_name,
        "duration_s":            duration_s,
        "n_samples":             len(samples),
        "start_pct":             samples[0][1],
        "end_pct":               samples[-1][1],
        "drain_rate_pct_per_hour": max(0.0, drain_per_hour),
        "avg_power_w":           avg_power_w,
        "samples":               samples,
        "ts":                    datetime.now().isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DB query helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_throttle_stats(db_path: Path, since_ts: int) -> dict:
    """Return throttle event counts and top apps from the action_log."""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        total = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE timestamp > ? AND action_taken='throttle'",
            (since_ts,),
        ).fetchone()[0]

        released = conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE timestamp > ? AND action_taken='allow'",
            (since_ts,),
        ).fetchone()[0]

        top_rows = conn.execute(
            """SELECT app_name, COUNT(*) as cnt
               FROM action_log WHERE timestamp > ? AND action_taken='throttle'
               GROUP BY app_name ORDER BY cnt DESC LIMIT 5""",
            (since_ts,),
        ).fetchall()

        conn.close()
        return {
            "throttle_events": total,
            "allow_events":    released,
            "top_throttled":   [(r[0], r[1]) for r in top_rows],
        }
    except Exception as exc:
        logger.debug("DB query failed: %s", exc)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Report generator
# ─────────────────────────────────────────────────────────────────────────────

def generate_report(
    baseline: Optional[dict],
    active: Optional[dict],
    throttle_stats: dict,
) -> str:
    """Render the Markdown evaluation report."""

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# ⚡ PowerLayer — Evaluation Report",
        "",
        f"Generated: {now_str}",
        "",
        "---",
        "",
    ]

    # ── Summary banner ────────────────────────────────────────────────────────
    if baseline and active:
        b = baseline["drain_rate_pct_per_hour"]
        a = active["drain_rate_pct_per_hour"]

        if b and b > 0 and a is not None:
            improvement_pct = (b - a) / b * 100
            saved_pph = b - a
            # Estimate extra runtime from full charge (100%)
            extra_h = (100 / a - 100 / b) if (a > 0 and b > 0) else 0
            extra_min = extra_h * 60

            lines += [
                "## 🏆 Summary",
                "",
                f"| Metric | Baseline (no PowerLayer) | Active (PowerLayer ON) | Improvement |",
                f"|--------|--------------------------|------------------------|-------------|",
                f"| Drain rate | **{b:.2f}% / hr** | **{a:.2f}% / hr** | **{improvement_pct:+.1f}%** |",
                f"| Est. runtime (full charge) | {100/b:.1f}h | {100/a:.1f}h | **+{extra_min:.0f} min** |",
            ]

            if baseline.get("avg_power_w") and active.get("avg_power_w"):
                bw = baseline["avg_power_w"]
                aw = active["avg_power_w"]
                lines.append(
                    f"| Avg power draw | {bw:.2f}W | {aw:.2f}W | **{(bw-aw)/bw*100:.1f}%** |"
                )

            lines += ["", f"**Battery life extended by approximately {extra_min:.0f} minutes per charge.**", ""]
        else:
            lines += [
                "## ⚠ Summary",
                "",
                "Could not compute improvement — one or both drain rates are zero or missing.",
                "This may mean the device is on AC power or the measurement period was too short.",
                "",
            ]
    else:
        lines += [
            "## ℹ Single-phase measurement",
            "",
            "Only one phase was measured. Run without `--skip-baseline` for a full A/B comparison.",
            "",
        ]

    # ── Phase details ─────────────────────────────────────────────────────────
    lines += ["---", "", "## 📊 Phase Details", ""]
    for phase in [baseline, active]:
        if not phase:
            continue
        pname = phase["phase"]
        lines += [f"### {pname}", ""]
        lines += [
            f"- **Duration**: {phase.get('duration_s', '?')}s "
            f"({phase.get('duration_s', 0)//60} min)",
            f"- **Start battery**: {phase.get('start_pct', '?'):.1f}%",
            f"- **End battery**: {phase.get('end_pct', '?'):.1f}%",
            f"- **Drain rate**: {phase.get('drain_rate_pct_per_hour', 0):.3f}% / hr",
            f"- **Samples**: {phase.get('n_samples', 0)}",
        ]
        if phase.get("avg_power_w"):
            lines.append(f"- **Avg power**: {phase['avg_power_w']:.2f}W")
        lines.append("")

    # ── Throttle stats ────────────────────────────────────────────────────────
    if throttle_stats:
        lines += ["---", "", "## 🚦 Throttle Activity", ""]
        lines += [
            f"- **Total throttle events**: {throttle_stats.get('throttle_events', 0)}",
            f"- **Total allow events**: {throttle_stats.get('allow_events', 0)}",
            "",
        ]
        top = throttle_stats.get("top_throttled", [])
        if top:
            lines += ["**Most throttled apps:**", ""]
            lines += ["| App | Throttle Events |", "|-----|----------------|"]
            for app, cnt in top:
                lines.append(f"| {app} | {cnt} |")
            lines.append("")

    # ── Footer ────────────────────────────────────────────────────────────────
    lines += [
        "---",
        "",
        "> **Methodology**: Drain rate calculated via linear regression (OLS) on sysfs capacity samples.",
        "> Short measurement windows and AC power may reduce accuracy.",
        "> Run for at least 10 minutes per phase for reliable results.",
    ]

    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Daemon control helpers
# ─────────────────────────────────────────────────────────────────────────────

def _daemon_running() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "powerlayer"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip() == "active":
            return True
    except Exception:
        pass

    # Check local run pid
    pid_path = Path("data/runtime/daemon.pid")
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            return True
        except Exception:
            pass
    return False


def _daemon_stop() -> None:
    logger.info("Stopping PowerLayer daemon...")
    # 1. Try systemd
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", "powerlayer"],
            capture_output=True, timeout=10
        )
    except Exception:
        pass

    # 2. Try local daemon PID
    pid_path = Path("data/runtime/daemon.pid")
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 2)  # SIGINT for clean exit
            for _ in range(5):
                time.sleep(1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
        except Exception:
            pass


def _daemon_start() -> None:
    logger.info("Starting PowerLayer daemon...")
    # 1. Try systemd if service loaded
    has_systemd = False
    try:
        res = subprocess.run(
            ["systemctl", "--user", "list-unit-files", "powerlayer.service"],
            capture_output=True, text=True, timeout=5
        )
        if "powerlayer.service" in res.stdout:
            has_systemd = True
    except Exception:
        pass

    if has_systemd:
        try:
            subprocess.run(["systemctl", "--user", "start", "powerlayer"], timeout=10)
            time.sleep(3)
            return
        except Exception:
            pass

    # 2. Fallback: local run_local.sh start
    local_run = Path("run_local.sh")
    if local_run.exists():
        try:
            subprocess.run(["./run_local.sh", "start"], capture_output=True, timeout=10)
            time.sleep(3)
        except Exception as e:
            logger.error("Failed to start local daemon: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="PowerLayer battery drain benchmark (A/B test)"
    )
    parser.add_argument(
        "--duration", type=int, default=10,
        help="Duration of each phase in minutes (default: 10)"
    )
    parser.add_argument(
        "--sample-interval", type=int, default=30,
        help="Battery sampling interval in seconds (default: 30)"
    )
    parser.add_argument(
        "--db", default=str(_PROJECT_ROOT / "data" / "runtime" / "sandbox.db"),
        help="Path to PowerLayer runtime DB for throttle stats"
    )
    parser.add_argument(
        "--skip-baseline", action="store_true",
        help="Skip baseline phase (only measure PowerLayer active phase)"
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="Re-generate report from saved JSON results (no new measurement)"
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    _PROJECT_ROOT.joinpath("data/runtime").mkdir(parents=True, exist_ok=True)

    # ── Report-only mode ──────────────────────────────────────────────────────
    if args.report_only:
        if not _RESULTS_JSON.exists():
            logger.error("No results file found at %s. Run the benchmark first.", _RESULTS_JSON)
            sys.exit(1)
        with open(_RESULTS_JSON) as f:
            saved = json.load(f)
        report = generate_report(
            saved.get("baseline"),
            saved.get("active"),
            saved.get("throttle_stats", {}),
        )
        _REPORT_PATH.write_text(report)
        print(f"\nReport written to: {_REPORT_PATH}")
        print(report)
        return

    # ── Battery check ─────────────────────────────────────────────────────────
    bat = _find_battery()
    if not bat:
        logger.error(
            "No battery detected (running on desktop or battery sysfs missing). "
            "Battery benchmarking is only meaningful on a laptop."
        )
        sys.exit(1)

    logger.info("Battery found: %s  (%.0f%%)", bat.name, _read_battery_pct(bat) or 0)

    duration_s = args.duration * 60
    db_path = Path(args.db)

    # Dynamically select db path if default is used and systemd is installed
    if args.db == str(_PROJECT_ROOT / "data" / "runtime" / "sandbox.db"):
        has_systemd = False
        try:
            res = subprocess.run(
                ["systemctl", "--user", "list-unit-files", "powerlayer.service"],
                capture_output=True, text=True, timeout=5
            )
            if "powerlayer.service" in res.stdout:
                has_systemd = True
        except Exception:
            pass

        if has_systemd:
            db_path = _PROJECT_ROOT / "data" / "runtime" / "powerlayer.db"
            logger.info("Systemd service detected. Benchmarking on production DB: %s", db_path)
        else:
            logger.info("Local environment detected. Benchmarking on test DB: %s", db_path)

    # ── Phase A: Baseline ──────────────────────────────────────────────────────
    baseline_result = None
    if not args.skip_baseline:
        logger.info("=" * 60)
        logger.info("PHASE A — Baseline (PowerLayer OFF)")
        logger.info("Stopping PowerLayer daemon…")
        _daemon_stop()

        print(f"\n{'='*60}")
        print(f"  PHASE A — Baseline  ({args.duration} min, PowerLayer OFF)")
        print(f"  Unplug charger now if plugged in.")
        print(f"  Starting in 10 seconds…")
        print(f"{'='*60}\n")
        time.sleep(10)

        baseline_result = measure_phase(
            "Phase A — Baseline (PowerLayer OFF)",
            duration_s,
            args.sample_interval,
            bat,
        )
        logger.info("Phase A done. Drain: %.3f%%/hr", baseline_result.get("drain_rate_pct_per_hour", 0))

    # ── Phase B: Active ────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("PHASE B — Active (PowerLayer ON)")
    logger.info("Starting PowerLayer daemon…")
    _daemon_start()

    since_ts = int(time.time())

    print(f"\n{'='*60}")
    print(f"  PHASE B — PowerLayer Active  ({args.duration} min)")
    print(f"  Starting in 5 seconds…")
    print(f"{'='*60}\n")
    time.sleep(5)

    active_result = measure_phase(
        "Phase B — PowerLayer Active",
        duration_s,
        args.sample_interval,
        bat,
    )
    logger.info("Phase B done. Drain: %.3f%%/hr", active_result.get("drain_rate_pct_per_hour", 0))

    # ── Throttle stats from DB ────────────────────────────────────────────────
    throttle_stats = _get_throttle_stats(db_path, since_ts)

    # ── Save raw results ──────────────────────────────────────────────────────
    results = {
        "baseline":      baseline_result,
        "active":        active_result,
        "throttle_stats": throttle_stats,
        "generated_at":  datetime.now().isoformat(),
    }
    with open(_RESULTS_JSON, "w") as f:
        json.dump(results, f, indent=2)
    logger.info("Raw results saved to %s", _RESULTS_JSON)

    # ── Generate report ───────────────────────────────────────────────────────
    report = generate_report(baseline_result, active_result, throttle_stats)
    _REPORT_PATH.write_text(report)

    print("\n" + "=" * 60)
    print(report)
    print("=" * 60)
    print(f"\nFull report written to: {_REPORT_PATH}")


if __name__ == "__main__":
    main()
