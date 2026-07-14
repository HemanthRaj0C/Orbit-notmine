"""
model/features.py
─────────────────
Feature engineering pipeline for PowerLayer.

Design principles (per advisor review):
  - Use RELATIVE metrics (ratios) not absolutes — transfers across hardware.
  - Cold-start safe: apps with <10 historical events default ratios to 1.0
    instead of dividing by zero.
  - Temporal trend feature (cpu_trend_last_3_cycles): captures whether CPU
    is rising, flat, or falling — stateless snapshots miss this context.
  - Category is ONE-HOT encoded so the RF learns resource patterns, not
    just category strings (prevents "development_tool → always active" bias).
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Label encoding ────────────────────────────────────────────────────────────

LABEL_CLASSES = ["active_needed", "idle_likely", "background_unused"]
LABEL_TO_INT  = {c: i for i, c in enumerate(LABEL_CLASSES)}
INT_TO_LABEL  = {i: c for c, i in LABEL_TO_INT.items()}

# ── Known app categories (for consistent one-hot columns) ─────────────────────

KNOWN_CATEGORIES = [
    "browser",
    "cloud_sync",
    "communication",
    "development_tool",
    "streaming",
    "system_utility",
]

# ── Base feature columns expected from the dataset CSV ───────────────────────

RAW_FEATURE_COLS = [
    "time_since_last_foreground",  # seconds since app was in focus
    "sync_freq_ratio",             # current sync rate / app's historical avg
    "within_typical_active_hours", # bool: is it within user's normal work hours?
    "cpu_pct_relative",            # current CPU / app's historical avg CPU
    "app_category",                # categorical: browser, streaming, etc.
]

# ── Full engineered feature names (used for column ordering in model) ─────────

def get_feature_names() -> list[str]:
    """Return ordered list of all feature column names after engineering."""
    base = [
        "time_since_last_foreground",
        "sync_freq_ratio",
        "within_typical_active_hours",
        "cpu_pct_relative",
        "cpu_trend_last_3_cycles",      # temporal: positive=rising, negative=falling
        "is_high_cpu_relative",         # binary: cpu_pct_relative > 1.5
        "is_stale",                     # binary: time_since_last_foreground > 3600s
    ]
    category_cols = [f"cat_{c}" for c in KNOWN_CATEGORIES]
    return base + category_cols


# ── Core engineering functions ────────────────────────────────────────────────

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Transform a raw DataFrame (with RAW_FEATURE_COLS columns) into the
    full engineered feature matrix.

    Parameters
    ----------
    df : DataFrame with at minimum the columns in RAW_FEATURE_COLS.
         May also contain 'cpu_trend_last_3_cycles' if pre-computed from
         a live sliding window; if absent, defaults to 0.0 (flat trend).

    Returns
    -------
    DataFrame with columns matching get_feature_names().
    """
    out = pd.DataFrame(index=df.index)

    # ── Passthrough base features ─────────────────────────────────────────────
    out["time_since_last_foreground"] = pd.to_numeric(
        df["time_since_last_foreground"], errors="coerce"
    ).fillna(0.0)
    out["sync_freq_ratio"] = pd.to_numeric(
        df["sync_freq_ratio"], errors="coerce"
    ).fillna(1.0)

    # Boolean column: handles bool, numpy bool_, "true"/"false" str, 0/1 int.
    # pandas 3.x may use StringDtype (not object), so we cannot rely on .astype(float).
    active_hours = df["within_typical_active_hours"]
    if pd.api.types.is_bool_dtype(active_hours):
        out["within_typical_active_hours"] = active_hours.astype(float)
    else:
        # Convert to Python str first to normalise StringDtype → object
        out["within_typical_active_hours"] = (
            active_hours
            .astype(str)           # works for both object and StringDtype
            .str.strip()
            .str.lower()
            .map({"true": 1.0, "false": 0.0, "1": 1.0, "0": 0.0,
                  "yes": 1.0, "no": 0.0})
            .fillna(0.0)
        )

    out["cpu_pct_relative"] = pd.to_numeric(
        df["cpu_pct_relative"], errors="coerce"
    ).fillna(1.0)

    # ── Temporal trend feature ────────────────────────────────────────────────
    # Positive  → CPU rising over last 3 snapshots
    # Zero      → flat / unknown (cold-start safe default)
    # Negative  → CPU falling (app winding down)
    if "cpu_trend_last_3_cycles" in df.columns:
        out["cpu_trend_last_3_cycles"] = df["cpu_trend_last_3_cycles"].astype(float)
    else:
        out["cpu_trend_last_3_cycles"] = 0.0

    # ── Derived binary flags ──────────────────────────────────────────────────
    out["is_high_cpu_relative"] = (out["cpu_pct_relative"] > 1.5).astype(float)
    out["is_stale"]             = (out["time_since_last_foreground"] > 3600).astype(float)

    # ── One-hot encode app_category ───────────────────────────────────────────
    # Ensures consistent columns even if a new category appears at inference time.
    cat_series = df["app_category"].astype(str).str.lower().str.strip()
    for cat in KNOWN_CATEGORIES:
        out[f"cat_{cat}"] = (cat_series == cat).astype(float)

    return out[get_feature_names()]


def engineer_single(event: dict[str, Any],
                    cpu_history: list[float] | None = None,
                    history_count: int = 0) -> pd.DataFrame:
    """
    Engineer features for a single live event dict (from the collector).

    Parameters
    ----------
    event        : dict with keys matching RAW_FEATURE_COLS
    cpu_history  : last 3 cpu_pct_relative readings (oldest → newest)
                   Used to compute cpu_trend_last_3_cycles.
                   If None or len < 2, trend defaults to 0.0 (flat).
    history_count: total number of historical events seen for this app.
                   If < 10, ratios are left as-is (not divided by zero).
                   Cold-start: feature defaults are already in event dict.

    Returns
    -------
    Single-row DataFrame ready for model.predict().
    """
    row = {
        "time_since_last_foreground": float(event.get("time_since_last_foreground", 0)),
        "sync_freq_ratio":            float(event.get("sync_freq_ratio", 1.0)),
        "within_typical_active_hours": 1.0 if event.get("within_typical_active_hours", True) else 0.0,
        "cpu_pct_relative":           float(event.get("cpu_pct_relative", 1.0)),
        "app_category":               str(event.get("app_category", "system_utility")),
    }

    # Compute temporal trend from sliding window history
    if cpu_history and len(cpu_history) >= 2:
        deltas = [cpu_history[i+1] - cpu_history[i] for i in range(len(cpu_history)-1)]
        row["cpu_trend_last_3_cycles"] = float(np.mean(deltas))
    else:
        row["cpu_trend_last_3_cycles"] = 0.0

    df = pd.DataFrame([row])
    return engineer_features(df)


def encode_labels(series: pd.Series) -> np.ndarray:
    """Map label strings → integer class indices."""
    return series.map(LABEL_TO_INT).astype(int).values


def decode_labels(arr: np.ndarray) -> list[str]:
    """Map integer class indices → label strings."""
    return [INT_TO_LABEL.get(int(i), "unknown") for i in arr]
