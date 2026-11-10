#!/usr/bin/env python3
"""Train the RF signal classifier (Task 3.1)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ml.signal_classifier import SignalClassifier, HAVE_SKLEARN
from prepare_training_data import prepare_training_data


def synthesize_demo_set(n_per_class: int = 8) -> tuple[list[np.ndarray], list[str]]:
    """Synthetic IQ when no labeled captures exist yet — lets CI/smoke train."""
    rng = np.random.default_rng(0)
    data, labels = [], []
    for label, tone in [("o4", 0.05), ("wifi", 0.12), ("bluetooth", 0.2),
                        ("elrs", 0.3), ("noise", 0.0)]:
        for _ in range(n_per_class):
            t = np.arange(10000)
            carrier = np.exp(2j * np.pi * tone * t) if tone else 0
            noise = (rng.standard_normal(10000) + 1j * rng.standard_normal(10000)) * 0.3
            data.append((carrier + noise).astype(np.complex64))
            labels.append(label)
    return data, labels


def main() -> None:
    if not HAVE_SKLEARN:
        raise SystemExit("scikit-learn/joblib required")
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/raw")
    ap.add_argument("--model", default="models/signal_classifier.pkl")
    ap.add_argument("--demo", action="store_true",
                    help="Train on synthetic IQ if no raw captures present")
    args = ap.parse_args()

    data, labels = prepare_training_data(args.data_dir)
    if not data and args.demo:
        print("No raw captures; using synthetic demo set")
        data, labels = synthesize_demo_set()
    if not data:
        raise SystemExit(f"No training data under {args.data_dir} (pass --demo to synthesize)")

    clf = SignalClassifier()
    clf.train(data, labels)
    out = Path(args.model)
    clf.save_model(out)
    print(f"Trained on {len(data)} samples → {out}")

    # Smoke predict
    pred = clf.predict(data[0])
    print(f"Smoke predict: {pred['class']} ({pred['confidence']:.2f})")


if __name__ == "__main__":
    main()
