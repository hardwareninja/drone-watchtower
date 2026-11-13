#!/usr/bin/env python3
"""Passive MAVLink capture for Watchtower.

Finds SiK-style GFSK telemetry on 433 / 915 MHz with the HackRF One, and
parses MAVLink v1/v2 frames (HEARTBEAT, GPS, attitude, battery, HUD).
Receive only — no injection, no GCS impersonation.

Usage: python3 mavlink_demod.py --selftest
       python3 mavlink_demod.py --iq FILE --fs 2000000 --baud 64000
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

try:
    import numpy as np
    HAVE_NP = True
except ImportError:
    HAVE_NP = False
    np = None

ONE = os.environ.get("HACKRF_HOP", "0000000000000000a06063c82451835f")
FS_CAP = 2_000_000
FS_WB = 8_000_000
IQ_PATH = "/tmp/mavlink-demod.iq"
BANDS = (("432:436", 432.0, 436.0), ("902:928", 902.0, 928.0))
BAUDS = (64000, 128000, 96000, 32000, 192000, 57600, 19200)
# Si4432 / SiK default sync. Whitening starts after these two bytes.
SIK_SYNC = bytes((0x2D, 0xD4))

STX1, STX2 = 0xFE, 0xFD

# CRC extra bytes for the common FPV / autopilot messages we decode.
CRC_EXTRA = {
    0: 50, 1: 124, 2: 137, 4: 237, 8: 242, 20: 214, 21: 159, 22: 220,
    24: 24, 26: 170, 27: 144, 29: 12, 30: 39, 32: 220, 33: 104, 36: 20,
    42: 28, 62: 183, 65: 118, 74: 20, 76: 152, 77: 143, 87: 133,
    109: 185, 125: 35, 141: 230, 147: 154, 148: 178, 230: 163,
    241: 7, 242: 247, 253: 83, 254: 29,
}

VEHICLE = {
    1: "fixed wing", 2: "quadrotor", 3: "coaxial", 4: "helicopter",
    10: "rover", 11: "boat", 13: "hexarotor", 14: "octorotor",
    15: "tricopter", 19: "VTOL", 20: "VTOL quad", 21: "VTOL hex",
    22: "VTOL octo", 29: "gimbal",
}
AUTOPILOT = {3: "ArduPilot", 4: "OpenPilot", 12: "PX4", 8: "invalid"}
COPTER_MODE = {
    0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED",
    5: "LOITER", 6: "RTL", 7: "CIRCLE", 9: "LAND", 11: "DRIFT",
    13: "SPORT", 14: "FLIP", 15: "AUTOTUNE", 16: "POSHOLD", 17: "BRAKE",
    18: "THROW", 21: "FLOWHOLD", 27: "SYSID",
}


def crc_x25(data: bytes, extra: int | None = None) -> int:
    crc = 0xFFFF
    for b in data:
        tmp = b ^ (crc & 0xFF)
        tmp ^= (tmp << 4) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    if extra is not None:
        tmp = extra ^ (crc & 0xFF)
        tmp ^= (tmp << 4) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def decode_payload(msgid: int, payload: bytes) -> dict:
    out: dict = {}
    if msgid == 0 and len(payload) >= 9:
        custom, typ, ap, base, status, ver = struct.unpack_from("<IBBBBB", payload, 0)
        out.update(
            type=typ, type_name=VEHICLE.get(typ, f"type {typ}"),
            autopilot=ap, autopilot_name=AUTOPILOT.get(ap, f"ap {ap}"),
            armed=bool(base & 0x80), mav_version=ver, custom_mode=custom,
        )
        if ap == 3 and typ in (2, 13, 14, 15):
            out["mode"] = COPTER_MODE.get(custom, f"mode {custom}")
        else:
            out["mode"] = f"mode {custom}"
    elif msgid == 1 and len(payload) >= 31:
        vals = struct.unpack_from("<IIIHHHHHHHBBBB", payload, 0)
        out["voltage"] = round(vals[9] / 1000.0, 2) if vals[9] not in (0, 65535) else None
        rem = vals[12] if len(payload) > 30 else -1
        out["battery_pct"] = rem if 0 <= rem <= 100 else None
    elif msgid == 24 and len(payload) >= 30:
        raw = struct.unpack_from("<QBiiiHHHHB", payload, 0)
        fix, lat, lon, alt, sats = raw[1], raw[2], raw[3], raw[4], raw[9]
        if abs(lat) <= 900000000 and abs(lon) <= 1800000000 and (lat or lon):
            out["lat"] = round(lat / 1e7, 7)
            out["lon"] = round(lon / 1e7, 7)
            out["alt_m"] = round(alt / 1000.0, 1)
        out["fix"] = fix
        out["sats"] = sats
    elif msgid == 33 and len(payload) >= 28:
        _, lat, lon, alt, rel, vx, vy, vz, hdg = struct.unpack_from("<IiiiihhhH", payload, 0)
        if abs(lat) <= 900000000 and abs(lon) <= 1800000000 and (lat or lon):
            out["lat"] = round(lat / 1e7, 7)
            out["lon"] = round(lon / 1e7, 7)
            out["alt_m"] = round(rel / 1000.0, 1)
        out["gs_ms"] = round((vx * vx + vy * vy) ** 0.5 / 100.0, 2)
        out["heading"] = None if hdg == 65535 else round(hdg / 100.0, 1)
    elif msgid == 30 and len(payload) >= 28:
        _, roll, pitch, yaw = struct.unpack_from("<Ifff", payload, 0)
        out["roll"] = round(roll * 57.2958, 1)
        out["pitch"] = round(pitch * 57.2958, 1)
        out["yaw"] = round(yaw * 57.2958, 1)
    elif msgid == 74 and len(payload) >= 20:
        air, gs, hdg, thr, alt, climb = struct.unpack_from("<ffHHff", payload, 0)
        out["gs_ms"] = round(gs, 2)
        out["heading"] = hdg
        out["alt_m"] = round(alt, 1)
        out["throttle"] = thr
    elif msgid == 147 and len(payload) >= 36:
        remaining = struct.unpack_from("<b", payload, 32)[0]
        volts = struct.unpack_from("<10h", payload, 10)
        cells = [v / 1000.0 for v in volts if 0 < v < 60000]
        if cells:
            out["voltage"] = round(sum(cells), 2)
        if 0 <= remaining <= 100:
            out["battery_pct"] = remaining
    elif msgid == 109 and len(payload) >= 9:
        rssi, rem = payload[4], payload[5]
        out["link_rssi"] = rssi
        out["link_remrssi"] = rem
    elif msgid == 253 and len(payload) >= 1:
        text = payload[1:51].split(b"\x00", 1)[0].decode("ascii", "replace")
        if text:
            out["statustext"] = text[:80]
    return out


def extract_frames(buf: bytes) -> list[dict]:
    """Walk a byte stream for MAVLink v1 (0xFE) and v2 (0xFD) with valid CRC."""
    frames = []
    i = 0
    n = len(buf)
    while i < n - 8:
        b = buf[i]
        if b == STX2:
            plen = buf[i + 1]
            flags = buf[i + 2]
            hdr = 10
            total = hdr + plen + 2 + (13 if flags & 1 else 0)
            if i + total > n or plen > 255:
                i += 1
                continue
            msgid = buf[i + 7] | (buf[i + 8] << 8) | (buf[i + 9] << 16)
            extra = CRC_EXTRA.get(msgid)
            crc_got = buf[i + hdr + plen] | (buf[i + hdr + plen + 1] << 8)
            crc_calc = crc_x25(buf[i + 1:i + hdr + plen], extra)
            if extra is None or crc_got != crc_calc:
                i += 1
                continue
            payload = buf[i + hdr:i + hdr + plen]
            info = decode_payload(msgid, payload)
            frames.append({
                "ver": 2, "sysid": buf[i + 5], "compid": buf[i + 6],
                "msgid": msgid, "seq": buf[i + 4], **info,
            })
            i += total
            continue
        if b == STX1:
            plen = buf[i + 1]
            hdr = 6
            total = hdr + plen + 2
            if i + total > n or plen > 255:
                i += 1
                continue
            msgid = buf[i + 5]
            extra = CRC_EXTRA.get(msgid)
            crc_got = buf[i + hdr + plen] | (buf[i + hdr + plen + 1] << 8)
            crc_calc = crc_x25(buf[i + 1:i + hdr + plen], extra)
            if extra is None or crc_got != crc_calc:
                i += 1
                continue
            payload = buf[i + hdr:i + hdr + plen]
            info = decode_payload(msgid, payload)
            frames.append({
                "ver": 1, "sysid": buf[i + 3], "compid": buf[i + 4],
                "msgid": msgid, "seq": buf[i + 2], **info,
            })
            i += total
            continue
        i += 1
    return frames


def pack_v2(msgid: int, payload: bytes, sysid: int = 1, compid: int = 1, seq: int = 0) -> bytes:
    hdr = bytes([STX2, len(payload), 0, 0, seq & 0xFF, sysid, compid,
                 msgid & 0xFF, (msgid >> 8) & 0xFF, (msgid >> 16) & 0xFF])
    extra = CRC_EXTRA[msgid]
    crc = crc_x25(hdr[1:] + payload, extra)
    return hdr + payload + bytes([crc & 0xFF, crc >> 8])


def bits_to_bytes(bits, msb_first: bool) -> bytes:
    n = (len(bits) // 8) * 8
    out = bytearray(n // 8)
    for i in range(0, n, 8):
        v = 0
        chunk = bits[i:i + 8]
        if msb_first:
            for b in chunk:
                v = (v << 1) | int(b)
        else:
            for k, b in enumerate(chunk):
                v |= int(b) << k
        out[i // 8] = v
    return bytes(out)


def uart_bytes(bits) -> bytes:
    """Async 8N1: start 0, 8 data LSB first, stop 1."""
    out = bytearray()
    i = 0
    n = len(bits)
    while i < n - 9:
        if bits[i] != 0:
            i += 1
            continue
        val = 0
        for k in range(8):
            val |= int(bits[i + 1 + k]) << k
        if bits[i + 9] != 1:
            i += 1
            continue
        out.append(val)
        i += 10
    return bytes(out)


def _slice_bits(filt, sps: float):
    """Return candidate bitstreams: integer stride and fractional indexes."""
    out = []
    step = max(2, int(round(sps)))
    best = None
    for off in range(step):
        samples = filt[off::step]
        if len(samples) < 64:
            continue
        mag = float(np.mean(np.abs(samples)))
        if best is None or mag > best[0]:
            best = (mag, samples)
    if best is not None:
        out.append((best[1] > 0).astype(np.uint8))
    nbits = int((len(filt) - 2) / sps)
    if nbits >= 64:
        best = None
        steps = max(4, step)
        for k in range(min(steps, 16)):
            off = k * sps / steps
            idx = np.round(off + np.arange(nbits) * sps).astype(np.int64)
            idx = idx[(idx >= 0) & (idx < len(filt))]
            if len(idx) < 64:
                continue
            samples = filt[idx]
            mag = float(np.mean(np.abs(samples)))
            if best is None or mag > best[0]:
                best = (mag, samples)
        if best is not None:
            out.append((best[1] > 0).astype(np.uint8))
    return out


def gfsk_bits(sig, fs: float, baud: int):
    cands = gfsk_bit_candidates(sig, fs, baud)
    return cands[0] if cands else None


def gfsk_bit_candidates(sig, fs: float, baud: int):
    if sig is None or len(sig) < 64:
        return []
    dphi = np.angle(sig[1:] * np.conj(sig[:-1])).astype(np.float32)
    sps = fs / float(baud)
    if sps < 1.5:
        return []
    win = max(3, int(round(sps)) | 1)
    kernel = np.ones(win, dtype=np.float32) / win
    out = []
    for d in (dphi, dphi - np.median(dphi)):
        filt = np.convolve(d, kernel, mode="same")
        out.extend(_slice_bits(filt, sps))
    return out


def energy_bursts(sig, fs: float):
    """Slice hop dwells out of a parked capture so clock recovery is not noise-dominated."""
    if sig is None or len(sig) < int(fs * 0.003):
        return []
    step = max(1, int(fs / 250_000))
    p = np.abs(sig[::step]).astype(np.float32)
    win = max(8, int(fs / 4000 / step))
    env = np.convolve(p, np.ones(win, dtype=np.float32) / win, mode="same")
    med = float(np.median(env[:: max(1, len(env) // 400)]))
    thr = max(med * 4.0, med + 0.02, 0.04)
    minlen = int(fs * 0.002 / step)
    maxlen = int(fs * 0.080 / step)
    idx = np.where(env >= thr)[0]
    if len(idx) == 0:
        return []
    breaks = np.where(np.diff(idx) > 2)[0]
    starts = np.r_[idx[0], idx[breaks + 1]]
    ends = np.r_[idx[breaks], idx[-1]]
    pad = int(fs * 0.0004)
    out = []
    for a, b in zip(starts, ends):
        if minlen <= (b - a + 1) <= maxlen:
            sa = max(0, int(a * step) - pad)
            sb = min(len(sig), int((b + 1) * step) + pad)
            out.append(sig[sa:sb])
    return out[:48]


def channelize(sig, fs: float, bw: float = 200e3):
    """Mix a hop burst to DC, low-pass, and decimate before GFSK."""
    n = len(sig)
    if n < 64:
        return sig, fs
    spec = np.fft.fft(sig * np.hanning(n))
    k = int(np.argmax(np.abs(spec)))
    freq = float(np.fft.fftfreq(n, 1.0 / fs)[k])
    if abs(freq) < 15e3:
        return sig, fs
    t = np.arange(n, dtype=np.float64) / fs
    bb = sig * np.exp(-1j * 2 * np.pi * freq * t)
    if fs <= 2.5e6 and abs(freq) < 150e3:
        return bb.astype(np.complex64), fs
    fc = min(0.45, bw / fs)
    if fc >= 0.4:
        return bb.astype(np.complex64), fs
    taps = 97
    kidx = np.arange(taps) - (taps - 1) / 2.0
    h = np.sinc(2 * fc * kidx) * np.hamming(taps)
    h /= h.sum()
    filt = np.convolve(bb, h.astype(np.complex64), mode="same")
    dec = max(1, int(round(fs / (bw * 2.5))))
    return filt[::dec].astype(np.complex64), fs / dec


def pn9_xor(data: bytes, seed: int = 0x1FF) -> bytes:
    """Si4432 / SiLabs PN9 whitening, LSB of each byte first, taps x^9+x^5+1."""
    state = seed & 0x1FF
    out = bytearray(len(data))
    for i, byte in enumerate(data):
        w = 0
        for bit in range(8):
            w |= (state & 1) << bit
            nxt = (state ^ (state >> 5)) & 1
            state = ((state >> 1) | (nxt << 8)) & 0x1FF
        out[i] = byte ^ w
    return bytes(out)


def _bit_pattern(data: bytes, msb_first: bool):
    bits = []
    for byte in data:
        if msb_first:
            bits.extend((byte >> k) & 1 for k in range(7, -1, -1))
        else:
            bits.extend((byte >> k) & 1 for k in range(8))
    return bits


def sik_payloads(bits) -> list[bytes]:
    """Find SiK/Si4432 packets: preamble, sync 0x2D 0xD4, then PN9-whitened body."""
    if bits is None or len(bits) < 48:
        return []
    b = np.asarray(bits, dtype=np.uint8)
    payloads = []
    syncs = [
        np.array(_bit_pattern(SIK_SYNC, True), dtype=np.uint8),
        np.array(_bit_pattern(SIK_SYNC, False), dtype=np.uint8),
    ]
    for sync in syncs:
        sl = len(sync)
        i = 0
        n = len(b) - sl - 32
        while i < n:
            if not np.array_equal(b[i:i + sl], sync):
                i += 1
                continue
            rest = bits_to_bytes(b[i + sl:], True)
            rest_le = bits_to_bytes(b[i + sl:], False)
            for raw in (rest, rest_le):
                if len(raw) < 8:
                    continue
                for seed in (0x1FF, 0x1FE):
                    clear = pn9_xor(raw, seed)
                    # hardware header is 2-byte netid; length; then FIFO
                    payloads.append(clear)
                    if len(clear) > 2:
                        payloads.append(clear[2:])
                    if len(clear) > 3:
                        payloads.append(clear[3:])
            i += sl
    return payloads


def parse_sik_bits(bits) -> list[dict]:
    """SiK air frame: 0x2D 0xD4, PN9, 16-bit netid, length, FIFO (+ TDM trailer)."""
    if bits is None or len(bits) < 48:
        return []
    b = np.asarray(bits, dtype=np.uint8)
    pat = np.array(_bit_pattern(SIK_SYNC, True), dtype=np.uint8)
    out = []
    i = 0
    n = len(b) - 48
    while i < n:
        if int(np.sum(b[i:i + 16] != pat)) != 0:
            i += 1
            continue
        if i >= 32:
            pre = b[i - 32:i]
            if int(np.sum(pre[1:] != pre[:-1])) < 26:
                i += 1
                continue
        elif i >= 16:
            pre = b[i - 16:i]
            if int(np.sum(pre[1:] != pre[:-1])) < 12:
                i += 1
                continue
        else:
            i += 1
            continue
        raw = bits_to_bytes(b[i + 16:], True)
        i += 16
        if len(raw) < 8:
            continue
        clear = pn9_xor(raw, 0x1FF)
        netid = int.from_bytes(clear[:2], "big")
        plen = clear[2]
        if netid == 0 or netid == 0xFFFF:
            continue
        body = clear[3:]
        if 4 <= plen <= len(body):
            body = body[:plen]
        mav_body = body[:-2] if len(body) >= 8 else body
        found = extract_frames(mav_body)
        if not found:
            found = extract_frames(body)
        if found:
            for fr in found:
                fr["sik_netid"] = netid
            out.extend(found)
            continue
        sid = netid & 0xFF
        if sid == 0:
            sid = (netid >> 8) & 0xFF or 1
        out.append({
            "sysid": sid, "compid": 1, "msgid": 109,
            "type_name": "SiK radio",
            "autopilot_name": f"netid {netid:04X}",
            "statustext": f"SiK netid {netid:04X}",
            "sik_netid": netid,
        })
    return out


def load_iq(path: str):
    raw = np.fromfile(path, dtype=np.int8)
    if len(raw) < 16:
        return None
    i = raw[0::2].astype(np.float32)
    q = raw[1::2].astype(np.float32)
    return ((i + 1j * q) / 127.0).astype(np.complex64)


def frames_from_bits(bits) -> list[dict]:
    found = []
    seen = set()
    for invert in (False, True):
        b = 1 - bits if invert else bits
        streams = [bits_to_bytes(b, True), bits_to_bytes(b, False), uart_bytes(b)]
        streams.extend(sik_payloads(b))
        for stream in streams:
            for fr in extract_frames(stream):
                key = (fr["sysid"], fr["msgid"], fr.get("seq"))
                if key in seen:
                    continue
                seen.add(key)
                found.append(fr)
    return found


def demod_iq(sig, fs: float) -> list[dict]:
    if not HAVE_NP or sig is None:
        return []
    chunks = energy_bursts(sig, fs)
    if not chunks:
        # No SiK-like bursts. Do NOT demod the whole buffer — on ARM an
        # 8 Msps × 2 s capture stalls the HackRF One hop for minutes.
        if len(sig) > int(fs * 0.12):
            return []
        chunks = [sig]
    found = []
    seen = set()
    for chunk in chunks:
        ch, fs2 = channelize(chunk, fs)
        for baud in BAUDS:
            bitsets = gfsk_bit_candidates(ch, fs2, baud)
            if not bitsets:
                continue
            for bits in bitsets:
                for invert in (False, True):
                    b = (1 - bits) if invert else bits
                    for fr in parse_sik_bits(b) + frames_from_bits(b):
                        key = (fr["sysid"], fr["msgid"], fr.get("seq"), baud)
                        if key in seen:
                            continue
                        seen.add(key)
                        fr["baud"] = baud
                        found.append(fr)
            if found:
                break
    mav = [fr for fr in found if fr.get("type_name") != "SiK radio"]
    sik = [fr for fr in found if fr.get("type_name") == "SiK radio"]
    if sik:
        counts = Counter(fr.get("sik_netid") for fr in sik)
        best_id, nbest = counts.most_common(1)[0]
        if nbest >= 2:
            sik = [fr for fr in sik if fr.get("sik_netid") == best_id]
        found = mav + sik[:1]
    return found


def parse_sweep(text: str, lo: float, hi: float) -> list[tuple[float, float, float]]:
    """Return (mhz, peak_db, floor_db) for narrow (≤0.75 MHz) carriers."""
    bins: dict[float, list[float]] = {}
    vals = []
    for line in text.splitlines():
        parts = line.split(", ")
        if len(parts) < 7:
            continue
        try:
            hz_low = float(parts[2])
            bw = float(parts[4])
            row = [float(x) for x in parts[6:] if x.strip()]
        except ValueError:
            continue
        for i, db in enumerate(row):
            mhz = round((hz_low + i * bw) / 1e6, 3)
            bins.setdefault(mhz, []).append(db)
            vals.append(db)
    if not vals:
        return []
    vals.sort()
    floor = vals[len(vals) // 2]
    hits = sorted((mhz, max(dbs)) for mhz, dbs in bins.items() if max(dbs) > floor + 12)
    clusters = []
    cur = []
    for mhz, pk in hits:
        if not cur or mhz - cur[-1][0] <= 0.35:
            cur.append((mhz, pk))
        else:
            clusters.append(cur)
            cur = [(mhz, pk)]
    if cur:
        clusters.append(cur)
    out = []
    for c in clusters:
        width = c[-1][0] - c[0][0]
        if width > 0.75:
            continue
        if not (lo <= c[0][0] <= hi):
            continue
        peak = max(c, key=lambda x: x[1])
        out.append((peak[0], peak[1], floor))
    out.sort(key=lambda x: -x[1])
    return out


def survey(serial: str = ONE) -> list[tuple[float, float, float]]:
    peaks = []
    for span, lo, hi in BANDS:
        try:
            proc = subprocess.run(
                ["hackrf_sweep", "-d", serial, "-f", span, "-w", "250000",
                 "-l", "32", "-g", "24", "-N", "2", "-r", "-"],
                capture_output=True, text=True, timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        peaks.extend(parse_sweep(proc.stdout, lo, hi))
    peaks.sort(key=lambda x: -x[1])
    return peaks[:2]


def capture(center_hz: int, seconds: float = 1.6, serial: str = ONE,
            fs: int | None = None, lna: int = 16, vga: int = 12, amp: int = 0) -> str | None:
    fs = fs or FS_CAP
    n = int(fs * seconds)
    try:
        cap = subprocess.run(
            ["hackrf_transfer", "-d", serial, "-r", IQ_PATH, "-f", str(center_hz),
             "-s", str(fs), "-n", str(n), "-l", str(lna), "-g", str(vga), "-a", str(amp)],
            capture_output=True, timeout=seconds + 30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cap.returncode != 0 or not Path(IQ_PATH).exists():
        return None
    return IQ_PATH


def merge_vehicle(acc: dict, fr: dict, freq: float | None = None, via: str = "rf") -> None:
    sid = int(fr.get("sysid") or 0)
    if not sid:
        return
    v = acc.setdefault(sid, {
        "sysid": sid, "compid": fr.get("compid"), "packets": 0,
        "via": via, "freq": freq, "type_name": "MAVLink",
    })
    v["packets"] = v.get("packets", 0) + 1
    v["last"] = time.time()
    if freq:
        v["freq"] = freq
    if fr.get("baud"):
        v["baud"] = fr["baud"]
    v["via"] = via
    for key in ("type_name", "autopilot_name", "mode", "armed", "lat", "lon",
                "alt_m", "heading", "gs_ms", "roll", "pitch", "yaw",
                "voltage", "battery_pct", "sats", "fix", "statustext",
                "link_rssi", "sik_netid"):
        if fr.get(key) is not None:
            v[key] = fr[key]


def public_vehicle(v: dict) -> dict:
    return {
        "sysid": v.get("sysid"),
        "compid": v.get("compid"),
        "type_name": v.get("type_name") or "MAVLink",
        "autopilot_name": v.get("autopilot_name") or "autopilot",
        "mode": v.get("mode"),
        "armed": bool(v.get("armed")),
        "lat": v.get("lat"),
        "lon": v.get("lon"),
        "alt_m": v.get("alt_m"),
        "heading": v.get("heading"),
        "gs_ms": v.get("gs_ms"),
        "roll": v.get("roll"),
        "pitch": v.get("pitch"),
        "yaw": v.get("yaw"),
        "voltage": v.get("voltage"),
        "battery_pct": v.get("battery_pct"),
        "sats": v.get("sats"),
        "fix": v.get("fix"),
        "statustext": v.get("statustext"),
        "packets": v.get("packets") or 0,
        "freq": v.get("freq"),
        "baud": v.get("baud"),
        "via": v.get("via") or "rf",
        "sik_netid": v.get("sik_netid"),
        "present": True,
    }


def pass_rf(serial: str = ONE) -> list[dict]:
    """One RF turn: short 915/433 captures, burst-gate, demod SiK/MAVLink.

    Kept deliberately light so the HackRF One hop (ELRS→TBS→MAVLink→VTX)
    does not stall for minutes on empty air.
    """
    if not HAVE_NP:
        return []
    acc: dict[int, dict] = {}
    # 2 Msps is enough for SiK channelize; 8 Msps wideband was too heavy on ARM.
    plans = (
        (915_000_000, FS_CAP, 0.6, 915.0),
        (433_920_000, FS_CAP, 0.4, 433.92),
    )
    t0 = time.time()
    for hz, fs, sec, label in plans:
        path = capture(hz, sec, serial, fs=fs)
        if not path:
            continue
        sig = None
        try:
            sig = load_iq(path)
            bursts = energy_bursts(sig, fs) if sig is not None else []
            frames = demod_iq(sig, fs) if bursts else []
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        nburst = len(bursts) if sig is not None else 0
        nids = sorted({fr.get("sik_netid") for fr in frames if fr.get("sik_netid")})
        print(f"mavlink {label} MHz bursts {nburst} → {len(frames)} frames "
              f"netid {nids} ({time.time() - t0:.1f}s)", flush=True)
        for fr in frames:
            merge_vehicle(acc, fr, freq=label, via=f"{label:.0f} MHz")
        if acc:
            break
    return [public_vehicle(v) for v in acc.values()]


def modulate_raw(data: bytes, fs: int = FS_CAP, baud: int = 64000):
    """MSB-first NRZ GFSK, matching Si4432 packet bits."""
    bits = []
    for byte in data:
        bits.extend((byte >> k) & 1 for k in range(7, -1, -1))
    bits = np.array([1, 0] * 8 + bits + [1, 1, 1, 1], dtype=np.float32)
    sps = int(fs / baud)
    nrz = np.repeat(2 * bits - 1, sps)
    t = np.arange(-6, 7)
    g = np.exp(-0.5 * (t / 1.8) ** 2)
    g /= g.sum()
    shaped = np.convolve(nrz, g, mode="same")
    phase = np.cumsum(shaped * (np.pi * baud / fs))
    return np.exp(1j * phase).astype(np.complex64)


def modulate_gfsk(payload: bytes, fs: int = FS_CAP, baud: int = 64000):
    """8N1 UART over GFSK, for the self-test only."""
    bits = []
    for byte in payload:
        bits.append(0)
        for k in range(8):
            bits.append((byte >> k) & 1)
        bits.append(1)
    bits = np.array([1, 1, 1, 1] + bits + [1, 1, 1, 1], dtype=np.float32)
    sps = int(fs / baud)
    nrz = np.repeat(2 * bits - 1, sps)
    # modest BT gaussian
    t = np.arange(-6, 7)
    g = np.exp(-0.5 * (t / 1.8) ** 2)
    g /= g.sum()
    shaped = np.convolve(nrz, g, mode="same")
    phase = np.cumsum(shaped * (np.pi * baud / fs))
    return np.exp(1j * phase).astype(np.complex64)


def selftest() -> int:
    if not HAVE_NP:
        print("FAIL numpy missing")
        return 1
    hb = pack_v2(0, struct.pack("<IBBBBB", 5, 2, 3, 0x80, 4, 3), sysid=7, seq=1)
    gps = pack_v2(
        33,
        struct.pack("<IiiiihhhH", 1000, 222783000, 1141747000, 120000, 45000, 300, 400, 0, 9000),
        sysid=7, seq=2,
    )
    blob = b"\x00" * 40 + hb + b"\x11" * 8 + gps + b"\x00" * 20
    parsed = extract_frames(blob)
    ids = sorted(p["msgid"] for p in parsed)
    if ids != [0, 33]:
        print("FAIL parse", parsed)
        return 1
    if parsed[0]["type_name"] != "quadrotor" or parsed[0]["sysid"] != 7:
        print("FAIL heartbeat", parsed[0])
        return 1
    if abs(parsed[1]["lat"] - 22.2783) > 1e-4 or abs(parsed[1]["lon"] - 114.1747) > 1e-4:
        print("FAIL gps", parsed[1])
        return 1
    sig = modulate_gfsk(blob)
    noise = (np.random.default_rng(3).standard_normal(len(sig)) +
             1j * np.random.default_rng(4).standard_normal(len(sig))) * 0.05
    recovered = demod_iq(sig + noise.astype(np.complex64), FS_CAP)
    got = {(r["sysid"], r["msgid"]) for r in recovered}
    if (7, 0) not in got or (7, 33) not in got:
        print("FAIL demod", recovered[:8], "n", len(recovered))
        return 1
    # SiK air: preamble + 0x2D 0xD4 + PN9(netid + MAVLink)
    body = pn9_xor(bytes((0x00, 0x19)) + hb + gps)
    air = bytes((0x55, 0x55, 0x55, 0x55, 0x2D, 0xD4)) + body
    quiet = np.zeros(int(FS_CAP * 0.01), dtype=np.complex64)
    hop = modulate_raw(air)
    sik_sig = np.concatenate([quiet, hop, quiet, hop, quiet])
    sik_sig += (np.random.default_rng(5).standard_normal(len(sik_sig)) +
                1j * np.random.default_rng(6).standard_normal(len(sik_sig))) * 0.04
    sik_sig = sik_sig.astype(np.complex64)
    sik_got = {(r["sysid"], r["msgid"]) for r in demod_iq(sik_sig, FS_CAP)}
    if (7, 0) not in sik_got:
        print("FAIL sik", sik_got)
        return 1
    print("PASS mavlink parse + GFSK demod + SiK PN9")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv or not argv[1:]:
        return selftest()
    iq = None
    fs = FS_CAP
    baud = 64000
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--iq":
            iq, i = args[i + 1], i + 2
        elif args[i] == "--fs":
            fs, i = int(args[i + 1]), i + 2
        elif args[i] == "--baud":
            baud, i = int(args[i + 1]), i + 2
        else:
            i += 1
    if not iq:
        return selftest()
    sig = load_iq(iq)
    bits = gfsk_bits(sig, fs, baud) if sig is not None else None
    frames = frames_from_bits(bits) if bits is not None else []
    print(f"{len(frames)} frames")
    for fr in frames[:20]:
        print(fr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
