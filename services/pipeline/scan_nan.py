#!/usr/bin/env python3
"""Count 802.11 frame types in a capture; look for NAN (Remote ID) action frames."""
import sys
from collections import Counter

from scapy.all import Dot11, rdpcap

pkts = rdpcap(sys.argv[1] if len(sys.argv) > 1 else "/tmp/rid-ch6.pcap")
frames = 0
actions = 0
nan_oui = 0
nan_srcs = Counter()
srcs = Counter()

for p in pkts:
    if not p.haslayer(Dot11):
        continue
    d = p[Dot11]
    frames += 1
    srcs[d.addr2] += 1
    if b"\x50\x6f\x9a" in bytes(p):  # NAN/Remote ID OUI in ANY frame
        nan_oui += 1
        nan_srcs[d.addr2] += 1
    if d.type == 0 and d.subtype == 13:  # management / action
        actions += 1

print(f"total frames: {frames}")
print(f"action frames: {actions}, with NAN OUI: {nan_oui}")
print("top transmitters:", srcs.most_common(5))
if nan_srcs:
    print("NAN transmitters:", nan_srcs.most_common(5))
