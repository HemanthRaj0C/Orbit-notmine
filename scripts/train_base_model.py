"""
scripts/train_base_model.py
───────────────────────────
Train the PowerLayer base Random Forest classifier on persona datasets.

Usage:
    python scripts/train_base_model.py
    python scripts/train_base_model.py --data-dir data/personas --out model/artifacts/base_model.joblib

What it does:
  1. Loads all persona CSVs from data/personas/
  2. Shuffles + stratified 80/20 train-test split
  3. Adds cpu_trend_last_3_cycles synthetic trend values for training rows
     (simulated since CSVs are static snapshots)
  4. Trains RandomForest with class_weight='balanced'
  5. Prints a full classification report + feature importances
  6. Saves the trained model to model/artifacts/base_model.joblib
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import StratifiedKFold, train_test_split

# ── Make project importable ───────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from model.base_model import PowerLayerModel
from model.features import LABEL_CLASSES, engineer_features, encode_labels

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("train")


# ── Data loading ──────────────────────────────────────────────────────────────

def load_datasets(data_dir: Path) -> pd.DataFrame:
    """Load and concatenate all persona CSVs. Returns combined DataFrame."""
    dfs = []
    for csv_path in sorted(data_dir.glob("*.csv")):
        df = pd.read_csv(csv_path)
        # Derive persona name from filename for diagnostics
        df["_persona"] = csv_path.stem
        dfs.append(df)
        logger.info("  Loaded %-25s  %d rows", csv_path.name, len(df))

    if not dfs:
        raise FileNotFoundError(f"No CSV files found in {data_dir}")

    combined = pd.concat(dfs, ignore_index=True)
    logger.info("Total combined: %d rows", len(combined))
    return combined


def add_synthetic_trend(df: pd.DataFrame, seed: int = 42) -> pd.DataFrame:
    """
    Synthesise cpu_trend_last_3_cycles for static CSV rows.

    For active_needed rows:  trend is slightly positive  (0.05 – 0.30)
    For idle_likely rows:    trend is near zero           (-0.10 – 0.10)
    For background_unused:   trend is slightly negative  (-0.25 – -0.05)

    This gives the model signal for the temporal feature without requiring
    the training data to be a live time series.
    """
    rng = np.random.default_rng(seed)
    n   = len(df)
    trend = np.zeros(n, dtype=float)

    for i, label in enumerate(df["label"].values):
        if label == "active_needed":
            trend[i] = rng.uniform(0.05, 0.30)
        elif label == "idle_likely":
            trend[i] = rng.uniform(-0.10, 0.10)
        else:  # background_unused
            trend[i] = rng.uniform(-0.25, -0.05)

    # Add ±5% noise so the tree boundaries aren't perfectly clean
    trend += rng.normal(0, 0.03, n)
    df = df.copy()
    df["cpu_trend_last_3_cycles"] = trend
    return df


# ── Training ──────────────────────────────────────────────────────────────────

def train(data_dir: Path, out_path: Path) -> None:
    print()
    print("═" * 60)
    print("  PowerLayer — Base Model Training")
    print("═" * 60)

    # 1. Load data
    print("\n── Loading datasets ─────────────────────────────────")
    df = load_datasets(data_dir)

    # 2. Validate labels
    invalid = df[~df["label"].isin(LABEL_CLASSES)]
    if not invalid.empty:
        logger.warning("%d rows with invalid labels dropped.", len(invalid))
        df = df[df["label"].isin(LABEL_CLASSES)]

    # 3. Add synthetic trend feature
    df = add_synthetic_trend(df)

    # 4. Class distribution before split
    print("\n── Class distribution ───────────────────────────────")
    for label, count in df["label"].value_counts().items():
        pct = 100 * count / len(df)
        bar = "█" * int(pct / 2)
        print(f"  {label:<22} {count:>5}  {bar} {pct:.1f}%")

    # 5. Per-persona breakdown
    print("\n── Per-persona breakdown ────────────────────────────")
    for persona, grp in df.groupby("_persona"):
        counts = grp["label"].value_counts().to_dict()
        print(f"  {persona:<20}  total={len(grp)}  {counts}")

    # 6. Train / test split (stratified so all labels appear in test set)
    X_raw_train, X_raw_test = train_test_split(
        df, test_size=0.2, random_state=42, stratify=df["label"]
    )
    print(f"\n── Split: {len(X_raw_train)} train / {len(X_raw_test)} test")

    # 7. Train
    print("\n── Training RandomForest (class_weight='balanced') ──")
    model = PowerLayerModel(model_path=out_path)
    stats = model.train(X_raw_train)
    print(f"  Trained on {stats['n_samples']} samples")
    print(f"  Class counts: {stats['class_counts']}")

    # 8. Evaluate on hold-out test set
    print("\n── Test Set Evaluation ──────────────────────────────")
    y_pred, y_conf = model.predict_batch(X_raw_test)
    y_true = X_raw_test["label"].values

    print(classification_report(y_true, y_pred, target_names=LABEL_CLASSES))

    print("── Confusion Matrix ─────────────────────────────────")
    cm = confusion_matrix(y_true, y_pred, labels=LABEL_CLASSES)
    header = "".join(f"{c[:6]:>10}" for c in LABEL_CLASSES)
    print(f"  {'':>22}{header}")
    for row_label, row in zip(LABEL_CLASSES, cm):
        cells = "".join(f"{v:>10}" for v in row)
        print(f"  {row_label:<22}{cells}")

    print(f"\n  Mean confidence: {y_conf.mean():.3f}")

    # 9. Feature importances
    print("\n── Feature Importances ──────────────────────────────")
    importances = model.feature_importances()
    for feat, imp in list(importances.items())[:10]:
        bar = "█" * int(imp * 200)
        print(f"  {feat:<35} {imp:.4f}  {bar}")

    # 10. 5-fold cross-validation on full dataset
    print("\n── 5-Fold Cross-Validation ──────────────────────────")
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import accuracy_score

    X_all = engineer_features(df)
    y_all = encode_labels(df["label"])
    skf   = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    fold_accs = []
    for fold, (tr_idx, va_idx) in enumerate(skf.split(X_all, y_all), 1):
        clf = RandomForestClassifier(
            n_estimators=200, max_depth=12, min_samples_leaf=5,
            class_weight="balanced", random_state=42, n_jobs=-1
        )
        clf.fit(X_all.iloc[tr_idx], y_all[tr_idx])
        preds = clf.predict(X_all.iloc[va_idx])
        acc   = accuracy_score(y_all[va_idx], preds)
        fold_accs.append(acc)
        print(f"  Fold {fold}: accuracy = {acc:.4f}")
    print(f"  Mean CV accuracy: {np.mean(fold_accs):.4f}  ±{np.std(fold_accs):.4f}")

    # 11. Save
    saved = model.save(out_path)
    print(f"\n── Model saved → {saved}")
    print("═" * 60)
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Train PowerLayer base model")
    parser.add_argument(
        "--data-dir", type=Path,
        default=_ROOT / "data" / "personas",
        help="Directory containing persona CSV files",
    )
    parser.add_argument(
        "--out", type=Path,
        default=_ROOT / "model" / "artifacts" / "base_model.joblib",
        help="Output path for trained model artifact",
    )
    args = parser.parse_args()
    train(args.data_dir, args.out)


if __name__ == "__main__":
    main()
