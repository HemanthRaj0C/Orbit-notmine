"""
cli/tray.py
─────────────────────────────────────────────────────────────────────────────
PowerLayer Desktop Tray Indicator

Shows a system-tray icon that lets the user see PowerLayer's status at a
glance and access quick actions — just like Windows Security or Dropbox.

Compatibility:
  • GNOME  — requires the AppIndicator extension
             https://extensions.gnome.org/extension/615/appindicator-support/
  • KDE Plasma 5/6  — AppIndicator / StatusNotifierItem supported natively
  • XFCE, MATE, Cinnamon — native appindicator support
  • Sway / Waybar — via the waybar/tray module (SNI)

Run directly (for testing):
    python cli/tray.py --db data/runtime/powerlayer.db

Or let the service manage it:
    systemctl --user start powerlayer-tray
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── Graceful import of GTK / AppIndicator ─────────────────────────────────────
try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib
    _GTK = True
except ImportError:
    _GTK = False

try:
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3
    _APPINDICATOR = True
except Exception:
    _APPINDICATOR = False

try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify
    Notify.init("PowerLayer")
    _NOTIFY = True
except Exception:
    _NOTIFY = False

logger = logging.getLogger(__name__)

# ── Project root ───────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent

# ── Icon paths ─────────────────────────────────────────────────────────────────
_ICON_DIR = _PROJECT_ROOT / "assets" / "icons"
_ICON_ACTIVE   = str(_ICON_DIR / "powerlayer-active.png")
_ICON_IDLE     = str(_ICON_DIR / "powerlayer-idle.png")
_ICON_THROTTLE = str(_ICON_DIR / "powerlayer-throttle.png")
_ICON_FALLBACK = "battery-good-symbolic"           # system theme fallback


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _icon(name: str) -> str:
    """Return path if file exists, else fall back to a system theme icon."""
    if os.path.isfile(name):
        return name
    return _ICON_FALLBACK


def _send_desktop_notify(title: str, body: str, urgency: str = "normal") -> None:
    """
    Send a desktop notification via libnotify (gi) if available,
    falling back to notify-send shell command.
    """
    if _NOTIFY:
        try:
            n = Notify.Notification.new(title, body, "dialog-information")
            urgency_map = {
                "low":      Notify.Urgency.LOW,
                "normal":   Notify.Urgency.NORMAL,
                "critical": Notify.Urgency.CRITICAL,
            }
            n.set_urgency(urgency_map.get(urgency, Notify.Urgency.NORMAL))
            n.show()
            return
        except Exception:
            pass

    # Fallback: notify-send
    try:
        urgency_flag = ["--urgency", urgency]
        subprocess.run(
            ["notify-send", *urgency_flag, title, body],
            timeout=3, check=False,
        )
    except FileNotFoundError:
        logger.debug("notify-send not found — desktop notification skipped.")


def _run_cli(*args: str) -> str:
    """Run a powerlayer CLI sub-command and return its output."""
    py = sys.executable
    cli = str(_PROJECT_ROOT / "cli" / "__init__.py")
    try:
        result = subprocess.run(
            [py, cli, *args],
            capture_output=True, text=True, timeout=10
        )
        return (result.stdout + result.stderr).strip()
    except Exception as exc:
        return f"Error: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# DB reader (runs in background thread, no GTK lock needed)
# ─────────────────────────────────────────────────────────────────────────────

class StatusReader(threading.Thread):
    """
    Polls the PowerLayer DB every 15 seconds and exposes the latest status
    as a plain dict that the tray can read safely.
    """

    POLL_INTERVAL = 15          # seconds between DB polls

    def __init__(self, db_path: Path) -> None:
        super().__init__(daemon=True, name="pl-status-reader")
        self.db_path = db_path
        self._lock = threading.Lock()
        self._status: dict = {
            "running":        False,
            "battery_pct":    None,
            "active_procs":   0,
            "throttled_apps": [],
            "total_actions":  0,
            "total_savings_mah": 0,
        }
        self._stop = threading.Event()

    @property
    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self._poll()
            self._stop.wait(self.POLL_INTERVAL)

    def _poll(self) -> None:
        if not self.db_path.exists():
            return
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=3)
            conn.row_factory = sqlite3.Row

            # Battery
            row = conn.execute(
                "SELECT battery_pct FROM events ORDER BY timestamp DESC, id DESC LIMIT 1"
            ).fetchone()
            battery = row["battery_pct"] if row else None

            # Recently throttled apps (last 5 min)
            cutoff = int(time.time()) - 300
            throttled = [
                r["app_name"] for r in conn.execute(
                    """SELECT DISTINCT app_name FROM action_log
                       WHERE timestamp > ? AND action_taken='throttle'
                       ORDER BY timestamp DESC LIMIT 5""",
                    (cutoff,),
                ).fetchall()
            ]

            # Total action count
            total = conn.execute(
                "SELECT COUNT(*) FROM action_log"
            ).fetchone()[0]

            # Active procs from last 30s
            cutoff30 = int(time.time()) - 30
            active = conn.execute(
                "SELECT COUNT(DISTINCT app_name) FROM events WHERE timestamp > ?",
                (cutoff30,),
            ).fetchone()[0]

            conn.close()

            with self._lock:
                self._status = {
                    "running":        True,
                    "battery_pct":    battery,
                    "active_procs":   active,
                    "throttled_apps": throttled,
                    "total_actions":  total,
                }
        except Exception as exc:
            logger.debug("StatusReader poll error: %s", exc)
            with self._lock:
                self._status["running"] = False


# ─────────────────────────────────────────────────────────────────────────────
# Tray Application
# ─────────────────────────────────────────────────────────────────────────────

class PowerLayerTray:
    """
    AppIndicator3-based system tray icon for PowerLayer.

    Menu structure:
      ⚡ PowerLayer — Active (battery icon)
      ──────────────────────────────────
      📊 Status Dashboard...
      📋 View Report...
      ──────────────────────────────────
      ⚙  Open Config...
      🔕 Shadow Mode (toggle)
      ──────────────────────────────────
      🔄 Restart Daemon
      ❌ Quit Tray
    """

    UPDATE_INTERVAL_MS = 15_000     # update menu label every 15 s

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._reader = StatusReader(db_path)
        self._shadow_mode = True
        
        config_path = _PROJECT_ROOT / "config.yaml"
        if config_path.exists():
            try:
                import yaml
                with open(config_path) as f:
                    cfg = yaml.safe_load(f) or {}
                self._shadow_mode = bool(cfg.get("shadow_mode", True))
            except Exception:
                pass

        if not _GTK:
            logger.error("PyGObject / GTK3 not available. Cannot start tray.")
            sys.exit(1)

        if not _APPINDICATOR:
            logger.warning(
                "AppIndicator3 not available. Falling back to Gtk.StatusIcon "
                "(less reliable, deprecated in GNOME)."
            )
            self._use_status_icon = True
        else:
            self._use_status_icon = False

        self._build()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build(self) -> None:
        if not self._use_status_icon:
            self._indicator = AppIndicator3.Indicator.new(
                "powerlayer",
                _icon(_ICON_IDLE),
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            self._indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self._indicator.set_title("Orbit")
        else:
            self._status_icon = Gtk.StatusIcon.new_from_icon_name("battery-good-symbolic")
            self._status_icon.set_tooltip_text("Orbit")
            self._status_icon.connect("popup-menu", self._on_status_icon_popup)

        self._menu = self._build_menu()

        if not self._use_status_icon:
            self._indicator.set_menu(self._menu)

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()

        # ── Header label ──────────────────────────────────────────────────────
        self._lbl_status = Gtk.MenuItem(label="⚡ Orbit — Starting…")
        self._lbl_status.set_sensitive(False)
        menu.append(self._lbl_status)

        self._lbl_battery = Gtk.MenuItem(label="🔋 Battery: —")
        self._lbl_battery.set_sensitive(False)
        menu.append(self._lbl_battery)

        self._lbl_throttled = Gtk.MenuItem(label="🚦 Throttled: none")
        self._lbl_throttled.set_sensitive(False)
        menu.append(self._lbl_throttled)

        menu.append(Gtk.SeparatorMenuItem())

        # ── Actions ───────────────────────────────────────────────────────────
        item_status = Gtk.MenuItem(label="📊  Open Status Dashboard")
        item_status.connect("activate", self._on_open_status)
        menu.append(item_status)

        item_report = Gtk.MenuItem(label="📋  View Battery Report")
        item_report.connect("activate", self._on_open_report)
        menu.append(item_report)

        menu.append(Gtk.SeparatorMenuItem())

        # Shadow mode toggle
        self._item_shadow = Gtk.CheckMenuItem(label="🔕  Shadow Mode (no enforcement)")
        self._item_shadow.set_active(self._shadow_mode)
        self._item_shadow.connect("toggled", self._on_shadow_toggle)
        menu.append(self._item_shadow)

        menu.append(Gtk.SeparatorMenuItem())

        item_restart = Gtk.MenuItem(label="🔄  Restart Daemon")
        item_restart.connect("activate", self._on_restart)
        menu.append(item_restart)

        item_logs = Gtk.MenuItem(label="📜  Follow Logs…")
        item_logs.connect("activate", self._on_show_logs)
        menu.append(item_logs)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="❌  Quit Tray")
        item_quit.connect("activate", self._on_quit)
        menu.append(item_quit)

        menu.show_all()
        return menu

    # ── Periodic update ───────────────────────────────────────────────────────

    def _schedule_update(self) -> None:
        GLib.timeout_add(self.UPDATE_INTERVAL_MS, self._update_menu)

    def _update_menu(self) -> bool:
        """Called every UPDATE_INTERVAL_MS ms on the GTK main thread."""
        st = self._reader.status

        # Status label
        if st["running"]:
            icon = _icon(_ICON_THROTTLE) if st["throttled_apps"] else _icon(_ICON_ACTIVE)
            self._lbl_status.set_label("⚡ Orbit — Active")
        else:
            icon = _icon(_ICON_IDLE)
            self._lbl_status.set_label("⚡ Orbit — Idle / No DB")

        # Battery
        bat = st.get("battery_pct")
        bat_str = f"{bat:.0f}%" if bat is not None else "—"
        self._lbl_battery.set_label(f"🔋 Battery: {bat_str}  |  {st['active_procs']} apps tracked")

        # Throttled
        thr = st.get("throttled_apps", [])
        if thr:
            self._lbl_throttled.set_label(f"🚦 Throttled: {', '.join(thr[:3])}")
        else:
            self._lbl_throttled.set_label("🚦 Throttled: none")

        # Update icon
        if not self._use_status_icon and _APPINDICATOR:
            self._indicator.set_icon_full(icon, "PowerLayer status icon")

        return True     # Keep the timeout running

    # ── Menu callbacks ────────────────────────────────────────────────────────

    def _on_open_status(self, _item: Gtk.MenuItem) -> None:
        """Open a terminal running `powerlayer status`."""
        py = sys.executable
        cli = str(_PROJECT_ROOT / "cli" / "__init__.py")
        _launch_terminal(f"{py} {cli} --db {self._db_path} status")

    def _on_open_report(self, _item: Gtk.MenuItem) -> None:
        py = sys.executable
        cli = str(_PROJECT_ROOT / "cli" / "__init__.py")
        _launch_terminal(f"{py} {cli} --db {self._db_path} report")

    def _on_shadow_toggle(self, item: Gtk.CheckMenuItem) -> None:
        self._shadow_mode = item.get_active()
        state = "enabled" if self._shadow_mode else "disabled"
        _send_desktop_notify("PowerLayer", f"Shadow mode {state}.")
        logger.info("Shadow mode %s via tray.", state)
        
        # Write to config.yaml
        config_path = _PROJECT_ROOT / "config.yaml"
        if config_path.exists():
            try:
                import yaml
                with open(config_path, "r") as f:
                    cfg = yaml.safe_load(f) or {}
                cfg["shadow_mode"] = self._shadow_mode
                with open(config_path, "w") as f:
                    yaml.safe_dump(cfg, f, default_flow_style=False)
            except Exception as e:
                logger.error("Failed to save shadow_mode to config.yaml: %s", e)

    def _on_restart(self, _item: Gtk.MenuItem) -> None:
        try:
            subprocess.run(
                ["systemctl", "--user", "restart", "powerlayer"],
                timeout=10, check=False
            )
            _send_desktop_notify("PowerLayer", "Daemon restarted.")
        except Exception as exc:
            _send_desktop_notify("PowerLayer", f"Restart failed: {exc}", "critical")

    def _on_show_logs(self, _item: Gtk.MenuItem) -> None:
        _launch_terminal("journalctl --user -u powerlayer -f")

    def _on_quit(self, _item: Gtk.MenuItem) -> None:
        self._reader.stop()
        Gtk.main_quit()

    # ── Fallback StatusIcon ───────────────────────────────────────────────────

    def _on_status_icon_popup(self, icon, button, time):
        self._menu.popup(None, None, None, None, button, time)

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self) -> None:
        self._reader.start()
        self._schedule_update()
        GLib.timeout_add(500, self._update_menu)   # first update quickly
        _send_desktop_notify("Orbit", "Tray indicator started. ⚡")
        Gtk.main()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _launch_terminal(cmd: str) -> None:
    """
    Launch a command in the user's preferred terminal emulator.
    Tries modern terminals first (kitty, alacritty, wezterm, foot),
    then desktop defaults (gnome-terminal, konsole, xfce4-terminal, xterm).
    """
    terms = ["kitty", "alacritty", "wezterm", "foot", "gnome-terminal", "konsole", "xfce4-terminal", "xterm"]
    for term in terms:
        if not _which(term):
            continue
        try:
            if term == "gnome-terminal":
                subprocess.Popen(["gnome-terminal", "--", "bash", "-c", f"{cmd}; read -p 'Press Enter…'"])
            elif term == "konsole":
                subprocess.Popen(["konsole", "-e", "bash", "-c", f"{cmd}; read -p 'Press Enter…'"])
            elif term in ["kitty", "alacritty", "wezterm", "foot"]:
                subprocess.Popen([term, "bash", "-c", f"{cmd}; read -p 'Press Enter…'"])
            else:
                subprocess.Popen([term, "-e", f"bash -c '{cmd}; read -p Press Enter…'"])
            return
        except Exception:
            continue
    logger.warning("No suitable terminal emulator found.")


def _which(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="PowerLayer desktop tray indicator"
    )
    parser.add_argument(
        "--db",
        default=str(_PROJECT_ROOT / "data" / "runtime" / "sandbox.db"),
        help="Path to the PowerLayer runtime DB",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db = Path(args.db)
    if not db.parent.exists():
        db.parent.mkdir(parents=True)

    app = PowerLayerTray(db_path=db)
    app.run()


if __name__ == "__main__":
    main()
