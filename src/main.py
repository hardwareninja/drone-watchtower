"""Enhanced pipeline glue (integration guide from enhancement tasks).

Wraps the new signal-processing / fusion / ML modules. The live sensor box
still runs via webapp/backend/app.py + the luyii HackRF scanner; this class
is the research/offline entry point described in the enhancement doc.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from signal_processing.zc_detector import ZCDetector
from signal_processing.channel_estimator import ChannelEstimator
from fusion.remote_id_fusion import RemoteIDFusion
from ml.signal_classifier import SignalClassifier, HAVE_SKLEARN


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = ROOT / "models" / "signal_classifier.pkl"


class EnhancedDroneWatchtower:
    def __init__(self, model_path: Path | None = DEFAULT_MODEL):
        self.zc_detector = ZCDetector()
        self.channel_estimator = ChannelEstimator(pilot_pattern=None, window_size=5)
        self.fusion = RemoteIDFusion(time_window=2.0)
        self.classifier: SignalClassifier | None = None
        if HAVE_SKLEARN:
            self.classifier = SignalClassifier()
            path = model_path or DEFAULT_MODEL
            if path.exists():
                self.classifier.load_model(path)

    def process_iq(self, iq_data: np.ndarray, sample_rate: float = 20e6) -> dict:
        """Enhanced IQ processing pipeline."""
        iq = np.asarray(iq_data, dtype=np.complex128).ravel()

        classification = {"class": "o4", "confidence": 0.0, "probabilities": {}}
        if self.classifier is not None and self.classifier.trained:
            classification = self.classifier.predict(iq)
            if classification["class"] != "o4":
                return {"signal_type": classification["class"], "o4_data": None,
                        "classification_confidence": classification["confidence"]}

        zc_detections = self.zc_detector.detect(iq)
        droneid_data = None
        if zc_detections:
            ofdm_symbols = self._extract_ofdm_symbols(iq, zc_detections[0])
            pilot_symbols = self._get_pilot_symbols(len(ofdm_symbols))
            channel_est = self.channel_estimator.estimate_channel(
                ofdm_symbols, pilot_symbols
            )
            equalized = self.channel_estimator.equalize(ofdm_symbols, channel_est)
            droneid_data = self._decode_droneid(equalized)

        return {
            "signal_type": "o4",
            "classification_confidence": classification.get("confidence", 0.0),
            "zc_detections": zc_detections,
            "droneid_data": droneid_data,
        }

    def _extract_ofdm_symbols(self, iq_data: np.ndarray, zc_detection: dict) -> np.ndarray:
        # Placeholder until hooked into the luyii OFDM extractor; take a window
        # after the ZC peak for channel-estimation demos.
        start = max(0, int(zc_detection.get("delay", 0)))
        return iq_data[start:start + 1024] if iq_data.size > start else iq_data

    def _get_pilot_symbols(self, n: int) -> np.ndarray:
        return np.ones(max(1, n // 4), dtype=np.complex128)

    def _decode_droneid(self, equalized_symbols: np.ndarray) -> dict:
        # Placeholder — live decode stays in third_party/.../o2_droneid_decode.py
        import time
        return {
            "serial_number": "UNKNOWN",
            "timestamp": time.time(),
            "frequency": 2442e6,
            "equalized_energy": float(np.mean(np.abs(equalized_symbols))),
        }
