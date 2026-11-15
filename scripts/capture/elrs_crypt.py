#!/usr/bin/env python3
"""Decide whether a binding phrase matches observed ELRS traffic.

ExpressLRS does not put the binding phrase into the OTA payload. It uses
it two ways:

  * The MD5 of the phrase gives a 6-byte UID. The CRC of each OTA packet
    is seeded from that UID, so the phrase predicts the CRC.
  * The UID also seeds the FHSS hop sequence, so the phrase predicts
    exactly which frequencies the link uses, and in what order.

The FHSS test is the decisive one: 80 frequencies in a fixed order. A wrong
phrase produces a completely different hop sequence, while the right
phrase produces the one the radio is actually transmitting on.

This module mirrors the firmware:
  * ``src/lib/CONFIG/config.cpp``  - SetBindPhrase
  * ``src/lib/OTA/OTA.cpp``        - OtaUpdateCrcInitFromUid
  * ``src/lib/FHSS/FHSS.cpp``      - FHSSrandomiseFHSSsequenceBuild
  * ``src/lib/FHSS/random.cpp``    - rngSeed / rng
"""
from __future__ import annotations

import hashlib

BIND_KEY = b'-DMY_BINDING_PHRASE="'
UID_LEN = 6
OTA_VERSION_ID = 4
ELRS_CRC16_POLY = 0x3D65
ELRS_CRC14_POLY = 0x2E57

# 2.4 GHz ISM domain, from ``src/lib/FHSS/FHSS.cpp`` (SX128x build).
ISM2G4_START_HZ = 2_400_400_000
ISM2G4_STOP_HZ = 2_479_400_000
ISM2G4_CHANNELS = 80
FHSS_SEQUENCE_LEN = 256


def generate_uid(phrase: str) -> bytes:
    """The 6-byte UID for a binding phrase."""
    data = BIND_KEY + phrase.strip().encode("utf-8") + b'"'
    return hashlib.md5(data).digest()[:UID_LEN]


def uid_seed(uid: bytes) -> int:
    """The FHSS seed, from ``OtaGetUidSeed``."""
    return ((uid[2] << 24) + (uid[3] << 16)
            + (uid[4] << 8) + (uid[5] ^ OTA_VERSION_ID))


def crc16_init(uid: bytes) -> int:
    """The OTA CRC seed, from ``OtaUpdateCrcInitFromUid``."""
    return ((uid[4] << 8) | uid[5]) ^ (OTA_VERSION_ID << 8)


def _crc16_table(poly: int) -> list[int]:
    bitmask = 0xFFFF
    highbit = 1 << 15
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = (crc << 1) ^ (poly if (crc & highbit) else 0)
        table.append(crc & bitmask)
    return table


_CRC_TABLES: dict[int, list[int]] = {}


def crc16(data: bytes, seed: int = 0, poly: int = ELRS_CRC16_POLY) -> int:
    """The ELRS 16-bit CRC."""
    table = _CRC_TABLES.get(poly)
    if table is None:
        table = _crc16_table(poly)
        _CRC_TABLES[poly] = table
    crc = seed & 0xFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ table[((crc >> 8) ^ byte) & 0xFF]
    return crc & 0xFFFF


class _Rng:
    """The firmware's linear congruential RNG, from ``random.cpp``."""

    def __init__(self, seed: int):
        self.seed = seed & 0xFFFFFFFF

    def next(self) -> int:
        m = 2147483648
        a = 214013
        c = 2531011
        self.seed = (a * self.seed + c) % m
        return self.seed >> 16

    def n(self, upper: int) -> int:
        return self.next() % upper


def fhss_sequence(seed: int, freq_count: int = ISM2G4_CHANNELS) -> list[int]:
    """The FHSS hop sequence for a UID seed.

    Mirrors ``FHSSrandomiseFHSSsequenceBuild``. The sequence is built in
    blocks of ``freq_count``; within a block one entry is the sync channel
    and another is channel 0, and the rest are shuffled.
    """
    sync_channel = freq_count // 2
    count = (FHSS_SEQUENCE_LEN // freq_count) * freq_count
    sequence = [0] * count
    for i in range(count):
        if i % freq_count == 0:
            sequence[i] = sync_channel
        elif i % freq_count == sync_channel:
            sequence[i] = 0
        else:
            sequence[i] = i % freq_count

    rng = _Rng(seed)
    for i in range(count):
        if i % freq_count != 0:
            offset = (i // freq_count) * freq_count
            rand = rng.n(freq_count - 1) + 1
            sequence[i], sequence[offset + rand] = (
                sequence[offset + rand], sequence[i])
    return sequence


def fhss_frequencies(seed: int, start_hz: int = ISM2G4_START_HZ,
                     stop_hz: int = ISM2G4_STOP_HZ,
                     freq_count: int = ISM2G4_CHANNELS) -> list[int]:
    """The frequencies the link hops through, in order."""
    seq = fhss_sequence(seed, freq_count)
    freq_spread = (stop_hz - start_hz) // (freq_count - 1)
    return [start_hz + (freq_spread * ch) for ch in seq]


def phrase_frequencies(phrase: str) -> list[int]:
    """The hop frequencies a binding phrase implies."""
    return fhss_frequencies(uid_seed(generate_uid(phrase)))


def match_phrase(observed_hz: list[int], phrase: str,
                 tolerance_hz: int = 250_000) -> dict:
    """Score how well a phrase explains the observed hop frequencies.

    The ELRS hop repeats every 256 packets, so the observed frequencies
    should all appear in the phrase's predicted sequence.
    """
    predicted = phrase_frequencies(phrase)
    predicted_set = set(predicted)
    hits = 0
    for hz in observed_hz:
        if any(abs(hz - p) <= tolerance_hz for p in predicted_set):
            hits += 1
    return {
        "phrase": phrase,
        "hits": hits,
        "total": len(observed_hz),
        "ratio": hits / len(observed_hz) if observed_hz else 0.0,
    }
