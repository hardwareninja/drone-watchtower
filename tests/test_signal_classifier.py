"""Tests for Task 3.1 signal classifier."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ml.signal_classifier import HAVE_SKLEARN, SignalClassifier

pytestmark = pytest.mark.skipif(not HAVE_SKLEARN, reason="scikit-learn not installed")


def _tone(freq: float, n: int = 4000) -> np.ndarray:
    t = np.arange(n)
    return (np.exp(2j * np.pi * freq * t) + 0.2 * (
        np.random.randn(n) + 1j * np.random.randn(n)
    )).astype(np.complex64)


def test_train_and_predict(tmp_path):
    clf = SignalClassifier()
    data, labels = [], []
    for label, f in [("o4", 0.05), ("wifi", 0.12), ("noise", 0.0)]:
        for _ in range(6):
            data.append(_tone(f) if f else (
                0.3 * (np.random.randn(4000) + 1j * np.random.randn(4000))
            ).astype(np.complex64))
            labels.append(label)
    clf.train(data, labels)
    model = tmp_path / "clf.pkl"
    clf.save_model(model)
    clf2 = SignalClassifier()
    clf2.load_model(model)
    result = clf2.predict(data[0])
    assert result["class"] in clf.classes or result["class"] in labels
    assert 0.0 <= result["confidence"] <= 1.0
