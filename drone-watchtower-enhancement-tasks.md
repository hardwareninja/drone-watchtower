# Drone Watchtower Enhancement Tasks

# Drone Watchtower Enhancement Tasks

## 🎯 Project Overview

This document contains a prioritized list of enhancements for the drone-watchtower project. Each task includes context, requirements, and implementation guidance.

---

## 📋 Task Priority

### Priority 1: Signal Processing Enhancements

#### Task 1.1: ZC Sequence Detection with Pulse Compression

**Context:**
O4 DroneID signals use Zadoff-Chu (ZC) sequences for synchronization. Current detection may miss signals in low SNR environments.

**Requirements:**
- Implement pulse compression detection for ZC sequences
- Support both ZC root 600 and root 147
- Improve detection sensitivity by 6-10 dB
- Add multi-scale delay correlation for multipath environments

**Implementation:**

```python
# File: src/signal_processing/zc_detector.py

import numpy as np
from scipy import signal

class ZCDetector:
    """Enhanced ZC sequence detector with pulse compression"""
    
    def __init__(self, sample_rate=20e6):
        self.sample_rate = sample_rate
        self.zc_roots = [600, 147]  # O4 uses both roots
        
    def generate_zc_sequence(self, root, length=601):
        """Generate ZC sequence for given root"""
        n = np.arange(length)
        # ZC sequence formula
        phase = -np.pi * root * n * (n + 1) / length
        return np.exp(1j * phase)
    
    def pulse_compression(self, iq_data, zc_ref):
        """Apply pulse compression (matched filtering)"""
        # Matched filter = correlation with conjugate of reference
        compressed = np.correlate(iq_data, np.conj(zc_ref), mode='same')
        return compressed
    
    def multi_scale_correlation(self, iq_data, zc_ref, max_delay=10):
        """Multi-scale delay correlation for multipath"""
        delays = np.arange(-max_delay, max_delay + 1)
        correlation_map = np.zeros(len(delays))
        
        for i, delay in enumerate(delays):
            shifted_ref = np.roll(zc_ref, delay)
            corr = np.abs(np.correlate(iq_data, np.conj(shifted_ref), mode='valid'))
            correlation_map[i] = corr.max()
        
        best_delay = delays[np.argmax(correlation_map)]
        best_correlation = correlation_map.max()
        
        return best_delay, best_correlation
    
    def detect(self, iq_data, threshold=0.7):
        """Detect ZC sequences in IQ data"""
        detections = []
        
        for root in self.zc_roots:
            zc_ref = self.generate_zc_sequence(root)
            
            # Pulse compression
            compressed = self.pulse_compression(iq_data, zc_ref)
            
            # Multi-scale correlation
            delay, correlation = self.multi_scale_correlation(iq_data, zc_ref)
            
            # Normalize correlation
            max_possible = np.abs(zc_ref).sum()
            normalized_corr = correlation / max_possible
            
            if normalized_corr > threshold:
                detections.append({
                    'root': root,
                    'delay': delay,
                    'correlation': normalized_corr,
                    'confidence': min(1.0, normalized_corr / threshold)
                })
        
        return detections

# Usage example:
# detector = ZCDetector(sample_rate=20e6)
# detections = detector.detect(iq_data, threshold=0.7)
# print(f"Detected {len(detections)} ZC sequences")
```

**Testing:**
```python
# File: tests/test_zc_detector.py

import pytest
from src.signal_processing.zc_detector import ZCDetector

def test_zc_detection_high_snr():
    """Test ZC detection in high SNR environment"""
    detector = ZCDetector()
    
    # Generate test signal with embedded ZC sequence
    zc_seq = detector.generate_zc_sequence(600)
    noise = np.random.randn(10000) + 1j * np.random.randn(10000)
    test_signal = noise.copy()
    test_signal[5000:5000+len(zc_seq)] += zc_seq * 10  # High SNR
    
    detections = detector.detect(test_signal, threshold=0.5)
    assert len(detections) > 0
    assert detections[0]['root'] == 600

def test_zc_detection_low_snr():
    """Test ZC detection in low SNR environment"""
    detector = ZCDetector()
    
    zc_seq = detector.generate_zc_sequence(600)
    noise = np.random.randn(10000) + 1j * np.random.randn(10000)
    test_signal = noise.copy()
    test_signal[5000:5000+len(zc_seq)] += zc_seq * 0.5  # Low SNR
    
    detections = detector.detect(test_signal, threshold=0.3)
    # Should still detect with pulse compression
    assert len(detections) > 0
```

**Acceptance Criteria:**
- [x] Detects ZC sequences at SNR > -5 dB
- [x] Supports both root 600 and 147
- [x] Returns detection confidence score
- [x] Handles multipath with delay estimation
- [x] Unit tests pass

---

#### Task 1.2: Time-Domain Channel Smoothing

**Context:**
O4 signals suffer from multipath fading in indoor/urban environments. Channel smoothing improves demodulation performance.

**Requirements:**
- Implement time-domain channel estimation
- Apply smoothing filter to reduce noise
- Integrate with existing OFDM demodulator
- Support configurable window size

**Implementation:**

```python
# File: src/signal_processing/channel_estimator.py

import numpy as np
from scipy.ndimage import uniform_filter1d

class ChannelEstimator:
    """Time-domain channel estimator with smoothing"""
    
    def __init__(self, pilot_pattern, window_size=5):
        self.pilot_pattern = pilot_pattern
        self.window_size = window_size
        
    def estimate_channel(self, ofdm_symbols, pilot_symbols):
        """Estimate channel response from pilot symbols"""
        # LS (Least Squares) estimation at pilot positions
        pilot_positions = np.where(self.pilot_pattern)[0]
        channel_at_pilots = ofdm_symbols[pilot_positions] / pilot_symbols
        
        # Interpolate to all subcarriers
        channel_estimate = np.interp(
            np.arange(len(ofdm_symbols)),
            pilot_positions,
            channel_at_pilots
        )
        
        return channel_estimate
    
    def smooth_channel(self, channel_estimate):
        """Apply time-domain smoothing"""
        # Moving average filter
        smoothed = uniform_filter1d(
            channel_estimate, 
            size=self.window_size,
            mode='wrap'
        )
        return smoothed
    
    def equalize(self, ofdm_symbols, channel_estimate):
        """Equalize OFDM symbols using channel estimate"""
        smoothed_channel = self.smooth_channel(channel_estimate)
        equalized = ofdm_symbols / smoothed_channel
        return equalized

# Integration with existing demodulator:
# estimator = ChannelEstimator(pilot_pattern, window_size=5)
# channel_est = estimator.estimate_channel(ofdm_symbols, pilot_symbols)
# equalized = estimator.equalize(ofdm_symbols, channel_est)
```

**Acceptance Criteria:**
- [ ] Reduces BER by > 30% in multipath conditions *(needs live multipath IQ bench)*
- [x] Configurable window size
- [x] Works with existing OFDM demodulator *(via `EnhancedDroneWatchtower` glue)*
- [x] Unit tests with simulated multipath channel

---

### Priority 2: Remote ID Fusion

#### Task 2.1: Remote ID + DroneID Time Correlation

**Context:**
Remote ID provides plaintext position data while DroneID provides serial numbers. Fusing both gives complete tracking.

**Requirements:**
- Implement time alignment algorithm
- Correlate Remote ID and DroneID detections
- Handle clock drift between sources
- Output fused tracks with confidence scores

**Implementation:**

```python
# File: src/fusion/remote_id_fusion.py

import numpy as np
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Dict, Optional

@dataclass
class RemoteIDDetection:
    timestamp: float
    serial_number: Optional[str]
    latitude: float
    longitude: float
    altitude: Optional[float]
    source: str  # 'wifi' or 'bluetooth'

@dataclass
class DroneIDDetection:
    timestamp: float
    serial_number: Optional[str]
    frequency: float
    rssi: float
    raw_data: bytes

@dataclass
class FusedTrack:
    timestamp: float
    serial_number: Optional[str]
    latitude: float
    longitude: float
    altitude: Optional[float]
    confidence: float
    sources: List[str]

class RemoteIDFusion:
    """Fuse Remote ID and DroneID detections"""
    
    def __init__(self, time_window=2.0, max_drift=0.5):
        self.time_window = time_window  # seconds
        self.max_drift = max_drift  # seconds
        self.remote_id_buffer: List[RemoteIDDetection] = []
        self.droneid_buffer: List[DroneIDDetection] = []
        
    def add_remote_id(self, detection: RemoteIDDetection):
        """Add Remote ID detection to buffer"""
        self.remote_id_buffer.append(detection)
        # Keep only recent detections
        cutoff = detection.timestamp - 60  # 1 minute buffer
        self.remote_id_buffer = [
            d for d in self.remote_id_buffer if d.timestamp > cutoff
        ]
    
    def add_droneid(self, detection: DroneIDDetection):
        """Add DroneID detection to buffer"""
        self.droneid_buffer.append(detection)
        cutoff = detection.timestamp - 60
        self.droneid_buffer = [
            d for d in self.droneid_buffer if d.timestamp > cutoff
        ]
    
    def correlate(self) -> List[FusedTrack]:
        """Correlate Remote ID and DroneID detections"""
        fused_tracks = []
        used_remote_ids = set()
        used_droneids = set()
        
        for rid_idx, rid in enumerate(self.remote_id_buffer):
            if rid_idx in used_remote_ids:
                continue
            
            best_match = None
            best_score = 0
            
            for did_idx, did in enumerate(self.droneid_buffer):
                if did_idx in used_droneids:
                    continue
                
                # Time correlation
                time_diff = abs(rid.timestamp - did.timestamp)
                if time_diff > self.time_window:
                    continue
                
                # Serial number correlation (if available)
                serial_match = False
                if rid.serial_number and did.serial_number:
                    serial_match = (rid.serial_number == did.serial_number)
                
                # Calculate correlation score
                time_score = 1.0 - (time_diff / self.time_window)
                serial_score = 1.0 if serial_match else 0.5
                
                # Weighted combination
                total_score = 0.6 * time_score + 0.4 * serial_score
                
                if total_score > best_score:
                    best_score = total_score
                    best_match = (rid_idx, did_idx, did)
            
            if best_match and best_score > 0.5:
                rid_idx, did_idx, did = best_match
                used_remote_ids.add(rid_idx)
                used_droneids.add(did_idx)
                
                # Create fused track
                fused_track = FusedTrack(
                    timestamp=(rid.timestamp + did.timestamp) / 2,
                    serial_number=rid.serial_number or did.serial_number,
                    latitude=rid.latitude,
                    longitude=rid.longitude,
                    altitude=rid.altitude,
                    confidence=best_score,
                    sources=['remote_id', 'droneid']
                )
                fused_tracks.append(fused_track)
        
        return fused_tracks
    
    def get_unmatched_remote_ids(self) -> List[RemoteIDDetection]:
        """Get Remote ID detections without DroneID match"""
        # Return Remote IDs that couldn't be correlated
        matched_indices = set()
        fused = self.correlate()
        
        # This is simplified - in practice, track which ones were matched
        return self.remote_id_buffer

# Usage:
# fusion = RemoteIDFusion(time_window=2.0)
# fusion.add_remote_id(rid_detection)
# fusion.add_droneid(did_detection)
# fused_tracks = fusion.correlate()
```

**Testing:**
```python
# File: tests/test_remote_id_fusion.py

def test_time_correlation():
    """Test time-based correlation"""
    fusion = RemoteIDFusion(time_window=2.0)
    
    # Add Remote ID detection
    rid = RemoteIDDetection(
        timestamp=100.0,
        serial_number="ABC123",
        latitude=22.3193,
        longitude=114.1694,
        altitude=100.0,
        source='wifi'
    )
    fusion.add_remote_id(rid)
    
    # Add DroneID detection within time window
    did = DroneIDDetection(
        timestamp=100.5,
        serial_number="ABC123",
        frequency=2442e6,
        rssi=-65,
        raw_data=b'\x00\x01\x02'
    )
    fusion.add_droneid(did)
    
    # Correlate
    tracks = fusion.correlate()
    
    assert len(tracks) == 1
    assert tracks[0].serial_number == "ABC123"
    assert tracks[0].confidence > 0.8
    assert 'remote_id' in tracks[0].sources
    assert 'droneid' in tracks[0].sources

def test_serial_number_mismatch():
    """Test correlation with mismatched serial numbers"""
    fusion = RemoteIDFusion(time_window=2.0)
    
    rid = RemoteIDDetection(
        timestamp=100.0,
        serial_number="ABC123",
        latitude=22.3193,
        longitude=114.1694,
        altitude=100.0,
        source='wifi'
    )
    fusion.add_remote_id(rid)
    
    did = DroneIDDetection(
        timestamp=100.5,
        serial_number="XYZ789",  # Different serial
        frequency=2442e6,
        rssi=-65,
        raw_data=b'\x00\x01\x02'
    )
    fusion.add_droneid(did)
    
    tracks = fusion.correlate()
    
    # Should still correlate based on time, but with lower confidence
    assert len(tracks) == 1
    assert tracks[0].confidence < 0.8
```

**Acceptance Criteria:**
- [x] Correlates detections within 2-second window
- [x] Handles serial number matching
- [x] Returns confidence scores
- [x] Buffers detections for 1 minute
- [x] Unit tests pass
- [x] Wired into live dashboard (`aircraftList` merge by serial) + firmware BLE MAC cache

---

### Priority 3: Machine Learning Integration

#### Task 3.1: Signal Classifier

**Context:**
Distinguish O4 signals from other 2.4GHz signals (WiFi, Bluetooth, ELRS) to reduce false positives.

**Requirements:**
- Train RF classifier on labeled IQ data
- Extract features: spectrum shape, autocorrelation, cyclic features
- Support real-time classification
- Export model for deployment

**Implementation:**

```python
# File: src/ml/signal_classifier.py

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
import joblib

class SignalClassifier:
    """ML-based signal classifier for 2.4GHz band"""
    
    def __init__(self):
        self.classifier = RandomForestClassifier(n_estimators=100, random_state=42)
        self.scaler = StandardScaler()
        self.classes = ['o4', 'wifi', 'bluetooth', 'elrs', 'noise']
        self.trained = False
    
    def extract_features(self, iq_data):
        """Extract features from IQ data"""
        features = []
        
        # 1. Spectrum features
        spectrum = np.abs(np.fft.fft(iq_data))
        spectrum_features = spectrum[:50]  # First 50 frequency bins
        features.extend(spectrum_features)
        
        # 2. Autocorrelation features
        autocorr = np.correlate(iq_data[:1000], iq_data[:1000], mode='full')
        autocorr_features = np.abs(autocorr[len(autocorr)//2:len(autocorr)//2+30])
        features.extend(autocorr_features)
        
        # 3. Cyclic features (O4 has 15kHz subcarrier spacing)
        cyclic_freq = 15000
        cyclic_features = self._compute_cyclic_autocorr(iq_data, cyclic_freq)
        features.extend(cyclic_features)
        
        # 4. Energy distribution
        energy_dist, _ = np.histogram(spectrum, bins=10)
        features.extend(energy_dist)
        
        # 5. Statistical features
        features.extend([
            np.mean(np.abs(iq_data)),
            np.std(np.abs(iq_data)),
            np.max(np.abs(iq_data)),
            np.abs(iq_data).mean() / (np.abs(iq_data).std() + 1e-10)  # SNR estimate
        ])
        
        return np.array(features)
    
    def _compute_cyclic_autocorr(self, iq_data, cyclic_freq, sample_rate=20e6):
        """Compute cyclic autocorrelation at given frequency"""
        # Simplified implementation
        n = len(iq_data)
        alpha = cyclic_freq / sample_rate
        
        # Compute cyclic autocorrelation
        cyclic_autocorr = np.zeros(10)
        for lag in range(10):
            corr = np.sum(iq_data[:n-lag] * np.conj(iq_data[lag:]) * 
                         np.exp(-2j * np.pi * alpha * np.arange(n-lag)))
            cyclic_autocorr[lag] = np.abs(corr) / n
        
        return cyclic_autocorr
    
    def train(self, training_data, labels):
        """Train classifier on labeled data"""
        features = []
        for iq_data in training_data:
            feat = self.extract_features(iq_data)
            features.append(feat)
        
        features = np.array(features)
        
        # Scale features
        features_scaled = self.scaler.fit_transform(features)
        
        # Train classifier
        self.classifier.fit(features_scaled, labels)
        self.trained = True
        
        # Save model
        self.save_model('signal_classifier.pkl')
    
    def predict(self, iq_data):
        """Classify signal type"""
        if not self.trained:
            raise ValueError("Classifier not trained. Call train() first.")
        
        features = self.extract_features(iq_data)
        features_scaled = self.scaler.transform(features.reshape(1, -1))
        
        prediction = self.classifier.predict(features_scaled)[0]
        probabilities = self.classifier.predict_proba(features_scaled)[0]
        
        return {
            'class': prediction,
            'confidence': probabilities.max(),
            'probabilities': dict(zip(self.classes, probabilities))
        }
    
    def save_model(self, filepath):
        """Save trained model"""
        model_data = {
            'classifier': self.classifier,
            'scaler': self.scaler,
            'classes': self.classes
        }
        joblib.dump(model_data, filepath)
    
    def load_model(self, filepath):
        """Load trained model"""
        model_data = joblib.load(filepath)
        self.classifier = model_data['classifier']
        self.scaler = model_data['scaler']
        self.classes = model_data['classes']
        self.trained = True

# Usage:
# classifier = SignalClassifier()
# classifier.train(training_data, labels)
# result = classifier.predict(iq_data)
# print(f"Signal type: {result['class']}, confidence: {result['confidence']}")
```

**Training Data Preparation:**
```python
# File: scripts/prepare_training_data.py

import numpy as np
import os
from pathlib import Path

def prepare_training_data(data_dir='data/raw'):
    """Prepare training data from raw IQ files"""
    training_data = []
    labels = []
    
    # Expected directory structure:
    # data/raw/
    #   o4/
    #     sample1.iq
    #     sample2.iq
    #   wifi/
    #     sample1.iq
    #   ...
    
    for signal_type in ['o4', 'wifi', 'bluetooth', 'elrs', 'noise']:
        signal_dir = Path(data_dir) / signal_type
        
        if not signal_dir.exists():
            print(f"Warning: {signal_dir} not found, skipping")
            continue
        
        for iq_file in signal_dir.glob('*.iq'):
            # Load IQ data (assuming complex64 format)
            iq_data = np.fromfile(iq_file, dtype=np.complex64)
            
            # Resample to standard length (e.g., 10000 samples)
            if len(iq_data) > 10000:
                iq_data = iq_data[:10000]
            elif len(iq_data) < 10000:
                # Pad with zeros
                iq_data = np.pad(iq_data, (0, 10000 - len(iq_data)))
            
            training_data.append(iq_data)
            labels.append(signal_type)
    
    return training_data, labels

# Usage:
# training_data, labels = prepare_training_data()
# classifier = SignalClassifier()
# classifier.train(training_data, labels)
```

**Acceptance Criteria:**
- [ ] Classifier achieves > 90% accuracy on test set *(needs labeled IQ under data/raw/)*
- [x] Supports 5 signal types
- [x] Real-time inference < 10ms *(synthetic smoke)*
- [x] Model can be saved and loaded
- [x] Training script included (`scripts/train_classifier.py --demo`)

---

## 🚀 Integration Guide

### Step 1: Update Project Structure

```bash
# Add new directories
mkdir -p src/signal_processing
mkdir -p src/fusion
mkdir -p src/ml
mkdir -p tests
mkdir -p scripts
mkdir -p data/raw/{o4,wifi,bluetooth,elrs,noise}
```

### Step 2: Update Main Pipeline

```python
# File: src/main.py (update existing)

from signal_processing.zc_detector import ZCDetector
from signal_processing.channel_estimator import ChannelEstimator
from fusion.remote_id_fusion import RemoteIDFusion
from ml.signal_classifier import SignalClassifier

class EnhancedDroneWatchtower:
    def __init__(self):
        self.zc_detector = ZCDetector()
        self.channel_estimator = ChannelEstimator(pilot_pattern=None)
        self.fusion = RemoteIDFusion()
        self.classifier = SignalClassifier()
        self.classifier.load_model('models/signal_classifier.pkl')
    
    def process_iq(self, iq_data, sample_rate=20e6):
        """Enhanced IQ processing pipeline"""
        
        # 1. Classify signal
        classification = self.classifier.predict(iq_data)
        
        if classification['class'] != 'o4':
            return {'signal_type': classification['class'], 'o4_data': None}
        
        # 2. Detect ZC sequences
        zc_detections = self.zc_detector.detect(iq_data)
        
        # 3. Channel estimation and equalization
        if zc_detections:
            # Extract OFDM symbols (simplified)
            ofdm_symbols = self._extract_ofdm_symbols(iq_data, zc_detections[0])
            
            # Estimate channel
            pilot_symbols = self._get_pilot_symbols()
            channel_est = self.channel_estimator.estimate_channel(
                ofdm_symbols, pilot_symbols
            )
            
            # Equalize
            equalized = self.channel_estimator.equalize(ofdm_symbols, channel_est)
            
            # Decode DroneID
            droneid_data = self._decode_droneid(equalized)
            
            return {
                'signal_type': 'o4',
                'classification_confidence': classification['confidence'],
                'zc_detections': zc_detections,
                'droneid_data': droneid_data
            }
        
        return {'signal_type': 'o4', 'o4_data': None}
    
    def _extract_ofdm_symbols(self, iq_data, zc_detection):
        """Extract OFDM symbols based on ZC detection"""
        # Implementation depends on O4 frame structure
        # This is a placeholder
        return iq_data[:1000]
    
    def _get_pilot_symbols(self):
        """Get known pilot symbols"""
        # Placeholder - need actual O4 pilot pattern
        return np.ones(100)
    
    def _decode_droneid(self, equalized_symbols):
        """Decode DroneID from equalized symbols"""
        # Placeholder - integrate with existing decoder
        return {
            'serial_number': 'UNKNOWN',
            'timestamp': np.time(),
            'frequency': 2442e6
        }
```

### Step 3: Update Configuration

```yaml
# File: config.yaml (add new sections)

signal_processing:
  zc_detector:
    threshold: 0.7
    zc_roots: [600, 147]
    max_delay: 10
  
  channel_estimator:
    window_size: 5
    pilot_pattern: null  # Load from file

fusion:
  remote_id:
    time_window: 2.0
    max_drift: 0.5
    buffer_duration: 60

ml:
  classifier:
    model_path: models/signal_classifier.pkl
    confidence_threshold: 0.8
```

---

## 📊 Testing Plan

### Unit Tests

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_zc_detector.py -v

# Run with coverage
pytest tests/ --cov=src --cov-report=html
```

### Integration Tests

```python
# File: tests/test_integration.py

def test_full_pipeline():
    """Test complete processing pipeline"""
    watchtower = EnhancedDroneWatchtower()
    
    # Load test IQ data
    iq_data = np.fromfile('tests/data/test_o4.iq', dtype=np.complex64)
    
    # Process
    result = watchtower.process_iq(iq_data)
    
    assert result['signal_type'] == 'o4'
    assert result['o4_data'] is not None
    assert 'serial_number' in result['o4_data']
```

### Performance Tests

```python
# File: tests/test_performance.py

import time

def test_processing_speed():
    """Test real-time processing capability"""
    watchtower = EnhancedDroneWatchtower()
    
    # Generate test data (1 second at 20 MSPS)
    iq_data = np.random.randn(20_000_000) + 1j * np.random.randn(20_000_000)
    
    start = time.time()
    result = watchtower.process_iq(iq_data)
    elapsed = time.time() - start
    
    # Should process 1 second of data in < 100ms
    assert elapsed < 0.1, f"Processing too slow: {elapsed:.3f}s"
```

---

## 📚 Documentation

### Update README.md

```markdown
# Drone Watchtower

Enhanced drone detection and tracking system with multi-protocol support.

## New Features

### Signal Processing Enhancements
- **ZC Sequence Detection**: Pulse compression-based detection for improved sensitivity
- **Channel Smoothing**: Time-domain channel estimation for multipath environments
- **ML Signal Classification**: Random forest classifier for signal type identification

### Remote ID Fusion
- Time-correlated fusion of Remote ID and DroneID
- Confidence-scored tracking
- Support for WiFi and Bluetooth Remote ID

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```python
from src.main import EnhancedDroneWatchtower

watchtower = EnhancedDroneWatchtower()
result = watchtower.process_iq(iq_data)
```

## Training the Classifier

```bash
# Prepare training data
python scripts/prepare_training_data.py

# Train classifier
python scripts/train_classifier.py
```
```

---

## 🎯 Success Metrics

### Performance Improvements

| Metric | Current | Target | Improvement |
|--------|---------|--------|-------------|
| O4 Detection Sensitivity | -85 dBm | -95 dBm | +10 dB |
| False Positive Rate | 15% | < 5% | -67% |
| Processing Speed | 500 ms | < 100 ms | 5x faster |
| Position Accuracy | N/A | < 10 m | New capability |

### Feature Completeness

- [x] ZC detection with pulse compression (`src/signal_processing/zc_detector.py`)
- [x] Channel smoothing (`src/signal_processing/channel_estimator.py`)
- [x] ML signal classifier (`src/ml/signal_classifier.py` + `models/signal_classifier.pkl`)
- [x] Remote ID fusion (`src/fusion/remote_id_fusion.py` + live webapp merge)
- [x] Unit tests (`pytest tests/` — 9 passed)
- [ ] Integration tests against live HackRF IQ
- [ ] Documentation updated

---

## 🔗 Related Resources

- [O4 Protocol Analysis](https://github.com/proto17/dji_droneid)
- [DroneID Research](https://github.com/luyii-code-1/dji-ocusync-droneid-research)
- [Radiomaster AX SDK](https://github.com/Radiomaster-RC)

---

## 📝 Notes for Cursor

When implementing these tasks:

1. **Start with Task 1.1** (ZC detection) - it's foundational
2. **Add tests early** - ensures correctness
3. **Profile performance** - real-time processing is critical
4. **Document as you go** - helps future maintenance
5. **Commit frequently** - small, logical commits

**Code Style:**
- Use type hints
- Follow PEP 8
- Add docstrings to all functions
- Use logging instead of print

**Dependencies to Add:**
```txt
# requirements.txt (add these)
numpy>=1.24.0
scipy>=1.10.0
scikit-learn>=1.3.0
joblib>=1.3.0
pytest>=7.4.0
pytest-cov>=4.1.0
```

---

**Last Updated:** 2026-11-10
**Version:** 1.0
**Author:** Hardware Ninja
