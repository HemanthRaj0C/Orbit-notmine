"""
model/correction_layer.py
─────────────────────────
Per-app user correction bias layer.

After the base Random Forest makes a prediction, this layer applies a
lightweight per-app adjustment based on explicit user overrides stored in
the `user_corrections` SQLite table.

How it works:
  - User runs: `powerlayer override spotify --always-allow`
  - CLI writes a correction_factor to user_corrections: {app: spotify, factor: 2.0}
  - CorrectionLayer.apply() multiplies the model's raw probability for
    active_needed by 2.0 before argmax, making it harder to throttle spotify
  - No retraining needed; purely inference-time adjustment
  - Correction factors are cached in-memory and reloaded from DB periodically

Design principle: correction_factor > 1.0 → user wants app PROTECTED
                  correction_factor < 1.0 → user wants app THROTTLED more
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np

from model.features import LABEL_CLASSES

logger = logging.getLogger(__name__)

# Reload correction factors from DB every N seconds
_CACHE_TTL_SECONDS = 60


class CorrectionLayer:
    """
    Applies per-app bias corrections to model probability outputs.
    Uses the user_corrections table written by the CLI.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path   = Path(db_path)
        self._cache:    dict[str, float] = {}   # app_name → correction_factor
        self._loaded_at: float = 0.0

    # ── Cache management ──────────────────────────────────────────────────────

    def _reload_if_stale(self) -> None:
        """Reload from DB if cache is older than _CACHE_TTL_SECONDS."""
        if time.time() - self._loaded_at < _CACHE_TTL_SECONDS:
            return
        self._reload()

    def _reload(self) -> None:
        """Read all active correction factors from the DB."""
        if not self._db_path.exists():
            self._cache = {}
            self._loaded_at = time.time()
            return
        try:
            conn = sqlite3.connect(str(self._db_path))
            rows = conn.execute(
                "SELECT app_name, correction_factor FROM user_corrections "
                "WHERE observation_count > 0"
            ).fetchall()
            conn.close()
            self._cache = {row[0]: float(row[1]) for row in rows}
            self._loaded_at = time.time()
            logger.debug("CorrectionLayer: loaded %d corrections.", len(self._cache))
        except Exception as exc:
            logger.warning("CorrectionLayer: DB read failed: %s", exc)
            self._cache = {}
            self._loaded_at = time.time()

    def get_factor(self, app_name: str) -> float:
        """Return correction factor for app (1.0 = no correction)."""
        self._reload_if_stale()
        return self._cache.get(app_name, 1.0)

    # ── Core correction ───────────────────────────────────────────────────────

    def apply(
        self,
        app_name: str,
        label: str,
        confidence: float,
    ) -> tuple[str, float]:
        """
        Apply user correction to a model prediction.

        Parameters
        ----------
        app_name   : process name from collector
        label      : predicted label from base model
        confidence : model confidence (0.0–1.0)

        Returns
        -------
        (corrected_label, corrected_confidence)
        """
        factor = self.get_factor(app_name)
        if factor == 1.0:
            return (label, confidence)   # no correction — fast path

        # Rebuild a synthetic probability vector and apply the factor
        # to the active_needed slot, then re-normalise.
        proba = np.zeros(len(LABEL_CLASSES))
        label_idx = LABEL_CLASSES.index(label) if label in LABEL_CLASSES else 1
        proba[label_idx] = confidence
        # Distribute remaining probability equally among other classes
        remaining = (1.0 - confidence) / max(len(LABEL_CLASSES) - 1, 1)
        for i in range(len(LABEL_CLASSES)):
            if i != label_idx:
                proba[i] = remaining

        # Apply correction to active_needed class
        active_idx = LABEL_CLASSES.index("active_needed")
        proba[active_idx] *= factor

        # Re-normalise so probabilities sum to 1
        total = proba.sum()
        if total > 0:
            proba /= total

        new_idx = int(np.argmax(proba))
        new_label = LABEL_CLASSES[new_idx]
        new_conf  = float(proba[new_idx])

        if new_label != label:
            logger.info(
                "CorrectionLayer: %s  %s→%s  (factor=%.2f)",
                app_name, label, new_label, factor
            )
        return (new_label, new_conf)

    # ── Write corrections (used by CLI) ───────────────────────────────────────

    def set_correction(
        self,
        app_name: str,
        factor: float,
        db_conn: sqlite3.Connection,
    ) -> None:
        """
        Write or update a correction factor for an app.
        Called by the CLI `powerlayer override` command.

        factor > 1.0 → protect app (harder to throttle)
        factor < 1.0 → be aggressive with app (easier to throttle)
        factor = 1.0 → reset to default
        """
        db_conn.execute(
            """
            INSERT INTO user_corrections (app_name, correction_factor, observation_count)
            VALUES (?, ?, 1)
            ON CONFLICT(app_name) DO UPDATE SET
                correction_factor  = excluded.correction_factor,
                observation_count  = observation_count + 1,
                last_updated_ts    = CAST(strftime('%s','now') AS INTEGER)
            """,
            (app_name, factor),
        )
        db_conn.commit()
        # Invalidate cache so next predict picks up the change
        self._cache[app_name] = factor
        logger.info("Correction set: %s → factor=%.2f", app_name, factor)
