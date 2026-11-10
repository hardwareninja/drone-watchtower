"""Enhanced ZC sequence detector with pulse compression (Task 1.1)."""
from __future__ import annotations

import numpy as np


class ZCDetector:
    """Enhanced ZC sequence detector with pulse compression."""

    def __init__(self, sample_rate: float = 20e6):
        self.sample_rate = sample_rate
        self.zc_roots = [600, 147]  # O4 uses both roots

    def generate_zc_sequence(self, root: int, length: int = 601) -> np.ndarray:
        """Generate ZC sequence for given root."""
        n = np.arange(length)
        phase = -np.pi * root * n * (n + 1) / length
        return np.exp(1j * phase)

    def pulse_compression(self, iq_data: np.ndarray, zc_ref: np.ndarray) -> np.ndarray:
        """Apply pulse compression (matched filtering)."""
        return np.correlate(iq_data, np.conj(zc_ref), mode="same")

    def multi_scale_correlation(
        self, iq_data: np.ndarray, zc_ref: np.ndarray, max_delay: int = 10
    ) -> tuple[int, float]:
        """Multi-scale delay correlation for multipath."""
        delays = np.arange(-max_delay, max_delay + 1)
        correlation_map = np.zeros(len(delays))
        for i, delay in enumerate(delays):
            shifted_ref = np.roll(zc_ref, int(delay))
            corr = np.abs(np.correlate(iq_data, np.conj(shifted_ref), mode="valid"))
            correlation_map[i] = float(corr.max()) if corr.size else 0.0
        best_i = int(np.argmax(correlation_map))
        return int(delays[best_i]), float(correlation_map[best_i])

    def detect(self, iq_data: np.ndarray, threshold: float = 0.7) -> list[dict]:
        """Detect ZC sequences in IQ data."""
        iq = np.asarray(iq_data, dtype=np.complex128).ravel()
        detections: list[dict] = []
        for root in self.zc_roots:
            zc_ref = self.generate_zc_sequence(root)
            compressed = self.pulse_compression(iq, zc_ref)
            delay, correlation = self.multi_scale_correlation(iq, zc_ref)
            max_possible = float(np.abs(zc_ref).sum())
            normalized_corr = correlation / max_possible if max_possible else 0.0
            # Also consider peak of the compressed output (same-scale match)
            peak = float(np.max(np.abs(compressed))) / max_possible if max_possible else 0.0
            score = max(normalized_corr, peak)
            if score > threshold:
                detections.append({
                    "root": root,
                    "delay": delay,
                    "correlation": score,
                    "confidence": min(1.0, score / threshold),
                })
        return detections
