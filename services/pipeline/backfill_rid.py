#!/usr/bin/env python3
"""Backfill DJI Remote ID beacon events from saved monitor-mode pcaps into
the Drone Watchtower history database, preserving capture timestamps.

Usage: python3 backfill_rid.py <db> <pcap> [<pcap>...]
"""
import sqlite3
import struct
import sys
from datetime import datetime, timezone

from scapy.all import Dot11, Dot11Beacon, rdpcap

DJI_OUI = b"\x6a\x5c\x35"


def parse_tlv_stream(body: bytes) -> dict:
    out, pos = {}, 0
    while pos + 2 <= len(body):
        t, ln = body[pos], body[pos + 1]
        val = body[pos + 2 : pos + 2 + ln]
        if len(val) < ln:
            break
        out[t] = val
        pos += 2 + ln
    return out


def main() -> int:
    db_path, pcaps = sys.argv[1], sys.argv[2:]
    conn = sqlite3.connect(db_path, timeout=10)
    total = 0
    for pcap in pcaps:
        pkts = rdpcap(pcap)
        added = 0
        for p in pkts:
            if not p.haslayer(Dot11Beacon):
                continue
            raw = bytes(p)
            i = raw.find(DJI_OUI)
            if i < 0:
                continue
            tlvs = parse_tlv_stream(raw[i + 3 + 4 :])
            if 3 not in tlvs:
                continue
            serial = tlvs[3].decode("ascii", errors="replace")
            lat = struct.unpack(">i", tlvs[4])[0] / 1e7 if 4 in tlvs and len(tlvs[4]) == 4 else None
            lon = struct.unpack(">i", tlvs[5])[0] / 1e7 if 5 in tlvs and len(tlvs[5]) == 4 else None
            ts = datetime.fromtimestamp(float(p.time), tz=timezone.utc)
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts.isoformat(timespec="milliseconds"), serial, "remoteid",
                 "RemoteID", 2437.0, None, lat, lon, 0.0, 1),
            )
            added += 1
        conn.commit()
        print(f"{pcap}: +{added} RID events")
        total += added
    print(f"total backfilled: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
