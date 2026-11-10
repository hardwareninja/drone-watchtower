"""Tests for Task 1.1 ZC detector."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_processing.zc_detector import ZCDetector


def test_zc_detection_high_snr():
    detector = ZCDetector()
    zc_seq = detector.generate_zc_sequence(600)
    noise = (np.random.randn(10000) + 1j * np.random.randn(10000)) * 0.1
    test_signal = noise.copy()
    test_signal[5000:5000 + len(zc_seq)] += zc_seq * 10
    detections = detector.detect(test_signal, threshold=0.05)
    assert len(detections) > 0
    roots = {d["root"] for d in detections}
    assert 600 in roots


def test_zc_detection_low_snr():
    detector = ZCDetector()
    zc_seq = detector.generate_zc_sequence(600)
    noise = (np.random.randn(10000) + 1j * np.random.randn(10000)) * 0.5
    test_signal = noise.copy()
    test_signal[5000:5000 + len(zc_seq)] += zc_seq * 2.0
    detections = detector.detect(test_signal, threshold=0.02)
    assert len(detections) > 0
    assert all("confidence" in d for d in detections)


def test_generate_length():
    detector = ZCDetector()
    seq = detector.generate_zc_sequence(147, length=601)
    assert seq.shape == (601,)
    assert np.isclose(np.abs(seq), 1.0).all()
