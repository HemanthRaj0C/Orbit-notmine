"""
model/base_model.py
───────────────────
Random Forest classifier for PowerLayer process state prediction.

Predicts one of three labels for any running process:
  - active_needed    → user is actively using this app; don't touch it
  - idle_likely      → app hasn't been used recently; light throttle OK
  - background_unused→ app is a background task; aggressive throttle safe

Design decisions (per advisor review):
  - class_weight="balanced": corrects for natural label imbalance between
    personas (light_user has mostly background_unused; streamer has mostly
    active_needed). Without this, the RF biases toward the majority class.
  - n_estimators=200: enough trees to be stable; not so many that inference
    slows the collector's 7s poll cycle.
  - max_depth=12: prevents overfitting on the ~1400-row synthetic dataset.
  - min_samples_leaf=5: no leaf fires on fewer than 5 training examples.
  - Model is saved with joblib (not pickle) for numpy array compatibility.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import LabelEncoder

from model.features import (
    engineer_features,
    engineer_single,
    encode_labels,
    decode_labels,
    LABEL_CLASSES,
    get_feature_names,
)

logger = logging.getLogger(__name__)

# Default path for the trained model artifact
_DEFAULT_MODEL_PATH = Path(__file__).parent / "artifacts" / "base_model.joblib"


class PowerLayerModel:
    """
    Thin wrapper around a scikit-learn RandomForestClassifier.

    Responsibilities:
      - Feature engineering (delegates to model/features.py)
      - Training + serialisation
      - Inference with confidence score
      - Graceful cold-start: returns (idle_likely, 0.0) if not trained yet
    """

    def __init__(self, model_path: Path | None = None) -> None:
        self.model_path = Path(model_path) if model_path else _DEFAULT_MODEL_PATH
        self._clf: RandomForestClassifier | None = None
        self._is_loaded = False

    # ── Training ──────────────────────────────────────────────────────────────

    def train(self, df: pd.DataFrame) -> dict[str, Any]:
        """
        Train on a combined DataFrame (all personas merged).

        Parameters
        ----------
        df : DataFrame containing RAW_FEATURE_COLS + 'label' column.

        Returns
        -------
        dict with 'n_samples', 'class_counts', 'feature_names'.
        """
        logger.info("Training base model on %d samples…", len(df))

        X = engineer_features(df)
        y = encode_labels(df["label"])

        self._clf = RandomForestClassifier(
            n_estimators=200,
            max_depth=12,
            min_samples_leaf=5,
            class_weight="balanced",   # corrects for persona label imbalance
            random_state=42,
            n_jobs=-1,                 # use all CPU cores for training
        )
        self._clf.fit(X, y)
        self._is_loaded = True

        # Class distribution for reporting
        unique, counts = np.unique(y, return_counts=True)
        class_counts = {LABEL_CLASSES[int(u)]: int(c) for u, c in zip(unique, counts)}

        logger.info("Training complete. Class distribution: %s", class_counts)
        return {
            "n_samples":     len(df),
            "class_counts":  class_counts,
            "feature_names": get_feature_names(),
        }

    # ── Serialisation ─────────────────────────────────────────────────────────

    def save(self, path: Path | None = None) -> Path:
        """Persist the trained classifier to disk with joblib."""
        if self._clf is None:
            raise RuntimeError("Model has not been trained yet.")
        dest = Path(path) if path else self.model_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self._clf, dest)
        logger.info("Model saved → %s", dest)
        return dest

    def load(self, path: Path | None = None) -> None:
        """Load a previously saved classifier from disk."""
        src = Path(path) if path else self.model_path
        if not src.exists():
            raise FileNotFoundError(f"No model artifact at {src}")
        self._clf = joblib.load(src)
        self._is_loaded = True
        logger.info("Model loaded ← %s", src)

    @property
    def is_ready(self) -> bool:
        return self._is_loaded and self._clf is not None

    # ── Inference ─────────────────────────────────────────────────────────────

    def predict(
        self,
        event: dict[str, Any],
        cpu_history: list[float] | None = None,
        history_count: int = 0,
    ) -> tuple[str, float]:
        """
        Predict the process state for a single live event.

        Parameters
        ----------
        event         : dict from the collector (see features.engineer_single)
        cpu_history   : last ≤3 cpu_pct_relative readings for trend feature
        history_count : number of historical DB rows for this app.
                        If <10 → cold-start; ratios already default to 1.0
                        in engineer_single, so prediction still works.

        Returns
        -------
        (label_str, confidence_float)
          label_str   : one of LABEL_CLASSES
          confidence  : probability of the predicted class (0.0–1.0)
        """
        if not self.is_ready:
            # Cold-start default: assume idle (safe; never throttles active work)
            logger.debug("Model not ready — returning cold-start default.")
            return ("idle_likely", 0.0)

        X = engineer_single(event, cpu_history=cpu_history,
                            history_count=history_count)
        proba = self._clf.predict_proba(X)[0]
        class_idx = int(np.argmax(proba))
        confidence = float(proba[class_idx])
        label = LABEL_CLASSES[class_idx]

        logger.debug("predict(%s) → %s  (conf=%.2f)",
                     event.get("app_name", "?"), label, confidence)
        return (label, confidence)

    def predict_batch(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """
        Batch prediction for evaluation / testing.

        Returns
        -------
        (labels_array, confidence_array)
        """
        if not self.is_ready:
            raise RuntimeError("Model not ready. Train or load first.")
        X = engineer_features(df)
        proba = self._clf.predict_proba(X)
        class_idx = np.argmax(proba, axis=1)
        confidence = proba[np.arange(len(proba)), class_idx]
        labels = np.array([LABEL_CLASSES[i] for i in class_idx])
        return labels, confidence

    # ── Feature importance ────────────────────────────────────────────────────

    def feature_importances(self) -> dict[str, float]:
        """Return dict of feature_name → importance score (sums to 1.0)."""
        if not self.is_ready:
            return {}
        names = get_feature_names()
        importances = self._clf.feature_importances_
        return dict(sorted(
            zip(names, importances),
            key=lambda x: x[1], reverse=True
        ))
