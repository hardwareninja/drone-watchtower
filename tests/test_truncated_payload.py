"""The truncated-decode stub must never become a detection.

A failed DroneID decode leaves the transport length byte at zero, so the
payload is three bytes. The DJI CRC-16 over those three bytes can be zero
by accident and the CRC-24 covers the transport rather than the payload, so
both checks pass. The same failed burst always produces the same three
bytes, so it passes every time and fills the history with rows that carry
no drone identity.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

import app  # noqa: E402


def line(payload_hex: str, **over) -> str:
    j = {
        "event_time_utc": "2026-11-15T12:42:14.289+00:00",
        "frequency_mhz": 5816.5,
        "payload_hex": payload_hex,
        "payload_classification": "crc_valid_nonclassic_or_encrypted",
        "crc24a_ok": True,
        "dji_crc16_ok": True,
        "dwell_average_power_dbfs": -34.5,
        "payload_sha256": "b817d1bf4fef82c04eafd7945137f5386",
    }
    j.update(over)
    return json.dumps(j)


def test_truncated_stub_is_rejected():
    """The 3-byte stub that passed both CRCs on the live scanner."""
    assert app.parse_line(line("00adb7")) is None


def test_stub_is_rejected_even_without_crc_flags():
    """A short payload is a failed decode whatever the CRC says."""
    assert app.parse_line(line("00adb7", crc24a_ok=False, dji_crc16_ok=False)) is None


def test_real_o4_frame_is_accepted():
    """A 131-byte O4 frame is a detection."""
    evt = app.parse_line(line("80" + "10494e46505c59" + "ce" * 113))
    assert evt is not None
    assert evt["crc"] is True
    assert evt["marker"] == "INFP"


def test_classic_91_byte_frame_is_accepted():
    """The shortest real frame: the classic O2 plaintext payload."""
    payload = "58" + "1002" + "00" * 86 + "1234"
    assert len(payload) == 2 * 91
    evt = app.parse_line(line(payload))
    assert evt is not None


def test_90_bytes_is_still_rejected():
    """One byte under the floor."""
    assert app.parse_line(line("00" * 90)) is None
    assert app.parse_line(line("00" * 91)) is not None


def test_garbage_line_is_rejected():
    assert app.parse_line("not json") is None
    assert app.parse_line(json.dumps({"no": "payload"})) is None
