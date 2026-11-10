"""Time-domain channel estimator with smoothing (Task 1.2)."""
from __future__ import annotations

import numpy as np


class ChannelEstimator:
    """Time-domain channel estimator with smoothing."""

    def __init__(self, pilot_pattern: np.ndarray | None, window_size: int = 5):
        if pilot_pattern is None:
            # Default: every 4th subcarrier is a pilot (placeholder pattern)
            pilot_pattern = np.zeros(64, dtype=bool)
            pilot_pattern[::4] = True
        self.pilot_pattern = np.asarray(pilot_pattern, dtype=bool)
        self.window_size = max(1, int(window_size))

    def estimate_channel(
        self, ofdm_symbols: np.ndarray, pilot_symbols: np.ndarray
    ) -> np.ndarray:
        """Estimate channel response from pilot symbols (LS at pilots + interp)."""
        ofdm = np.asarray(ofdm_symbols, dtype=np.complex128).ravel()
        pilots = np.asarray(pilot_symbols, dtype=np.complex128).ravel()
        n = len(ofdm)
        pattern = self.pilot_pattern
        if len(pattern) != n:
            # Resize pattern to match symbol length
            pattern = np.zeros(n, dtype=bool)
            step = max(1, n // max(1, int(self.pilot_pattern.sum()) or 1))
            pattern[::step] = True
        pilot_positions = np.where(pattern)[0]
        if pilot_positions.size == 0:
            return np.ones(n, dtype=np.complex128)
        p = pilots[: pilot_positions.size]
        if p.size != pilot_positions.size:
            p = np.resize(p, pilot_positions.size)
        # Avoid divide-by-zero on zero pilots
        p = np.where(np.abs(p) < 1e-12, 1.0 + 0j, p)
        channel_at_pilots = ofdm[pilot_positions] / p
        channel_estimate = np.interp(
            np.arange(n),
            pilot_positions.astype(float),
            np.real(channel_at_pilots),
        ) + 1j * np.interp(
            np.arange(n),
            pilot_positions.astype(float),
            np.imag(channel_at_pilots),
        )
        return channel_estimate.astype(np.complex128)

    def smooth_channel(self, channel_estimate: np.ndarray) -> np.ndarray:
        """Apply circular moving-average smoothing (numpy-only, no scipy required)."""
        ch = np.asarray(channel_estimate, dtype=np.complex128).ravel()
        w = self.window_size
        if w <= 1 or ch.size == 0:
            return ch
        kernel = np.ones(w, dtype=np.float64) / w
        real = np.convolve(np.real(ch), kernel, mode="same")
        imag = np.convolve(np.imag(ch), kernel, mode="same")
        # Approximate wrap at edges by blending with rolled averages
        if ch.size >= w:
            wrap = np.convolve(np.r_[ch[-w:], ch, ch[:w]], kernel, mode="same")
            mid = wrap[w:w + ch.size]
            real = np.real(mid)
            imag = np.imag(mid)
        return real + 1j * imag

    def equalize(
        self, ofdm_symbols: np.ndarray, channel_estimate: np.ndarray
    ) -> np.ndarray:
        """Equalize OFDM symbols using a smoothed channel estimate."""
        ofdm = np.asarray(ofdm_symbols, dtype=np.complex128).ravel()
        smoothed = self.smooth_channel(channel_estimate)
        denom = np.where(np.abs(smoothed) < 1e-12, 1.0 + 0j, smoothed)
        return ofdm / denom
