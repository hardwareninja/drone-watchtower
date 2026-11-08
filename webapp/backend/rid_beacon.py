#!/usr/bin/env python3
"""Wi-Fi beacon Remote ID for any broadcasting drone.

Two vendor IEs are accepted on the same monitor interface:

* ASTM F3411 / OpenDroneID, OUI fa:0b:bc, message pack (any UAS ID)
* DJI beacon TLVs, OUI 6a:5c:35 (Avata, Mini, Air, and the rest of that family)

The USB Wi-Fi adapter is found by its driver, so a different port and a
different interface name both work. It hops 2.4 GHz channels 1, 6, and 11.
Events go out on ZMQ tcp://127.0.0.1:4225 as {"watchtower_event": {...}}.
"""
import json
import os
import struct
from pathlib import Path
import subprocess
import threading
import time

import zmq
from scapy.all import Dot11, Dot11Beacon, sniff

import usb_locate

ZMQ_OUT = "tcp://127.0.0.1:4225"
radio = {"iface": os.environ.get("RID_IFACE") or ""}
DJI_OUI = b"\x6a\x5c\x35"
ASTM_OUI = b"\xfa\x0b\xbc"
CHANNELS = ((1, 2412.0), (6, 2437.0), (11, 2462.0))
tune = {"mhz": 2437.0}

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


def _uas_id(raw: bytes) -> str:
    text = raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()
    if text and all(c.isalnum() or c in "-_" for c in text):
        return text
    return ""


def event_from_id(serial: str, mac: str, lat: float, lon: float, status: str) -> dict | None:
    if not serial:
        return None
    return {
        "source": "remoteid",
        "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        + f".{int(time.time() * 1000) % 1000:03d}+00:00",
        "session": serial,
        "marker": "RemoteID",
        "mac": mac,
        "crc": True,
        "freq": tune["mhz"],
        "rssi": 0,
        "lat": lat,
        "lon": lon,
        "alt": 0.0,
        "speed": 0,
        "status": status,
    }


def event_from_tlv(tlvs: dict, mac: str) -> dict | None:
    serial = _uas_id(tlvs.get(3, b""))
    lat = struct.unpack(">i", tlvs[4])[0] / 1e7 if 4 in tlvs and len(tlvs[4]) == 4 else 0.0
    lon = struct.unpack(">i", tlvs[5])[0] / 1e7 if 5 in tlvs and len(tlvs[5]) == 4 else 0.0
    return event_from_id(serial, mac, lat, lon, "dji beacon")


def event_from_astm(pack: bytes, mac: str) -> dict | None:
    """OpenDroneID Wi-Fi message pack: type/size/count, then fixed-size messages."""
    if len(pack) < 3 + 25:
        return None
    size, count = pack[1], pack[2]
    if not (25 <= size <= 40 and 1 <= count <= 9):
        return None
    if 3 + count * size > len(pack):
        return None
    serial, lat, lon = "", 0.0, 0.0
    for n in range(count):
        msg = pack[3 + n * size:3 + (n + 1) * size]
        mtype = msg[0] >> 4
        if mtype == 0:
            serial = _uas_id(msg[2:22]) or serial
        elif mtype == 1 and len(msg) >= 13:
            lat = struct.unpack_from("<i", msg, 5)[0] / 1e7
            lon = struct.unpack_from("<i", msg, 9)[0] / 1e7
    return event_from_id(serial, mac, lat, lon, "astm beacon")


def publish(evt: dict | None) -> None:
    if not evt:
        return
    pub.send_string(json.dumps({"watchtower_event": evt}))
    print(f"[{evt['time']}] {evt['status']} {evt['session']} "
          f"@ ({evt['lat']:.6f},{evt['lon']:.6f}) {evt['freq']:.0f} MHz", flush=True)


def handle(pkt) -> None:
    if not pkt.haslayer(Dot11Beacon):
        return
    d = pkt[Dot11]
    raw = bytes(pkt)
    mac = d.addr2 or ""
    i = raw.find(DJI_OUI)
    if i >= 0:
        publish(event_from_tlv(parse_tlv_stream(raw[i + 7:]), mac))
        return
    j = raw.find(ASTM_OUI)
    if j >= 0:
        publish(event_from_astm(raw[j + 4:], mac))


def hop_channels() -> None:
    while True:
        iface = radio["iface"]
        for ch, mhz in CHANNELS:
            tune["mhz"] = mhz
            if iface:
                subprocess.run(
                    ["iw", "dev", iface, "set", "channel", str(ch)],
                    capture_output=True,
                )
            time.sleep(3)


def wait_iface() -> str:
    while True:
        iface = os.environ.get("RID_IFACE") or usb_locate.wifi_iface()
        if iface and Path(f"/sys/class/net/{iface}").exists():
            return iface
        print("RID beacon decoder: waiting for a USB Wi-Fi adapter", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=hop_channels, daemon=True).start()
    while True:
        radio["iface"] = wait_iface()
        print(f"RID beacon decoder: {radio['iface']} ASTM + DJI, hopping channels 1/6/11",
              flush=True)
        try:
            sniff(iface=radio["iface"], filter="type mgt subtype beacon", prn=handle, store=False)
        except Exception as exc:
            print(f"RID beacon decoder: {radio['iface']} closed: {exc}", flush=True)
            radio["iface"] = ""
            time.sleep(2)
