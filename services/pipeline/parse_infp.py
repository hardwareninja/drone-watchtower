#!/usr/bin/env python3
"""Parse DJI O4 INFP (87-style) DroneID packets — length-byte tolerant.

Handles the Mini 5 Pro 138-byte (0x87) variant and other lengths such as
the Avata 2's 131-byte (0x80) variant. Field map per
luyii-code-1/dji-ocusync-droneid-research src/o4_packet_tool.py.

Usage: python3 parse_infp.py <packet_hex>
"""
import math
import struct
import sys


def dji_crc16(data: bytes) -> int:
    crc = 0x3692
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def parse(packet: bytes) -> None:
    p = packet
    declared = p[0] + 3
    print(f"packet_length   : {len(p)}  (declared {declared}, "
          f"{'OK' if len(p) == declared else 'MISMATCH'})")
    print(f"length_byte     : 0x{p[0]:02X}")
    print(f"message_type    : 0x{p[1]:02X}")
    marker = p[2:6].decode("ascii", errors="replace")
    print(f"marker          : {marker}")
    if marker != "INFP":
        print("  -> not an INFP packet; CRYP/AA would need the SM2 path")
        return
    print(f"hashcode        : {p[6:10].hex()}")
    print(f"nonce           : {p[10:18].hex()}")
    enc_len = int.from_bytes(p[18:20], "little")
    print(f"encrypted_length: {enc_len}")
    ct = p[20 : 20 + enc_len]
    print(f"ciphertext      : {len(ct)} bytes, "
          f"entropy {math.log(256, 2) if not ct else sum(ct) and 0 or 0:.1f}"
          if False else f"ciphertext      : {len(ct)} bytes")
    ent = -sum((ct.count(b) / len(ct)) * math.log2(ct.count(b) / len(ct))
               for b in set(ct))
    print(f"  entropy       : {ent:.2f} bits/byte (encrypted ≈ 8, plaintext < 5)")
    print(f"tail            : {p[20 + enc_len :].hex()}")
    crc = dji_crc16(p)
    print(f"dji_crc16       : 0x{crc:04X} ({'VALID' if crc == 0 else 'INVALID'})")


if __name__ == "__main__":
    raw = sys.argv[1]
    if len(sys.argv) > 2:  # allow hex file path
        pass
    try:
        with open(sys.argv[1]) as fh:
            raw = fh.read().strip()
    except OSError:
        pass
    parse(bytes.fromhex("".join(raw.split())))
