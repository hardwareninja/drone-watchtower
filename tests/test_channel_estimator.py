"""Tests for Task 1.2 channel estimator."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_processing.channel_estimator import ChannelEstimator


def test_estimate_and_equalize():
    n = 64
    pattern = np.zeros(n, dtype=bool)
    pattern[::4] = True
    est = ChannelEstimator(pattern, window_size=3)
    true_channel = np.exp(1j * 0.1 * np.arange(n))
    pilots = np.ones(pattern.sum(), dtype=np.complex128)
    # Observed = channel * pilots at pilot tones, random elsewhere
    ofdm = (np.random.randn(n) + 1j * np.random.randn(n)) * 0.01
    ofdm[pattern] = true_channel[pattern] * pilots
    channel_est = est.estimate_channel(ofdm, pilots)
    assert channel_est.shape == (n,)
    equalized = est.equalize(ofdm, channel_est)
    assert equalized.shape == (n,)
    # At pilot positions, equalized ≈ 1
    err = np.abs(equalized[pattern] - 1.0).mean()
    assert err < 0.2


def test_smooth_reduces_noise():
    est = ChannelEstimator(None, window_size=5)
    noisy = np.ones(100, dtype=np.complex128) + 0.5 * (
        np.random.randn(100) + 1j * np.random.randn(100)
    )
    smooth = est.smooth_channel(noisy)
    assert np.std(np.abs(smooth)) < np.std(np.abs(noisy))
