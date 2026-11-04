#!/usr/bin/env python3
"""Dissect NAN frames carrying the 50:6F:9A OUI in a pcap."""
import sys
from collections import Counter

from scapy.all import Dot11, rdpcap

pkts = rdpcap(sys.argv[1])
nan_types = Counter()
samples = {}

for p in pkts:
    if not p.haslayer(Dot11) or b"\x50\x6f\x9a" not in bytes(p):
        continue
    d = p[Dot11]
    key = (d.type, d.subtype)
    nan_types[key] += 1
    if key not in samples:
        raw = bytes(p)
        oui_at = raw.find(b"\x50\x6f\x9a")
        samples[key] = {
            "src": d.addr2,
            "len": len(raw),
            "oui_context": raw[max(0, oui_at - 4):oui_at + 24].hex(),
        }

print("NAN frame types (type, subtype) -> count:")
for k, v in nan_types.items():
    print(f"  {k}: {v}")
print()
for k, s in samples.items():
    print(f"sample type={k} src={s['src']} len={s['len']}")
    print(f"  hex around OUI: {s['oui_context']}")
