"""Fuse Remote ID and DroneID detections (Task 2.1)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


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
    raw_data: bytes = b""


@dataclass
class FusedTrack:
    timestamp: float
    serial_number: Optional[str]
    latitude: float
    longitude: float
    altitude: Optional[float]
    confidence: float
    sources: list[str] = field(default_factory=list)


class RemoteIDFusion:
    """Fuse Remote ID and DroneID detections by time and serial."""

    def __init__(self, time_window: float = 2.0, max_drift: float = 0.5):
        self.time_window = time_window
        self.max_drift = max_drift
        self.remote_id_buffer: list[RemoteIDDetection] = []
        self.droneid_buffer: list[DroneIDDetection] = []
        self._matched_rid: set[int] = set()
        self._matched_did: set[int] = set()

    def add_remote_id(self, detection: RemoteIDDetection) -> None:
        self.remote_id_buffer.append(detection)
        cutoff = detection.timestamp - 60
        self.remote_id_buffer = [d for d in self.remote_id_buffer if d.timestamp > cutoff]
        self._matched_rid = {i for i in self._matched_rid if i < len(self.remote_id_buffer)}

    def add_droneid(self, detection: DroneIDDetection) -> None:
        self.droneid_buffer.append(detection)
        cutoff = detection.timestamp - 60
        self.droneid_buffer = [d for d in self.droneid_buffer if d.timestamp > cutoff]
        self._matched_did = {i for i in self._matched_did if i < len(self.droneid_buffer)}

    def correlate(self) -> list[FusedTrack]:
        """Correlate Remote ID and DroneID detections."""
        fused: list[FusedTrack] = []
        used_rid: set[int] = set()
        used_did: set[int] = set()

        for rid_idx, rid in enumerate(self.remote_id_buffer):
            best_score = 0.0
            best_match: tuple[int, DroneIDDetection] | None = None
            for did_idx, did in enumerate(self.droneid_buffer):
                if did_idx in used_did:
                    continue
                time_diff = abs(rid.timestamp - did.timestamp)
                if time_diff > self.time_window:
                    continue
                serial_match = bool(
                    rid.serial_number and did.serial_number
                    and rid.serial_number == did.serial_number
                )
                time_score = 1.0 - (time_diff / self.time_window)
                serial_score = 1.0 if serial_match else 0.5
                # Prefer exact serial matches even with slightly worse time
                total = (0.4 * time_score + 0.6 * serial_score) if serial_match else (
                    0.6 * time_score + 0.4 * serial_score
                )
                if total > best_score:
                    best_score = total
                    best_match = (did_idx, did)
            if best_match and best_score > 0.5:
                did_idx, did = best_match
                used_rid.add(rid_idx)
                used_did.add(did_idx)
                fused.append(FusedTrack(
                    timestamp=(rid.timestamp + did.timestamp) / 2,
                    serial_number=rid.serial_number or did.serial_number,
                    latitude=rid.latitude,
                    longitude=rid.longitude,
                    altitude=rid.altitude,
                    confidence=best_score,
                    sources=["remote_id", "droneid"],
                ))
        self._matched_rid = used_rid
        self._matched_did = used_did
        return fused

    def get_unmatched_remote_ids(self) -> list[RemoteIDDetection]:
        self.correlate()
        return [
            d for i, d in enumerate(self.remote_id_buffer) if i not in self._matched_rid
        ]

    def merge_sessions(
        self,
        rid_serial: str | None,
        rid_time: float | None,
        dji_serial: str | None,
        dji_time: float | None,
        window_s: float = 180.0,
    ) -> bool:
        """Live-dashboard helper: same serial within a window should merge."""
        if not rid_serial or not dji_serial:
            return False
        if rid_serial != dji_serial:
            return False
        if rid_time is None or dji_time is None:
            return True
        return abs(rid_time - dji_time) <= window_s
