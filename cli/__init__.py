"""
cli/__init__.py
───────────────
PowerLayer command-line interface.

Usage:
  python -m powerlayer status                       # live dashboard
  python -m powerlayer explain dropbox              # why was dropbox throttled?
  python -m powerlayer override spotify --always-allow
  python -m powerlayer override updater --always-throttle
  python -m powerlayer override dropbox --reset
  python -m powerlayer report                       # last 24h summary
  python -m powerlayer report --hours 48

Can also be called directly:
  python cli/__init__.py status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from cli.commands import cmd_status, cmd_explain, cmd_override, cmd_report


DEFAULT_DB = _ROOT / "data" / "runtime" / "sandbox.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="powerlayer",
        description="PowerLayer — Adaptive Linux Battery Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  powerlayer status
  powerlayer explain dropbox
  powerlayer override spotify --always-allow
  powerlayer override updater --always-throttle
  powerlayer override dropbox --reset
  powerlayer report
  powerlayer report --hours 48
        """,
    )

    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"Path to SQLite database (default: {DEFAULT_DB})",
        metavar="PATH",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # ── status ────────────────────────────────────────────────────────────────
    p_status = sub.add_parser(
        "status",
        help="Live dashboard: running processes, model decisions, system health",
    )
    p_status.add_argument(
        "--limit", type=int, default=15,
        help="Max rows to show per section (default: 15)",
    )

    # ── explain ───────────────────────────────────────────────────────────────
    p_explain = sub.add_parser(
        "explain",
        help="Show full reasoning for every decision made about an app",
    )
    p_explain.add_argument(
        "app", metavar="APP_NAME",
        help="Process name to explain (e.g. dropbox, spotify, code)",
    )
    p_explain.add_argument(
        "--limit", type=int, default=10,
        help="Max decisions to show (default: 10)",
    )

    # ── override ──────────────────────────────────────────────────────────────
    p_override = sub.add_parser(
        "override",
        help="Set a per-app user preference (takes effect immediately)",
    )
    p_override.add_argument(
        "app", metavar="APP_NAME",
        help="Process name to configure",
    )
    mode_group = p_override.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--always-allow", dest="mode", action="store_const", const="always-allow",
        help="Protect this app from throttling (correction_factor=3.0)",
    )
    mode_group.add_argument(
        "--always-throttle", dest="mode", action="store_const", const="always-throttle",
        help="Be aggressive with this app (correction_factor=0.1)",
    )
    mode_group.add_argument(
        "--reset", dest="mode", action="store_const", const="reset",
        help="Remove override — let the model decide again",
    )

    # ── report ────────────────────────────────────────────────────────────────
    p_report = sub.add_parser(
        "report",
        help="Battery savings and throttle activity summary",
    )
    p_report.add_argument(
        "--hours", type=int, default=24,
        help="Time window to summarise (default: 24h)",
    )

    # ── benchmark ─────────────────────────────────────────────────────────────
    p_bench = sub.add_parser(
        "benchmark",
        help="Run A/B battery benchmark test comparing baseline and PowerLayer active states",
    )
    p_bench.add_argument(
        "--duration", type=int, default=10,
        help="Duration of each phase in minutes (default: 10)",
    )
    p_bench.add_argument(
        "--sample-interval", type=int, default=30,
        help="Battery sampling interval in seconds (default: 30)",
    )
    p_bench.add_argument(
        "--skip-baseline", action="store_true",
        help="Skip baseline phase (only measure PowerLayer active phase)",
    )
    p_bench.add_argument(
        "--report-only", action="store_true",
        help="Re-generate report from saved JSON results (no new measurement)",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args   = parser.parse_args(argv)
    db     = args.db

    if args.command == "status":
        cmd_status(db, limit=args.limit)
    elif args.command == "explain":
        cmd_explain(db, app_name=args.app, limit=args.limit)
    elif args.command == "override":
        cmd_override(db, app_name=args.app, mode=args.mode)
    elif args.command == "report":
        cmd_report(db, hours=args.hours)
    elif args.command == "benchmark":
        # Call benchmark's main with custom parameter array
        from evaluation.benchmark import main as run_bench
        bench_args = ["--db", str(db)]
        bench_args += ["--duration", str(args.duration)]
        bench_args += ["--sample-interval", str(args.sample_interval)]
        if args.skip_baseline:
            bench_args.append("--skip-baseline")
        if args.report_only:
            bench_args.append("--report-only")
        run_bench(bench_args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
