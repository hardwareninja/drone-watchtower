#!/usr/bin/env python3
"""DJI WiFi-Beacon Remote ID decoder (F3411-22a beacon transport, DJI vendor IE).

The DJI Avata 2 broadcasts Remote ID inside ordinary 802.11 beacon frames on
channel 6, carrying a vendor-specific IE (OUI 6a:5c:35) with a 4-byte header
followed by TLVs:
  03 <len> <ASCII serial>     e.g. "1581F6W8B2448002008T"
  04 04 <int32 latitude>      1e-7 degrees (unverified scale — calibrate)
  05 04 <int32 longitude>
  06 02 / 07 02 / 08 04 / 09 04 / 0a 01 / 0b 02  (velocity/alt/status TBD)

Sniffs wlan0 (monitor mode ch 6, already set by watchtower-remoteid) and
publishes final watchtower events on ZMQ tcp://127.0.0.1:4225 as
{"watchtower_event": {...}} for the webapp backend.
"""
import json
import struct
import time

import zmq
from scapy.all import Dot11, Dot11Beacon, sniff

ZMQ_OUT = "tcp://127.0.0.1:4225"
DJI_OUI = b"\x6a\x5c\x35"

pub = zmq.Context.instance().socket(zmq.PUB)
pub.bind(ZMQ_OUT)


def parse_tlv_stream(body: bytes) -> dict:
    out = {}
    pos = 0
    while pos + 2 <= len(body):
        t, ln = body[pos], body[pos + 1]
        val = body[pos + 2 : pos + 2 + ln]
        if len(val) < ln:
            break
        out[t] = val
        pos += 2 + ln
    return out


def event_from_tlv(tlvs: dict, mac: str) -> dict | None:
    serial = tlvs.get(3, b"").decode("ascii", errors="replace")
    if not serial:
        return None
    lat = struct.unpack(">i", tlvs[4])[0] / 1e7 if 4 in tlvs and len(tlvs[4]) == 4 else 0.0
    lon = struct.unpack(">i", tlvs[5])[0] / 1e7 if 5 in tlvs and len(tlvs[5]) == 4 else 0.0
    evt = {
        "source": "remoteid",
        "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        + f".{int(time.time() * 1000) % 1000:03d}+00:00",
        "session": serial,
        "marker": "RemoteID",
        "mac": mac,
        "crc": True,
        "freq": 2437.0,
        "rssi": 0,
        "lat": lat,
        "lon": lon,
        "alt": 0.0,
        "speed": 0,
        "status": "beacon transport",
        "tlvs": {hex(k): v.hex() for k, v in tlvs.items()},
    }
    return evt


def handle(pkt) -> None:
    if not pkt.haslayer(Dot11Beacon):
        return
    d = pkt[Dot11]
    raw = bytes(pkt)
    i = raw.find(DJI_OUI)
    if i < 0:
        return
    # vendor IE body starts after OUI; skip 4-byte header then TLVs
    body = raw[i + 3 + 4 :]
    tlvs = parse_tlv_stream(body)
    if 3 not in tlvs:
        return
    evt = event_from_tlv(tlvs, d.addr2 or "")
    if evt:
        pub.send_string(json.dumps({"watchtower_event": evt}))
        print(f"[{evt['time']}] {evt['session']} @ ({evt['lat']:.6f},{evt['lon']:.6f})",
              flush=True)


if __name__ == "__main__":
    print("RID beacon decoder: sniffing wlan0 for DJI vendor IEs", flush=True)
    sniff(iface="wlan0", filter="type mgt subtype beacon", prn=handle, store=False)
