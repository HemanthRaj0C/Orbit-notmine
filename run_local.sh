#!/usr/bin/env bash
# run_local.sh
# ─────────────────────────────────────────────────────────────────────────────
# PowerLayer — Local Development & Demo Runner
#
# Allows running the entire PowerLayer pipeline (Daemon, Tray, CLI) in local
# user space without installing systemd services, modifying sudoers, or copying
# global commands.
#
# Usage:
#   ./run_local.sh start      # Starts daemon & tray in background
#   ./run_local.sh stop       # Stops background daemon & tray
#   ./run_local.sh restart    # Restarts background processes
#   ./run_local.sh status     # CLI: live status dashboard
#   ./run_local.sh report     # CLI: battery savings report
#   ./run_local.sh explain <app> # CLI: explain decisions for app
#   ./run_local.sh override <app> [options] # CLI: set app corrections
#   ./run_local.sh seed       # Mentor: seed sandbox.db with mock data
#   ./run_local.sh e2e        # Developer: run quick pipeline check
#   ./run_local.sh demo       # Mentor: live terminal dashboard
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
err()  { echo -e "  ${RED}✗${RESET}  $*"; exit 1; }
info() { echo -e "  ${CYAN}→${RESET}  $*"; }

# ── Project root ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
PID_DIR="$PROJECT_ROOT/data/runtime"
DAEMON_PID="$PID_DIR/daemon.pid"
TRAY_PID="$PID_DIR/tray.pid"

mkdir -p "$PID_DIR"

# ── Detect Python ─────────────────────────────────────────────────────────────
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

[[ -z "$PYTHON" ]] && err "Python 3.9+ is required."

# ── Ensure Model Exists ───────────────────────────────────────────────────────
ensure_model() {
    MODEL_ARTIFACT="$PROJECT_ROOT/model/artifacts/base_model.joblib"
    if [[ ! -f "$MODEL_ARTIFACT" ]]; then
        info "Model not found. Training base model first..."
        "$PYTHON" "$PROJECT_ROOT/scripts/train_base_model.py"
        ok "Model trained successfully."
    fi
}

# ── Start local services ──────────────────────────────────────────────────────
start_local() {
    ensure_model

    # Check if daemon is already running
    if [[ -f "$DAEMON_PID" ]]; then
        PID=$(cat "$DAEMON_PID")
        if kill -0 "$PID" 2>/dev/null; then
            warn "PowerLayer Daemon is already running (PID: $PID)."
        else
            rm -f "$DAEMON_PID"
        fi
    fi

    # Check if tray is already running
    if [[ -f "$TRAY_PID" ]]; then
        PID=$(cat "$TRAY_PID")
        if kill -0 "$PID" 2>/dev/null; then
            warn "PowerLayer Tray is already running (PID: $PID)."
        else
            rm -f "$TRAY_PID"
        fi
    fi

    # Start Daemon (starts in background, writes logs to data/runtime/sandbox.log)
    if [[ ! -f "$DAEMON_PID" ]]; then
        info "Starting PowerLayer Daemon in background (real data → sandbox.db)..."
        "$PYTHON" "$PROJECT_ROOT/tools/powerlayer_daemon.py" --live --interval 7 --db "$PROJECT_ROOT/data/runtime/sandbox.db" > /dev/null 2>&1 &
        echo $! > "$DAEMON_PID"
        ok "Daemon started (PID: $(cat "$DAEMON_PID")). Logs: data/runtime/sandbox.log"
    fi

    # Start Tray (if X/Wayland display available)
    if [[ ! -f "$TRAY_PID" ]]; then
        if [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
            info "Starting PowerLayer Tray in background (reads sandbox.db)..."
            export POWERLAYER_ROOT="$PROJECT_ROOT"
            "$PYTHON" "$PROJECT_ROOT/cli/tray.py" --db "$PROJECT_ROOT/data/runtime/sandbox.db" > /dev/null 2>&1 &
            echo $! > "$TRAY_PID"
            ok "Tray started (PID: $(cat "$TRAY_PID"))."
        else
            warn "No display detected. Skipping Tray launch."
        fi
    fi
}

# ── Stop local services ───────────────────────────────────────────────────────
stop_local() {
    # Stop Tray
    if [[ -f "$TRAY_PID" ]]; then
        PID=$(cat "$TRAY_PID")
        if kill -0 "$PID" 2>/dev/null; then
            info "Stopping PowerLayer Tray (PID: $PID)..."
            kill "$PID" || kill -9 "$PID"
            ok "Tray stopped."
        fi
        rm -f "$TRAY_PID"
    else
        info "Tray was not running."
    fi

    # Stop Daemon
    if [[ -f "$DAEMON_PID" ]]; then
        PID=$(cat "$DAEMON_PID")
        if kill -0 "$PID" 2>/dev/null; then
            info "Stopping PowerLayer Daemon (PID: $PID)..."
            kill "$PID" || kill -9 "$PID"
            ok "Daemon stopped."
        fi
        rm -f "$DAEMON_PID"
    else
        info "Daemon was not running."
    fi
}

# ── Usage / Help ──────────────────────────────────────────────────────────────
usage() {
    echo -e "${BOLD}${CYAN}PowerLayer — Local Developer & Demo Runner${RESET}"
    echo -e "Usage: ./run_local.sh ${BOLD}COMMAND${RESET} [args]"
    echo ""
    echo -e "  ${BOLD}1. Local Testing & Dev (real data → sandbox.db):${RESET}"
    echo -e "    start                 Start daemon & tray in background"
    echo -e "    stop                  Stop background daemon & tray"
    echo -e "    restart               Restart background services"
    echo -e "    status                CLI: show live test database status"
    echo -e "    report                CLI: generate battery report"
    echo -e "    explain <app>         CLI: explain decisions for an app"
    echo -e "    override <app> [opt]  CLI: set overrides for an app"
    echo -e "    e2e                   Dev: run quick 6-step model pipeline check"
    echo ""
    echo -e "  ${BOLD}2. Mentor Demo (simulated data → demo.db):${RESET}"
    echo -e "    demo                  Start simulation & terminal dashboard (auto-seeds)"
    echo -e "    seed                  Manually seed/refresh the demo database"
    echo -e "    demo-status           CLI: show status dashboard for seeded data"
    echo -e "    demo-report           CLI: show battery report for seeded data"
    echo -e "    demo-explain <app>    CLI: explain mock decisions for seeded app"
    echo ""
}

# ── Execute Commands ──────────────────────────────────────────────────────────
COMMAND="${1:-}"

case "$COMMAND" in
    start)
        start_local
        ;;
    stop)
        stop_local
        ;;
    restart)
        stop_local
        sleep 1
        start_local
        ;;
    status)
        "$PYTHON" "$PROJECT_ROOT/cli/__init__.py" --db "$PROJECT_ROOT/data/runtime/sandbox.db" status "${@:2}"
        ;;
    report)
        "$PYTHON" "$PROJECT_ROOT/cli/__init__.py" --db "$PROJECT_ROOT/data/runtime/sandbox.db" report "${@:2}"
        ;;
    explain)
        if [[ -z "${2:-}" ]]; then
            err "Usage: ./run_local.sh explain APP_NAME"
        fi
        "$PYTHON" "$PROJECT_ROOT/cli/__init__.py" --db "$PROJECT_ROOT/data/runtime/sandbox.db" explain "$2" "${@:3}"
        ;;
    override)
        if [[ -z "${2:-}" ]]; then
            err "Usage: ./run_local.sh override APP_NAME [options]"
        fi
        "$PYTHON" "$PROJECT_ROOT/cli/__init__.py" --db "$PROJECT_ROOT/data/runtime/sandbox.db" override "$2" "${@:3}"
        ;;
    seed)
        info "Seeding demo database (data/runtime/demo.db)..."
        "$PYTHON" "$PROJECT_ROOT/tools/seed_demo_db.py" --db "$PROJECT_ROOT/data/runtime/demo.db"
        ;;
    e2e)
        ensure_model
        "$PYTHON" "$PROJECT_ROOT/tools/e2e_flow_test.py" --db "$PROJECT_ROOT/data/runtime/sandbox.db" "${@:2}"
        ;;
    demo)
        info "Auto-seeding demo database first..."
        "$PYTHON" "$PROJECT_ROOT/tools/seed_demo_db.py" --db "$PROJECT_ROOT/data/runtime/demo.db" > /dev/null
        ok "Demo database seeded."
        info "Launching terminal dashboard in simulation mode (Ctrl+C to stop)..."
        "$PYTHON" "$PROJECT_ROOT/tools/demo_live.py" --sim --fast
        ;;
    demo-status)
        "$PYTHON" "$PROJECT_ROOT/cli/__init__.py" --db "$PROJECT_ROOT/data/runtime/demo.db" status "${@:2}"
        ;;
    demo-report)
        "$PYTHON" "$PROJECT_ROOT/cli/__init__.py" --db "$PROJECT_ROOT/data/runtime/demo.db" report "${@:2}"
        ;;
    demo-explain)
        if [[ -z "${2:-}" ]]; then
            err "Usage: ./run_local.sh demo-explain APP_NAME"
        fi
        "$PYTHON" "$PROJECT_ROOT/cli/__init__.py" --db "$PROJECT_ROOT/data/runtime/demo.db" explain "$2" "${@:3}"
        ;;
    *)
        usage
        ;;
esac
