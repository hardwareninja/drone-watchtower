"""ML-based signal classifier for the 2.4 GHz band (Task 3.1)."""
from __future__ import annotations

from pathlib import Path

import numpy as np

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import StandardScaler
    import joblib
    HAVE_SKLEARN = True
except ImportError:  # pragma: no cover
    HAVE_SKLEARN = False
    RandomForestClassifier = None  # type: ignore
    StandardScaler = None  # type: ignore
    joblib = None  # type: ignore


class SignalClassifier:
    """ML-based signal classifier for 2.4GHz band."""

    def __init__(self):
        if not HAVE_SKLEARN:
            raise ImportError(
                "scikit-learn and joblib are required for SignalClassifier "
                "(pip install scikit-learn joblib)"
            )
        self.classifier = RandomForestClassifier(n_estimators=100, random_state=42)
        self.scaler = StandardScaler()
        self.classes = ["o4", "wifi", "bluetooth", "elrs", "noise"]
        self.trained = False

    def extract_features(self, iq_data: np.ndarray) -> np.ndarray:
        """Extract features from IQ data."""
        iq = np.asarray(iq_data, dtype=np.complex128).ravel()
        if iq.size == 0:
            return np.zeros(50 + 30 + 10 + 10 + 4)
        features: list[float] = []

        spectrum = np.abs(np.fft.fft(iq))
        features.extend(spectrum[:50].tolist())
        if len(features) < 50:
            features.extend([0.0] * (50 - len(features)))

        n = min(1000, iq.size)
        autocorr = np.correlate(iq[:n], iq[:n], mode="full")
        mid = len(autocorr) // 2
        ac = np.abs(autocorr[mid:mid + 30])
        features.extend(ac.tolist())
        if len(ac) < 30:
            features.extend([0.0] * (30 - len(ac)))

        features.extend(self._compute_cyclic_autocorr(iq, 15000).tolist())

        energy_dist, _ = np.histogram(spectrum, bins=10)
        features.extend(energy_dist.astype(float).tolist())

        mag = np.abs(iq)
        features.extend([
            float(mag.mean()),
            float(mag.std()),
            float(mag.max()),
            float(mag.mean() / (mag.std() + 1e-10)),
        ])
        return np.asarray(features, dtype=np.float64)

    def _compute_cyclic_autocorr(
        self, iq_data: np.ndarray, cyclic_freq: float, sample_rate: float = 20e6
    ) -> np.ndarray:
        n = len(iq_data)
        alpha = cyclic_freq / sample_rate
        cyclic_autocorr = np.zeros(10)
        for lag in range(10):
            if n <= lag:
                continue
            idx = np.arange(n - lag)
            corr = np.sum(
                iq_data[: n - lag] * np.conj(iq_data[lag:])
                * np.exp(-2j * np.pi * alpha * idx)
            )
            cyclic_autocorr[lag] = float(np.abs(corr) / n)
        return cyclic_autocorr

    def train(self, training_data: list[np.ndarray], labels: list[str]) -> None:
        features = np.asarray([self.extract_features(x) for x in training_data])
        features_scaled = self.scaler.fit_transform(features)
        self.classifier.fit(features_scaled, labels)
        self.trained = True

    def predict(self, iq_data: np.ndarray) -> dict:
        if not self.trained:
            raise ValueError("Classifier not trained. Call train() or load_model() first.")
        features = self.extract_features(iq_data)
        features_scaled = self.scaler.transform(features.reshape(1, -1))
        prediction = self.classifier.predict(features_scaled)[0]
        probabilities = self.classifier.predict_proba(features_scaled)[0]
        class_names = list(self.classifier.classes_)
        return {
            "class": prediction,
            "confidence": float(probabilities.max()),
            "probabilities": dict(zip(class_names, map(float, probabilities))),
        }

    def save_model(self, filepath: str | Path) -> None:
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({
            "classifier": self.classifier,
            "scaler": self.scaler,
            "classes": self.classes,
        }, path)

    def load_model(self, filepath: str | Path) -> None:
        model_data = joblib.load(filepath)
        self.classifier = model_data["classifier"]
        self.scaler = model_data["scaler"]
        self.classes = model_data.get("classes", self.classes)
        self.trained = True
