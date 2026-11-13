import glob
import importlib.util
import json
import sys

import numpy as np

spec = importlib.util.spec_from_file_location("demod", "/tmp/elrs_demod.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

path = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob("/home/drone/elrs-captures/*/capture.iq"))[-1]
print("analyzing", path)
sig = mod.load_baseband(path)
print("baseband samples", len(sig))

for sf in (5,):
    N = 1 << sf
    t = np.arange(N)
    ref_down = np.exp(-1j * np.pi * t * t / N).astype(np.complex64)
    for inverted in (False, True):
        ref = np.conj(ref_down) if inverted else ref_down
        bins, mags, step = mod.window_peaks(sig, N, ref)
        med = float(np.median(mags))
        for mult in (4.0, 2.5):
            thr = med * mult
            runs = mod.find_runs(bins, mags, thr, N)
            good = []
            for rs, _b in runs:
                p = mod.demod_packet(sig, rs, N, ref, thr, step)
                if p and p["sync"] == [8, 16] and p["downchirps_ok"]:
                    good.append(p)
            kind = "inverted(downlink)" if inverted else "normal(uplink)"
            print(f"SF{sf} {kind:20s} thr x{mult}: runs {len(runs):4d}, verified ELRS packets {len(good)}")
            for p in good[:3]:
                print("   sample", p["sample"], "snr", p["snr_db"], "syms", p["symbols"][:10])
