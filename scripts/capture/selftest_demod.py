import importlib.util
import numpy as np


def upchirp(N):
    t = np.arange(N)
    return np.exp(1j * np.pi * t * t / N)


spec = importlib.util.spec_from_file_location("demod", "/tmp/elrs_demod.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

rng = np.random.default_rng(7)
results = []
for SF, pre in ((5, 12), (6, 14), (7, 12), (8, 12)):
    N = 1 << SF
    u = upchirp(N)
    sync_vals = [11 % N, 22 % N]
    pl = [int(v) for v in rng.integers(0, N, 20)]
    b0 = 9                                  # preamble bin: models the CFO
    parts = []
    for _ in range(pre):
        parts.append(np.roll(u, -b0))       # real LoRa: value v == roll(u, -v)
    for v in sync_vals:
        parts.append(np.roll(u, -(int(v) + b0) % N))
    d = np.roll(np.conj(u), -b0)
    for _ in range(2):
        parts.append(d)
    parts.append(d[: N // 4])
    for v in pl:
        parts.append(np.roll(u, -(int(v) + b0) % N))
    pk = np.concatenate(parts)
    pad = (rng.standard_normal(2 * 812500) + 1j * rng.standard_normal(2 * 812500)) * 0.3
    sig = np.concatenate([pad, pk, pad])
    pkts = []
    for inv in (False, True):
        pkts.extend(mod.scan_polarity(sig, SF, inv))
    good = any(
        p["sync"] == [int(v) for v in sync_vals] and p["symbols"][:20] == pl
        for p in pkts
    )
    results.append((SF, len(pkts), good))
    print(f"SF{SF}: found {len(pkts)}", "PASS" if good else "FAIL " + repr([(p["sync"], p["symbols"][:5]) for p in pkts]))

print("ALL-PASS" if all(r[2] for r in results) else "SOME-FAIL")
