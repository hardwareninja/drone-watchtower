#!/usr/bin/env python3
"""Passive ExpressLRS LoRa demodulator for the HackRF One. 2.4 GHz.

Captures IQ at 3.25 Msps (4x the ELRS LoRa bandwidth, so the decimate-by-4
baseband lands exactly on 812.5 kHz), then dechirps at SF5..SF8 in both
IQ polarities to find ELRS preambles. For each packet it records the
channel offset, SNR, sync symbols, and the raw payload symbol stream.
No control-channel decoding; symbols are stored for later analysis.

Usage: python3 elrs_demod.py [--iq FILE] [--seconds 15] [--center HZ]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import numpy as np

ONE = "0000000000000000a06063c82451835f"
API = "http://127.0.0.1:8080"
OUT = "/home/drone/elrs-captures"

FS_CAP = 3_250_000
BW = 812_500
DEC = 4                      # fs analysis = 812.5k = ELRS LoRa bandwidth
SF_LIST = (5, 6, 7, 8)
STEP_DIV = 4                 # coarse window step = N/4
MIN_RUN = 10                 # >= 2.5 stable symbols after fragmentation
MAX_RUN = 90                 # preamble is 12-14 symbols; longer = spur
EDGE_SKIP = 3                # ignore bins near 0 and N-1 (DC / filter edge)
MAX_PAYLOAD_SYMS = 48


def post(path):
    req = urllib.request.Request(API + path, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def sweep_best_center():
    """Pick a hop frequency away from Wi-Fi channel centers."""
    out = subprocess.run(
        ["hackrf_sweep", "-d", ONE, "-f", "2400:2483", "-w", "250000",
         "-l", "32", "-g", "24", "-N", "8", "-r", "-"],
        capture_output=True, text=True, timeout=120,
    )
    bins, vals = {}, []
    for line in out.stdout.splitlines():
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
        return None
    vals.sort()
    floor = vals[len(vals) // 2]
    best = None
    for mhz, dbs in bins.items():
        peak = max(dbs)
        if peak <= floor + 15 or not (2415 <= mhz <= 2475):
            continue
        nearest = min(abs(mhz - (2412 + 20 * k)) for k in range(13))
        score = peak - (25 if nearest < 8 else 0)
        if best is None or score > best[0]:
            best = (score, mhz, peak)
    if best is None:
        return None
    print(f"sweep floor {round(floor,1)} dB, chosen bin {best[1]} MHz (peak {round(best[2],1)} dB)")
    return int(best[1] * 1e6 + 125_000)


def capture(center, seconds):
    path = "/tmp/elrs-demod.iq"
    cap = subprocess.run(
        ["hackrf_transfer", "-d", ONE, "-r", path, "-f", str(center), "-s", str(FS_CAP),
         "-n", str(FS_CAP * seconds), "-l", "32", "-g", "24", "-a", "0"],
        capture_output=True, timeout=seconds + 120,
    )
    if cap.returncode != 0:
        print("capture failed:", cap.stderr.decode()[-400:])
        return None
    return path


def lowpass_taps():
    """33-tap sinc-hann keeping +-406 kHz for the decimate-by-4."""
    m = 33
    n = np.arange(m) - m // 2
    h = np.sinc(0.25 * n) * np.hanning(m)
    return (h / h.sum()).astype(np.float32)


def load_baseband(path):
    """int8 IQ -> complex64 at exactly 812.5 kHz, chunked to keep RAM low."""
    taps = lowpass_taps()
    edge = len(taps) - 1
    raw = np.fromfile(path, dtype=np.int8)
    out = []
    carry = np.empty(0, dtype=np.int8)
    CH = 8_000_000                     # int8 samples per chunk (I/Q interleaved)
    for s in range(0, len(raw), CH):
        blk = np.concatenate([carry, raw[s:s + CH]])
        carry = raw[max(0, s + CH - edge):s + CH] if s + CH < len(raw) else raw[s + CH - edge:s + CH] if s + CH - edge >= 0 else np.empty(0, np.int8)
        f = blk.astype(np.float32) / 127.0
        i = np.convolve(f[0::2], taps, mode="same")
        q = np.convolve(f[1::2], taps, mode="same")
        c = (i + 1j * q).astype(np.complex64)
        out.append(c[::DEC])
    return np.concatenate(out)


def load_raw(path):
    """int8 IQ -> complex64 at the full 3.25 Msps capture rate (for FLRC)."""
    raw = np.fromfile(path, dtype=np.int8)
    i = raw[0::2].astype(np.float32)
    q = raw[1::2].astype(np.float32)
    return ((i + 1j * q) / 127.0).astype(np.complex64)


FLRC_ENV_DEC = 20                  # envelope decimation: 1 sample = ~5 FLRC bits
FLRC_SPB = 5                      # 3.25 Msps / 650 kbps
FLRC_MIN_ALT = 24                 # alternating preamble bits required


def flrc_bursts(sig):
    """Energy bursts of FLRC-packet length: ~150-600 us."""
    p = (np.abs(sig) ** 2)[::FLRC_ENV_DEC].astype(np.float32)
    med = float(np.median(p[::50])) if len(p) > 50 else 1.0
    if med <= 0:
        return [], 1.0
    thr = med * 4.0
    runs = []
    n = len(p)
    i = 0
    while i < n:
        if p[i] <= thr:
            i += 1
            continue
        j = i
        while j + 1 < n and p[j + 1] > thr:
            j += 1
        length = j - i + 1                      # env samples; 1 env = 12.3 us
        if 10 <= length <= 70:                  # 123..860 us: FLRC packet(s)
            runs.append((i * FLRC_ENV_DEC, (j + 1) * FLRC_ENV_DEC, float(p[i:j + 1].max())))
        i = j + 1
    return runs, med


def flrc_word_from_burst(seg):
    """Anchor on the alternation break at the preamble/sync boundary."""
    if len(seg) < 500:
        return None
    d = np.angle(seg[1:] * np.conj(seg[:-1])) * (FS_CAP / (2 * np.pi))   # Hz
    d = d - np.median(d)                    # remove DC / LO offset
    span = 32 * FLRC_SPB                    # 160 samples of preamble
    ref = np.repeat(np.tile([1.0, -1.0], 16), FLRC_SPB) * 325_000.0
    c = np.correlate(d, ref, mode="valid")
    kc = int(np.argmax(np.abs(c)))          # coarse preamble position, polarity-free
    best = None
    for ph in range(FLRC_SPB):
        b = (d > 0).astype(np.int8)
        s = b[ph::FLRC_SPB]
        if len(s) < 70:
            continue
        runs = []
        st = 0
        while st < len(s) - 1:
            if s[st] != s[st + 1]:
                L = 2
                while st + L < len(s) and s[st + L] != s[st + L - 1]:
                    L += 1
                runs.append((st, L))
                st += L
            else:
                st += 1
        if not runs:
            continue
        st, L = max(runs, key=lambda r: r[1])
        if L < 28:
            continue
        re = st + L                        # where alternation breaks
        expect = (kc + span + 2 - ph) / FLRC_SPB     # coarse sync start, bit units

        def lead_alt(w):
            f = 1
            for i in range(1, len(w)):
                if w[i] != w[i - 1]:
                    f += 1
                else:
                    break
            return f

        cands = []
        for e in range(0, 9):              # sync may open with several alternating bits
            ss = re - e
            if ss < 0 or ss + 32 > len(s):
                continue
            w = s[ss:ss + 32]
            if lead_alt(w) == e:            # self-consistent boundary
                cands.append("".join(str(int(x)) for x in w))
        if not cands and (re < expect - 6 or re > expect + 10):
            continue                        # boundary nowhere near the packet
        if not cands:
            ss = max(0, int(round(expect)))
            if ss + 32 > len(s):
                continue
            cands.append("".join(str(int(x)) for x in s[ss:ss + 32]))
        if best is None or L > best[0]:
            best = (L, cands)
    if not best:
        return None
    return best[1]


def flrc_sync_words(sig):
    """Cluster bursts by their 32-bit sync word. Returns word -> stats."""
    runs, med = flrc_bursts(sig)
    votes: dict[str, int] = {}
    peaks: dict[str, float] = {}
    for s, e, peak in runs:
        s2 = max(0, s - 15 * FLRC_SPB)          # margin so the preamble start survives
        e2 = min(len(sig), e + 2 * FLRC_ENV_DEC)
        cands = flrc_word_from_burst(sig[s2:e2])
        if not cands:
            continue
        for word in set(cands):                 # one vote per word per burst
            votes[word] = votes.get(word, 0) + 1
            peaks[word] = max(peaks.get(word, 0.0), peak)
    if not votes:
        return {}
    top = max(votes.values())
    words = {}
    for word, n in votes.items():               # keep words that win real bursts
        if n >= max(2, top // 3):
            words[word] = {"count": n, "peak": peaks[word],
                           "snr_db": round(10 * np.log10(max(peaks[word], 1e-12) / med), 1)}
    return words


def window_peaks(sig, N, ref):
    """Dechirp every N-sample window (step N/4) against ref. Returns bins, mags."""
    step = max(1, N // STEP_DIV)
    nwin = (len(sig) - N) // step + 1
    bins = np.empty(nwin, dtype=np.int16)
    mags = np.empty(nwin, dtype=np.float32)
    stride = sig.strides[0]
    W = np.lib.stride_tricks.as_strided(
        sig, shape=(nwin, N), strides=(step * stride, stride))
    chunk = max(20_000, 4_000_000 // N)
    for s in range(0, nwin, chunk):
        w = W[s:s + chunk]
        f = np.fft.fft(w * ref)
        mag = np.abs(f)
        m = mag.argmax(axis=1)
        bins[s:s + chunk] = m
        mags[s:s + chunk] = mag[np.arange(len(w)), m]
    return bins, mags, step


def find_runs(bins, mags, thr, N):
    """Preamble signature: dechirp bin advances by `step` each window."""
    step = N // STEP_DIV
    runs = []
    i, n = 0, len(bins)
    while i < n:
        if mags[i] <= thr:
            i += 1
            continue
        j = i
        while (j + 1 < n and mags[j + 1] > thr
               and (int(bins[j + 1]) - int(bins[j])) % N == step):
            j += 1
        length = j - i + 1
        if MIN_RUN <= length <= MAX_RUN:
            runs.append((i, int(bins[i])))
        i = j + 1
    return runs


def symbol_at(sig, s0, N, ref):
    if s0 < 0 or s0 + N > len(sig):
        return None, 0.0
    f = np.abs(np.fft.fft(sig[s0:s0 + N] * ref))
    b = int(f.argmax())
    return b, float(f[b])


def demod_packet(sig, run_start, N, ref, thr, step):
    """Full offset search; sync + downchirp purity peaks at the true grid."""
    coarse = run_start * step
    anti = np.conj(ref)
    best = None
    for off in range(N):
        s0 = coarse + off
        if s0 < 0 or s0 + 8 * N > len(sig):
            continue
        bs, pmag, ok = [], 0.0, True
        for k in range(4):                    # 4 clean same-bin preamble windows
            f = np.abs(np.fft.fft(sig[s0 + k * N: s0 + (k + 1) * N] * ref))
            b = int(f.argmax())
            e = float(f[b])
            if e < thr:
                ok = False
                break
            bs.append(b)
            pmag = max(pmag, e)
        if not ok or len(set(bs)) != 1:
            continue
        b0 = bs[0]
        k = 0                                # walk the preamble on this grid
        while True:
            b, _ = symbol_at(sig, s0 + k * N, N, ref)
            if b != b0 or s0 + (k + 1) * N > len(sig):
                break
            k += 1
            if k > 20:
                break
        if k < 8:
            continue
        sync1, m1 = symbol_at(sig, s0 + k * N, N, ref)
        sync2, m2 = symbol_at(sig, s0 + (k + 1) * N, N, ref)
        if sync1 == b0 or sync2 == b0:
            continue
        d = []
        for qq in (2, 3):                    # the 2.25 downchirps after sync
            s = s0 + (k + qq) * N
            if s < 0 or s + N > len(sig):
                d.append(0.0)
                continue
            f = np.abs(np.fft.fft(sig[s: s + N] * anti))
            d.append(float(f.max()))
        score = min(d) + min(m1, m2)
        if best is None or score > best[0]:
            best = (score, s0, k, sync1, sync2, b0, min(d), pmag)
            if score >= 1.8 * pmag:
                break                         # true grid: everything full-scale
    if best is None:
        return None
    _, s0, k, sync1, sync2, b0, dmin, pmag = best
    down_ok = dmin > thr * 0.5
    payload_start = s0 + (k + 2 + 2.25) * N
    syms, mags = [], []
    j = 0
    while j < MAX_PAYLOAD_SYMS:
        s = int(payload_start) + j * N
        if s + N > len(sig):
            break
        b, mg = symbol_at(sig, s, N, ref)
        if mg < thr and j > 2:
            break
        syms.append((b - b0) % N)              # strip the CFO (preamble bin)
        mags.append(mg)
        j += 1
    if len(syms) < 4:
        return None
    snr = 10 * np.log10(max(pmag, 1e-12) / thr) + 6.0
    return {
        "preamble_len": k,
        "sync": [(sync1 - b0) % N, (sync2 - b0) % N],
        "downchirps_ok": down_ok,
        "symbols": syms,
        "sample": s0,
        "bin": b0,
        "snr_db": round(snr, 1),
    }


def scan_polarity(sig, sf, inverted):
    N = 1 << sf
    t = np.arange(N)
    ref_down = np.exp(-1j * np.pi * t * t / N).astype(np.complex64)
    ref = np.conj(ref_down) if inverted else ref_down
    bins, mags, step = window_peaks(sig, N, ref)
    med = float(np.median(mags))
    if med <= 0:
        return []
    thr = med * 4.0
    packets = []
    seen = set()
    for run_start, _first_bin in find_runs(bins, mags, thr, N):
        pkt = demod_packet(sig, run_start, N, ref, thr, step)
        if not pkt or pkt["sample"] in seen:
            continue
        seen.add(pkt["sample"])
        binw_khz = BW / N / 1000.0
        b0 = pkt["bin"]
        fbin = b0 if b0 < N // 2 else b0 - N
        pkt.update({
            "sf": sf,
            "iq": "inverted" if inverted else "normal",
            "freq_offset_khz": round(fbin * binw_khz, 1),
            "median_power": round(10 * np.log10(med), 1),
        })
        packets.append(pkt)
    return packets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iq", help="analyze an existing int8 IQ file instead of capturing")
    ap.add_argument("--seconds", type=int, default=15)
    ap.add_argument("--center", type=int, default=0, help="capture center Hz")
    args = ap.parse_args()

    owned_scan = False
    iq_path = args.iq
    center = args.center

    if not iq_path:
        if center == 0:
            post("/api/scan/stop")
            owned_scan = True
            time.sleep(3)
            center = sweep_best_center()
            if center is None:
                print("LINK-ABSENT")
                post("/api/scan/start")
                return
        iq_path = capture(center, args.seconds)
        if not iq_path:
            if owned_scan:
                post("/api/scan/start")
            return

    sig = load_baseband(iq_path)
    print(f"baseband {len(sig)} samples at {BW} Hz, {round(len(sig)/BW, 1)} s")

    packets = []
    for sf in SF_LIST:
        for inverted in (False, True):
            for p in scan_polarity(sig, sf, inverted):
                packets.append(p)
                print(f"  SF{p['sf']} {p['iq']:8s} bin {p['bin']:3d} "
                      f"offset {p['freq_offset_khz']:+}kHz snr {p['snr_db']} dB "
                      f"preamble {p['preamble_len']} sync {p['sync']} "
                      f"payload_syms {len(p['symbols'])}")
    packets.sort(key=lambda p: p["sample"])

    ups = [p for p in packets if p["iq"] == "normal"]
    downs = [p for p in packets if p["iq"] == "inverted"]
    if len(ups) >= 2:
        gaps = sorted((b["sample"] - a["sample"]) / BW * 1000
                      for a, b in zip(ups, ups[1:]) if b["sample"] > a["sample"])
        if gaps:
            print(f"uplink median interval {round(gaps[len(gaps)//2], 2)} ms")

    ts = time.strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(OUT, ts)
    if owned_scan:
        post("/api/scan/start")
        print("scan restarted")
    if packets:
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "detections.jsonl"), "w") as f:
            for p in packets:
                f.write(json.dumps(p) + "\n")
        shutil.copy(iq_path, os.path.join(outdir, "capture.iq"))
        os.remove(iq_path)
        print(f"saved {len(packets)} packets + IQ to {outdir}")
    else:
        print("NO-LORA-DETECTED")
        if os.path.exists(iq_path) and not args.iq:
            os.remove(iq_path)

    print(f"uplink {len(ups)} downlink {len(downs)}")


if __name__ == "__main__":
    main()
