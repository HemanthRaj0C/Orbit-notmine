#!/usr/bin/env bash
# install.sh
# ─────────────────────────────────────────────────────────────────────────────
# PowerLayer — Cross-distro Installer
#
# Works on:
#   Fedora / RHEL / CentOS Stream (dnf)
#   Ubuntu / Debian / Linux Mint   (apt)
#   Arch Linux / Manjaro           (pacman)
#   openSUSE                       (zypper)
#
# What this script does:
#   1. Verify system requirements (Python ≥3.9, cgroups v2, systemd)
#   2. Install missing Python dependencies
#   3. Train the ML model if not already trained
#   4. Register powerlayer and powerlayer-tray as user systemd services
#   5. Set up passwordless sudoers rule for `tc` (network throttling)
#   6. Create a global `powerlayer` CLI command symlink
#   7. Optionally check GNOME AppIndicator extension status
#
# Run as your normal user (not root — the script will sudo only when needed):
#   chmod +x install.sh
#   ./install.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
err()  { echo -e "  ${RED}✗${RESET}  $*"; exit 1; }
info() { echo -e "  ${CYAN}→${RESET}  $*"; }
step() { echo -e "\n${BOLD}[$1]${RESET} $2"; }

# ── Project root (this script lives in project root) ─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
PYTHON=""

echo ""
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  PowerLayer — Installer                              ${RESET}"
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "  Project: ${PROJECT_ROOT}"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
step "1" "Checking Python version..."
# ─────────────────────────────────────────────────────────────────────────────

for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" &>/dev/null; then
        PY_VERSION=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
        MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
        if [[ "$MAJOR" -ge 3 && "$MINOR" -ge 9 ]]; then
            PYTHON=$(command -v "$candidate")
            ok "Found $PYTHON (Python $PY_VERSION)"
            break
        fi
    fi
done

[[ -z "$PYTHON" ]] && err "Python 3.9+ is required. Please install it and re-run this script."

# ─────────────────────────────────────────────────────────────────────────────
step "2" "Checking cgroups v2..."
# ─────────────────────────────────────────────────────────────────────────────

if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
    ok "cgroups v2 is active."
else
    warn "cgroups v2 not detected. CPU throttling will be unavailable."
    warn "Enable it: sudo grubby --update-kernel=ALL --args='systemd.unified_cgroup_hierarchy=1'"
    warn "Or add 'systemd.unified_cgroup_hierarchy=1' to your kernel boot parameters."
fi

# ─────────────────────────────────────────────────────────────────────────────
step "3" "Checking systemd..."
# ─────────────────────────────────────────────────────────────────────────────

if command -v systemctl &>/dev/null && systemctl --user status &>/dev/null 2>&1; then
    ok "systemd (user session) is available."
else
    warn "systemd user session not detected. Service auto-start will be unavailable."
    warn "You can still run PowerLayer manually: python ${PROJECT_ROOT}/tools/powerlayer_daemon.py"
fi

# ─────────────────────────────────────────────────────────────────────────────
step "4" "Installing Python dependencies..."
# ─────────────────────────────────────────────────────────────────────────────

REQUIREMENTS="$PROJECT_ROOT/requirements.txt"
if [[ -f "$REQUIREMENTS" ]]; then
    "$PYTHON" -m pip install --quiet --user -r "$REQUIREMENTS"
    ok "Dependencies installed from requirements.txt"
else
    warn "requirements.txt not found — skipping pip install."
fi

# ─────────────────────────────────────────────────────────────────────────────
step "5" "Training model (if needed)..."
# ─────────────────────────────────────────────────────────────────────────────

MODEL_ARTIFACT="$PROJECT_ROOT/model/artifacts/base_model.joblib"
if [[ -f "$MODEL_ARTIFACT" ]]; then
    ok "Model already trained at $MODEL_ARTIFACT"
else
    info "Training ML model — this takes ~30 seconds..."
    "$PYTHON" "$PROJECT_ROOT/scripts/train_base_model.py"
    ok "Model trained and saved."
fi

# ─────────────────────────────────────────────────────────────────────────────
step "6" "Setting up runtime data directory..."
# ─────────────────────────────────────────────────────────────────────────────

mkdir -p "$PROJECT_ROOT/data/runtime"
ok "Runtime data directory: $PROJECT_ROOT/data/runtime"

# ─────────────────────────────────────────────────────────────────────────────
step "7" "Installing systemd user services..."
# ─────────────────────────────────────────────────────────────────────────────

SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

# Substitute actual paths into the service files
for svc_template in powerlayer.service powerlayer-tray.service; do
    src="$PROJECT_ROOT/scripts/$svc_template"
    dst="$SYSTEMD_USER_DIR/$svc_template"

    sed \
        -e "s|@@PYTHON@@|${PYTHON}|g" \
        -e "s|@@PROJECT_ROOT@@|${PROJECT_ROOT}|g" \
        "$src" > "$dst"

    ok "Installed $svc_template → $dst"
done

systemctl --user daemon-reload
ok "systemd user daemon reloaded."

systemctl --user enable powerlayer.service powerlayer-tray.service
ok "Services enabled (will auto-start on next login)."

# ─────────────────────────────────────────────────────────────────────────────
step "8" "Starting services now..."
# ─────────────────────────────────────────────────────────────────────────────

systemctl --user start powerlayer.service
ok "PowerLayer collector daemon is running."

# Tray app needs a display — only start it if $DISPLAY is set
if [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
    systemctl --user start powerlayer-tray.service
    ok "PowerLayer tray indicator is running."
else
    warn "No display detected ($DISPLAY/$WAYLAND_DISPLAY not set). Tray will start on next login."
fi

# ─────────────────────────────────────────────────────────────────────────────
step "9" "Setting up passwordless tc for network throttling..."
# ─────────────────────────────────────────────────────────────────────────────

CURRENT_USER="$(whoami)"
TC_PATH="$(command -v tc 2>/dev/null || echo '')"

if [[ -z "$TC_PATH" ]]; then
    warn "tc (iproute2) not found. Network throttling will be unavailable."
    warn "Install it: sudo dnf install iproute  OR  sudo apt install iproute2"
else
    SUDOERS_FILE="/etc/sudoers.d/powerlayer"
    SUDOERS_LINE="${CURRENT_USER} ALL=(root) NOPASSWD: ${TC_PATH} *"

    if [[ -f "$SUDOERS_FILE" ]]; then
        ok "Sudoers rule already exists at $SUDOERS_FILE"
    else
        info "Creating sudoers rule for tc (requires your password once)..."
        echo "$SUDOERS_LINE" | sudo tee "$SUDOERS_FILE" > /dev/null
        sudo chmod 0440 "$SUDOERS_FILE"
        ok "Network throttling enabled (passwordless tc configured)."
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
step "10" "Creating global 'powerlayer' CLI command..."
# ─────────────────────────────────────────────────────────────────────────────

CLI_WRAPPER="/usr/local/bin/powerlayer"
CLI_CONTENT="#!/bin/bash
exec ${PYTHON} ${PROJECT_ROOT}/cli/__init__.py \"\$@\"
"

if [[ -f "$CLI_WRAPPER" ]]; then
    ok "'powerlayer' command already exists at $CLI_WRAPPER"
else
    echo "$CLI_CONTENT" | sudo tee "$CLI_WRAPPER" > /dev/null
    sudo chmod +x "$CLI_WRAPPER"
    ok "Created global command: powerlayer"
fi

# ─────────────────────────────────────────────────────────────────────────────
step "11" "Checking desktop tray support..."
# ─────────────────────────────────────────────────────────────────────────────

DESKTOP="${XDG_CURRENT_DESKTOP:-unknown}"
info "Detected desktop: $DESKTOP"

if [[ "$DESKTOP" == *"GNOME"* ]]; then
    # Check if AppIndicator extension is enabled
    if command -v gnome-extensions &>/dev/null; then
        if gnome-extensions list --enabled 2>/dev/null | grep -q "appindicatorsupport\|ubuntu-appindicators\|AppIndicator"; then
            ok "GNOME AppIndicator extension is enabled. Tray icon will be visible."
        else
            warn "GNOME AppIndicator extension NOT detected."
            warn "The tray icon may not appear in GNOME without it."
            warn "Enable it: https://extensions.gnome.org/extension/615/appindicator-support/"
            warn "Or install: sudo ${PKG_MANAGER} install gnome-shell-extension-appindicator"
        fi
    fi
elif [[ "$DESKTOP" == *"KDE"* || "$DESKTOP" == *"Plasma"* ]]; then
    ok "KDE Plasma supports AppIndicator natively. Tray icon will appear."
elif [[ "$DESKTOP" == *"XFCE"* ]]; then
    ok "XFCE supports AppIndicator natively. Tray icon will appear."
else
    info "Desktop '$DESKTOP' — tray icon should appear if your bar supports AppIndicator/StatusNotifierItem."
fi

# ─────────────────────────────────────────────────────────────────────────────
# Final Summary
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${GREEN}══════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  PowerLayer installed successfully!                  ${RESET}"
echo -e "${BOLD}${GREEN}══════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "  ${BOLD}CLI Commands:${RESET}"
echo -e "    powerlayer status          # Live dashboard"
echo -e "    powerlayer report          # Battery savings report"
echo -e "    powerlayer explain <app>   # Why was this app throttled?"
echo -e "    powerlayer override spotify --always-allow"
echo ""
echo -e "  ${BOLD}Service Management:${RESET}"
echo -e "    systemctl --user status powerlayer"
echo -e "    systemctl --user stop powerlayer"
echo -e "    systemctl --user restart powerlayer"
echo -e "    journalctl --user -u powerlayer -f   # Live logs"
echo ""
echo -e "  ${BOLD}Uninstall:${RESET}"
echo -e "    systemctl --user disable --now powerlayer powerlayer-tray"
echo -e "    rm ~/.config/systemd/user/powerlayer*.service"
echo -e "    sudo rm -f /etc/sudoers.d/powerlayer /usr/local/bin/powerlayer"
echo ""
