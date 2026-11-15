"""MAVLink payload decoding: GPS_RAW_INT and friends."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

import mavlink_demod as M


def test_gps_raw_int_reads_every_field():
    """The payload is 35 bytes with 12 fields. An earlier decoder unpacked
    only 10 and read "sats" from the wrong offset, so a receiver with 12
    satellites reported zero."""
    lat = int(22.3193 * 1e7)
    lon = int(114.1694 * 1e7)
    payload = struct.pack("<QBiiiHHHHHHH", 1234567890, 3, lat, lon,
                           50000, 80, 120, 45, 18000, 0, 0, 12)
    out = M.decode_payload(24, payload)
    assert abs(out["lat"] - 22.3193) < 1e-6
    assert abs(out["lon"] - 114.1694) < 1e-6
    assert out["alt_m"] == 50.0
    assert out["fix"] == 3
    assert out["sats"] == 12
    assert out["track"] == 180.0


def test_gps_raw_int_handles_a_missing_position():
    payload = struct.pack("<QBiiiHHHHHHH", 1234567890, 0, 0, 0,
                           0, 0, 0, 0, 0, 0, 0, 0)
    out = M.decode_payload(24, payload)
    assert "lat" not in out
    assert out["fix"] == 0
    assert out["sats"] == 0


def test_heartbeat_decodes_into_type_and_mode():
    payload = struct.pack("<IBBBBB", 0x00000001, 3, 0, 0, 4, 3)
    out = M.decode_payload(0, payload)
    assert out["type_name"] == "coaxial"
    assert out["armed"] is False


def test_v1_frame_round_trips_through_extract_frames():
    def pack_v1(msgid, payload, sysid=1, compid=1, seq=0):
        body = bytes([len(payload), seq, sysid, compid, msgid & 0xFF])
        crc = M.crc_x25(body + payload, M.CRC_EXTRA[msgid])
        return (bytes([0xFE]) + body + payload
                + bytes([crc & 0xFF, (crc >> 8) & 0xFF]))

    lat = int(22.3193 * 1e7)
    lon = int(114.1694 * 1e7)
    gps = struct.pack("<QBiiiHHHHHHH", 1, 3, lat, lon, 50000,
                       80, 120, 45, 18000, 0, 0, 12)
    frames = M.extract_frames(pack_v1(24, gps))
    assert len(frames) == 1
    assert frames[0]["msgid"] == 24
    assert frames[0]["sats"] == 12
    assert abs(frames[0]["lat"] - 22.3193) < 1e-6
