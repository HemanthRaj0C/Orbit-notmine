"""
cli/display.py
──────────────
Terminal rendering helpers — colours, tables, progress bars.
Used by all CLI commands for consistent output.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from typing import Any

# ── Colour codes ──────────────────────────────────────────────────────────────
# Automatically disabled when not writing to a real terminal (pipes/redirection)

_IS_TTY = os.isatty(1)


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text


def green(t: str)  -> str: return _c("92", t)
def yellow(t: str) -> str: return _c("93", t)
def red(t: str)    -> str: return _c("91", t)
def blue(t: str)   -> str: return _c("94", t)
def cyan(t: str)   -> str: return _c("96", t)
def bold(t: str)   -> str: return _c("1",  t)
def dim(t: str)    -> str: return _c("2",  t)
def magenta(t: str)-> str: return _c("95", t)


# ── Layout helpers ────────────────────────────────────────────────────────────

def terminal_width() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def rule(char: str = "─", color_fn=dim) -> None:
    print(color_fn(char * terminal_width()))


def header(title: str, subtitle: str = "") -> None:
    width = terminal_width()
    rule("═", blue)
    pad = (width - len(title) - 2) // 2
    print(blue(bold(f"{'':>{pad}}  {title}")))
    if subtitle:
        print(dim(f"  {subtitle}"))
    rule("═", blue)


def section(title: str) -> None:
    print(f"\n{bold(cyan('  ' + title))}")
    print(dim("  " + "─" * (terminal_width() - 4)))


def kv(key: str, value: str, color_fn=None) -> None:
    val = color_fn(value) if color_fn else value
    print(f"  {dim(key + ':')}  {val}")


# ── Table ─────────────────────────────────────────────────────────────────────

def table(rows: list[dict], columns: list[tuple[str, str, int]]) -> None:
    """
    Print a formatted table.

    Parameters
    ----------
    rows    : list of dicts
    columns : list of (key, header, width) tuples
    """
    # Header row
    header_line = "  "
    sep_line    = "  "
    for key, header_text, width in columns:
        header_line += bold(f"{header_text:<{width}}")  + "  "
        sep_line    += dim("─" * width) + "  "
    print(header_line)
    print(sep_line)

    # Data rows
    for row in rows:
        line = "  "
        for key, _, width in columns:
            val = str(row.get(key, ""))[:width]
            # Colour the action column
            if key == "action":
                if val == "throttle":  val = yellow(f"{val:<{width}}")
                elif val == "allow":   val = green(f"{val:<{width}}")
                else:                  val = dim(f"{val:<{width}}")
            elif key == "label":
                if "active" in val:    val = green(f"{val:<{width}}")
                elif "background" in val: val = red(f"{val:<{width}}")
                else:                  val = yellow(f"{val:<{width}}")
            else:
                val = f"{val:<{width}}"
            line += val + "  "
        print(line)


# ── Progress bar ──────────────────────────────────────────────────────────────

def bar(value: float, width: int = 20, color_fn=green) -> str:
    """Return a filled progress bar string for a 0.0–1.0 value."""
    filled = int(value * width)
    empty  = width - filled
    return color_fn("█" * filled) + dim("░" * empty)


# ── Timestamp ─────────────────────────────────────────────────────────────────

def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def fmt_datetime(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_age(ts: int) -> str:
    """Return human-readable age like '2m ago' or '3h ago'."""
    age = int(__import__("time").time()) - ts
    if age < 60:   return f"{age}s ago"
    if age < 3600: return f"{age//60}m ago"
    return f"{age//3600}h ago"
