#!/usr/bin/env python3
"""One-shot passive ELRS probe for the HackRF One.

Stops the watchtower scan, sweeps 2.4 GHz to find where the hop is
landing, records raw IQ at the busiest bin, and measures burst timing.
No payload decoding, no control-channel contents.
"""
import os
import subprocess
import sys
import time
import urllib.request

ONE = "0000000000000000a06063c82451835f"
API = "http://127.0.0.1:8080"
IQ = "/tmp/elrs-probe.iq"
RATE = 10_000_000          # 10 Msps -> 10 MHz window, ~20 MB/s over USB2
NSAMP = 40_000_000        # 4 seconds
DEC = 10                   # decimate envelope by 10 -> 1 us per sample

try:
    import numpy as np
except ImportError:
    np = None


def post(path):
    req = urllib.request.Request(API + path, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def sweep_bins():
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
    return bins, vals


def analyze():
    size = os.path.getsize(IQ)
    print(f"iq_bytes {size} samples {size // 2}")
    runs = []
    if np is not None:
        iq = np.fromfile(IQ, dtype=np.int8)
        i_arr = iq[0::2].astype(np.float32)
        q_arr = iq[1::2].astype(np.float32)
        p = (i_arr * i_arr + q_arr * q_arr)[::DEC]
        med = float(np.median(p[::100]))
        thr = med * (10 ** (12.0 / 10.0))
        above = p > thr
        n = len(above)
        start = None
        for k in range(n):
            if above[k] and start is None:
                start = k
            elif not above[k] and start is not None:
                if k - start >= 3:            # >= 3 us
                    runs.append((start, k - start))
                start = None
        if start is not None and n - start >= 3:
            runs.append((start, n - start))
    else:
        med_vals, chunks = [], []
        with open(IQ, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                chunks.append(b)
                for j in range(0, len(b) - DEC * 2, DEC * 2):
                    I = b[j]; Q = b[j + 1]
                    med_vals.append(I * I + Q * Q)
        med_vals.sort()
        med = med_vals[len(med_vals) // 2]
        thr = med * (10 ** (12.0 / 10.0))
        start = None
        pos = 0
        for b in chunks:
            for j in range(0, len(b) - DEC * 2 + 1, DEC * 2):
                I = b[j]; Q = b[j + 1]
                hit = (I * I + Q * Q) > thr
                if hit and start is None:
                    start = pos
                elif not hit and start is not None:
                    if pos - start >= 3:
                        runs.append((start, pos - start))
                    start = None
                pos += 1
    return runs


def main():
    post("/api/scan/stop")
    time.sleep(3)
    print("scan paused")

    bins, vals = sweep_bins()
    if not vals:
        print("SWEEP-EMPTY")
        post("/api/scan/start")
        return
    vals.sort()
    floor = vals[len(vals) // 2]
    bursty = sorted(((max(dbs), mhz) for mhz, dbs in bins.items() if max(dbs) > floor + 15), reverse=True)
    print(f"floor_db {round(floor, 1)}")
    print("top_bins", [(m, round(p, 1)) for p, m in bursty[:8]])
    if not bursty:
        print("LINK-ABSENT")
        post("/api/scan/start")
        print("scan restarted")
        return

    center = int(bursty[0][1] * 1e6 + 125_000)
    print(f"capture_center_hz {center}")
    cap = subprocess.run(
        ["hackrf_transfer", "-r", IQ, "-f", str(center), "-s", str(RATE),
         "-n", str(NSAMP), "-l", "32", "-g", "24", "-a", "0"],
        capture_output=True, timeout=180,
    )
    if cap.returncode != 0:
        print("CAPTURE-FAILED")
        print(cap.stderr.decode()[-500:])
        post("/api/scan/start")
        return

    runs = analyze()
    os.remove(IQ)
    if not runs:
        print("NO-BURSTS-IN-WINDOW")
        post("/api/scan/start")
        print("scan restarted")
        return
    durs = [ln for _, ln in runs]
    gaps = []
    for a, b in zip(runs, runs[1:]):
        gaps.append((b[0] - a[0]) * DEC / 10_000.0)   # ms
    durs.sort(); gaps.sort()
    med_dur = durs[len(durs) // 2] * DEC / 10_000.0   # ms
    med_gap = gaps[len(gaps) // 2] if gaps else 0
    print(f"bursts {len(runs)}")
    print(f"median_duration_ms {round(med_dur, 3)}")
    print(f"median_interval_ms {round(med_gap, 3)}")
    if med_gap:
        print(f"in_window_rate_hz {round(1000.0 / med_gap, 1)}")
        est = (1000.0 / med_gap) * (83.0 / 10.0)
        print(f"full_band_rate_estimate_hz {round(est, 0)}")
    post("/api/scan/start")
    print("scan restarted")


if __name__ == "__main__":
    main()
