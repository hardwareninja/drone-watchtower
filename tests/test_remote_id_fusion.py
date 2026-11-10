"""Tests for Task 2.1 Remote ID fusion."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fusion.remote_id_fusion import (
    DroneIDDetection,
    RemoteIDDetection,
    RemoteIDFusion,
)


def test_time_correlation():
    fusion = RemoteIDFusion(time_window=2.0)
    fusion.add_remote_id(RemoteIDDetection(
        timestamp=100.0, serial_number="ABC123",
        latitude=22.3193, longitude=114.1694, altitude=100.0, source="wifi",
    ))
    fusion.add_droneid(DroneIDDetection(
        timestamp=100.5, serial_number="ABC123",
        frequency=2442e6, rssi=-65, raw_data=b"\x00\x01\x02",
    ))
    tracks = fusion.correlate()
    assert len(tracks) == 1
    assert tracks[0].serial_number == "ABC123"
    assert tracks[0].confidence > 0.8
    assert "remote_id" in tracks[0].sources
    assert "droneid" in tracks[0].sources


def test_serial_number_mismatch():
    fusion = RemoteIDFusion(time_window=2.0)
    fusion.add_remote_id(RemoteIDDetection(
        timestamp=100.0, serial_number="ABC123",
        latitude=22.3193, longitude=114.1694, altitude=100.0, source="wifi",
    ))
    fusion.add_droneid(DroneIDDetection(
        timestamp=100.5, serial_number="XYZ789",
        frequency=2442e6, rssi=-65, raw_data=b"\x00\x01\x02",
    ))
    tracks = fusion.correlate()
    assert len(tracks) == 1
    assert tracks[0].confidence < 0.8


def test_merge_sessions_helper():
    fusion = RemoteIDFusion()
    assert fusion.merge_sessions("A", 100.0, "A", 150.0, window_s=180)
    assert not fusion.merge_sessions("A", 100.0, "B", 150.0, window_s=180)
    assert not fusion.merge_sessions("A", 100.0, "A", 400.0, window_s=180)
