import importlib.util
import numpy as np


def upchirp(N):
    t = np.arange(N)
    return np.exp(1j * np.pi * t * t / N)


spec = importlib.util.spec_from_file_location("demod", "/tmp/elrs_demod.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

rng = np.random.default_rng(7)
N = 32
u = upchirp(N)
b0_true = 9
sync_true = [11, 22]
parts = []
for _ in range(12):
    parts.append(np.roll(u, b0_true))
for v in sync_true:
    parts.append(np.roll(u, int(v)))
d = np.roll(np.conj(u), (N - b0_true) % N)
for _ in range(2):
    parts.append(d)
parts.append(d[: N // 4])
pl = [int(v) for v in rng.integers(0, N, 20)]
for v in pl:
    parts.append(np.roll(u, int(v)))
pk = np.concatenate(parts)
padlen = 2 * 812500
pad = (rng.standard_normal(padlen) + 1j * rng.standard_normal(padlen)) * 0.3
sig = np.concatenate([pad, pk, pad])
P = padlen
print("packet starts at sample", P, "b0", b0_true, "sync", sync_true)
print("payload", pl[:8])

step = N // 4
t = np.arange(N)
ref = np.exp(-1j * np.pi * t * t / N).astype(np.complex64)
anti = np.conj(ref)

bins, mags, wstep = mod.window_peaks(sig, N, ref)
med = float(np.median(mags))
thr = med * 4.0
print("thr", round(thr, 2))
runs = mod.find_runs(bins, mags, thr, N)
print("runs", runs)

run_start = runs[0][0]
coarse = run_start * wstep
print("coarse", coarse, "P-coarse", P - coarse)

def sat(s0):
    if s0 < 0 or s0 + N > len(sig):
        return None, 0.0
    f = np.abs(np.fft.fft(sig[s0:s0 + N] * ref))
    b = int(f.argmax())
    return b, float(f[b])

for off in range(N):
    s0 = coarse + off
    if s0 < 0 or s0 + 8 * N > len(sig):
        continue
    bs, pmag, ok = [], 0.0, True
    for k in range(4):
        b, e = sat(s0 + k * N)
        if e < thr:
            ok = False
            break
        bs.append(b)
        pmag = max(pmag, e)
    if not ok or len(set(bs)) != 1:
        continue
    bb = bs[0]
    k = 0
    while True:
        b, _ = sat(s0 + k * N)
        if b != bb:
            break
        k += 1
        if k > 20:
            break
    s1, m1 = sat(s0 + k * N)
    s2, m2 = sat(s0 + (k + 1) * N)
    dd = []
    for qq in (2, 3):
        s = s0 + (k + qq) * N
        if s < 0 or s + N > len(sig):
            dd.append(0.0)
            continue
        f = np.abs(np.fft.fft(sig[s:s + N] * anti))
        dd.append(float(f.max()))
    score = min(dd) + min(m1, m2)
    marker = ""
    if (s0 - P) % N == 0:
        marker = "  <== true grid"
    print(f"off {off:2d} s0-P {s0 - P:6d} b0 {bb:2d} k {k:2d} sync [{s1},{s2}] m1 {m1:5.1f} m2 {m2:5.1f} d {dd[0]:5.1f},{dd[1]:5.1f} score {score:6.1f} pmag {pmag:5.1f}{marker}")
