"""
tests/test_model.py
───────────────────
Tests for the PowerLayer model layer:
  - Feature engineering (shape, column names, cold-start, encoding)
  - Base model (train, predict, batch predict, feature importances)
  - Correction layer (factor application, DB roundtrip, cache)
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from model.features import (
    engineer_features,
    engineer_single,
    encode_labels,
    decode_labels,
    get_feature_names,
    LABEL_CLASSES,
    RAW_FEATURE_COLS,
)
from model.base_model import PowerLayerModel
from model.correction_layer import CorrectionLayer


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_sample_df(n: int = 60) -> pd.DataFrame:
    """Create a minimal synthetic DataFrame for testing."""
    rng = np.random.default_rng(0)
    labels = (["active_needed"] * (n // 3) +
              ["idle_likely"] * (n // 3) +
              ["background_unused"] * (n - 2 * (n // 3)))
    categories = ["browser", "streaming", "development_tool",
                  "cloud_sync", "communication", "system_utility"]
    return pd.DataFrame({
        "time_since_last_foreground": rng.integers(10, 30000, n).astype(float),
        "sync_freq_ratio":            rng.uniform(0.05, 2.5, n),
        # Plain Python str — matches what pd.read_csv returns
        "within_typical_active_hours": ["true" if v else "false"
                                        for v in rng.integers(0, 2, n).tolist()],
        "cpu_pct_relative":           rng.uniform(0.05, 2.8, n),
        "app_category":               [categories[i] for i in
                                       rng.integers(0, len(categories), n).tolist()],
        "label": labels,
    })


# ── Test runner ───────────────────────────────────────────────────────────────

_results: list[tuple[str, bool, str]] = []


def test(name: str):
    """Decorator to register tests."""
    def decorator(fn):
        try:
            fn()
            _results.append((name, True, ""))
            print(f"  ✓ {name}")
        except Exception as exc:
            _results.append((name, False, str(exc)))
            print(f"  ✗ {name}")
            print(f"      {exc}")
        return fn
    return decorator


# ── Feature engineering tests ─────────────────────────────────────────────────

print("\n── Feature Engineering ──────────────────────────────")

@test("engineer_features: correct output columns")
def _():
    df = _make_sample_df(30)
    X = engineer_features(df)
    expected = get_feature_names()
    assert list(X.columns) == expected, f"Got {list(X.columns)}"


@test("engineer_features: no NaN values")
def _():
    df = _make_sample_df(30)
    X = engineer_features(df)
    assert not X.isnull().any().any(), "NaN values found in engineered features"


@test("engineer_features: shape matches (n_rows, n_features)")
def _():
    n = 50
    df = _make_sample_df(n)
    X = engineer_features(df)
    assert X.shape == (n, len(get_feature_names())), f"Shape mismatch: {X.shape}"


@test("engineer_features: one-hot category columns are 0/1 only")
def _():
    df = _make_sample_df(30)
    X = engineer_features(df)
    cat_cols = [c for c in X.columns if c.startswith("cat_")]
    assert set(X[cat_cols].values.flatten()).issubset({0.0, 1.0})


@test("engineer_features: bool column from 'true'/'false' strings")
def _():
    df = _make_sample_df(20)
    df["within_typical_active_hours"] = "true"
    X = engineer_features(df)
    assert (X["within_typical_active_hours"] == 1.0).all()


@test("engineer_single: returns single-row DataFrame")
def _():
    event = {
        "time_since_last_foreground": 120,
        "sync_freq_ratio": 1.2,
        "within_typical_active_hours": True,
        "cpu_pct_relative": 1.8,
        "app_category": "browser",
    }
    X = engineer_single(event)
    assert X.shape == (1, len(get_feature_names()))


@test("engineer_single: cold-start (no history) defaults trend to 0.0")
def _():
    event = {
        "time_since_last_foreground": 60,
        "sync_freq_ratio": 1.0,
        "within_typical_active_hours": True,
        "cpu_pct_relative": 1.0,
        "app_category": "system_utility",
    }
    X = engineer_single(event, cpu_history=None, history_count=0)
    assert X["cpu_trend_last_3_cycles"].iloc[0] == 0.0


@test("engineer_single: cpu_history computes correct trend direction")
def _():
    event = {
        "time_since_last_foreground": 30,
        "sync_freq_ratio": 1.1,
        "within_typical_active_hours": True,
        "cpu_pct_relative": 1.5,
        "app_category": "streaming",
    }
    # Rising: 0.5 → 1.0 → 1.5 → mean delta = +0.5
    X = engineer_single(event, cpu_history=[0.5, 1.0, 1.5])
    assert X["cpu_trend_last_3_cycles"].iloc[0] > 0, "Rising history should give positive trend"


@test("encode_labels / decode_labels: roundtrip")
def _():
    labels = pd.Series(["active_needed", "idle_likely", "background_unused"])
    encoded = encode_labels(labels)
    decoded = decode_labels(encoded)
    assert list(decoded) == list(labels)


# ── Base model tests ──────────────────────────────────────────────────────────

print("\n── Base Model ───────────────────────────────────────")

@test("model.predict: cold-start returns idle_likely with 0.0 confidence")
def _():
    model = PowerLayerModel()
    label, conf = model.predict({"app_category": "browser"})
    assert label == "idle_likely"
    assert conf == 0.0


@test("model.train: fits without errors on small dataset")
def _():
    df = _make_sample_df(90)
    model = PowerLayerModel()
    stats = model.train(df)
    assert stats["n_samples"] == 90
    assert model.is_ready


@test("model.predict: returns valid label + confidence in (0,1]")
def _():
    df = _make_sample_df(90)
    model = PowerLayerModel()
    model.train(df)
    event = {
        "time_since_last_foreground": 30,
        "sync_freq_ratio": 1.3,
        "within_typical_active_hours": True,
        "cpu_pct_relative": 2.1,
        "app_category": "streaming",
    }
    label, conf = model.predict(event)
    assert label in LABEL_CLASSES, f"Bad label: {label}"
    assert 0.0 < conf <= 1.0, f"Bad confidence: {conf}"


@test("model.predict_batch: shape matches input")
def _():
    df = _make_sample_df(60)
    model = PowerLayerModel()
    model.train(df)
    labels, confs = model.predict_batch(df)
    assert len(labels) == 60
    assert len(confs) == 60
    assert all(l in LABEL_CLASSES for l in labels)


@test("model.predict_batch: accuracy > 60% on clean training data")
def _():
    df = _make_sample_df(150)
    model = PowerLayerModel()
    model.train(df)
    labels, _ = model.predict_batch(df)
    acc = (labels == df["label"].values).mean()
    assert acc > 0.60, f"Accuracy too low: {acc:.2f}"


@test("model.save / load: roundtrip prediction consistency")
def _():
    df = _make_sample_df(90)
    model = PowerLayerModel()
    model.train(df)
    event = {
        "time_since_last_foreground": 100,
        "sync_freq_ratio": 0.9,
        "within_typical_active_hours": True,
        "cpu_pct_relative": 0.5,
        "app_category": "cloud_sync",
    }
    label1, conf1 = model.predict(event)

    with tempfile.NamedTemporaryFile(suffix=".joblib", delete=False) as f:
        tmp = Path(f.name)
    model.save(tmp)

    model2 = PowerLayerModel()
    model2.load(tmp)
    label2, conf2 = model2.predict(event)
    tmp.unlink(missing_ok=True)

    assert label1 == label2, f"Labels differ after reload: {label1} vs {label2}"
    assert abs(conf1 - conf2) < 1e-6


@test("model.feature_importances: returns all feature names summing to ~1.0")
def _():
    df = _make_sample_df(90)
    model = PowerLayerModel()
    model.train(df)
    imps = model.feature_importances()
    assert set(imps.keys()) == set(get_feature_names())
    total = sum(imps.values())
    assert abs(total - 1.0) < 1e-6, f"Importances sum to {total}"


# ── Correction layer tests ────────────────────────────────────────────────────

print("\n── Correction Layer ─────────────────────────────────")

def _make_correction_db() -> tuple[sqlite3.Connection, Path]:
    tmp = Path(tempfile.mktemp(suffix=".db"))
    conn = sqlite3.connect(str(tmp))
    conn.execute("""
        CREATE TABLE user_corrections (
            app_name           TEXT PRIMARY KEY,
            correction_factor  REAL    NOT NULL DEFAULT 1.0,
            observation_count  INTEGER NOT NULL DEFAULT 0,
            last_updated_ts    INTEGER
        )
    """)
    conn.commit()
    return conn, tmp


@test("correction_layer: no correction → passthrough")
def _():
    conn, tmp = _make_correction_db()
    cl = CorrectionLayer(tmp)
    label, conf = cl.apply("spotify", "active_needed", 0.82)
    assert label == "active_needed"
    assert abs(conf - 0.82) < 1e-6
    conn.close(); tmp.unlink(missing_ok=True)


@test("correction_layer: factor >1 protects active_needed apps")
def _():
    conn, tmp = _make_correction_db()
    cl = CorrectionLayer(tmp)
    cl.set_correction("spotify", 5.0, conn)
    # Model says background_unused with modest confidence;
    # factor=5.0 on active_needed should flip it.
    label, conf = cl.apply("spotify", "background_unused", 0.50)
    assert label == "active_needed", f"Expected active_needed, got {label}"
    conn.close(); tmp.unlink(missing_ok=True)


@test("correction_layer: factor <1 promotes throttling")
def _():
    conn, tmp = _make_correction_db()
    cl = CorrectionLayer(tmp)
    cl.set_correction("updater", 0.01, conn)
    # Model said active_needed; correction should move it away
    label, conf = cl.apply("updater", "active_needed", 0.55)
    assert label != "active_needed", f"Expected throttled label, got {label}"
    conn.close(); tmp.unlink(missing_ok=True)


@test("correction_layer: DB roundtrip stores and reloads factor")
def _():
    conn, tmp = _make_correction_db()
    cl = CorrectionLayer(tmp)
    cl.set_correction("code", 2.5, conn)
    # Force cache expiry
    cl._loaded_at = 0.0
    factor = cl.get_factor("code")
    assert abs(factor - 2.5) < 1e-6
    conn.close(); tmp.unlink(missing_ok=True)


# ── Summary ───────────────────────────────────────────────────────────────────

passed = sum(1 for _, ok, _ in _results if ok)
failed = sum(1 for _, ok, _ in _results if not ok)

print(f"\n{'═'*43}")
print(f"  Results: {passed} passed, {failed} failed")
print(f"{'═'*43}\n")

if failed:
    sys.exit(1)
