import importlib.util
import numpy as np

spec = importlib.util.spec_from_file_location("demod", "/tmp/elrs_demod.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

rng = np.random.default_rng(3)
FS = mod.FS_CAP
DEV = 325_000.0


def make_packet(bits, smooth=4):
    phase = np.zeros(len(bits) * mod.FLRC_SPB)
    for k, bit in enumerate(bits):
        f = DEV if bit else -DEV
        phase[k * mod.FLRC_SPB:(k + 1) * mod.FLRC_SPB] = 2 * np.pi * f / FS
    ph = np.cumsum(phase)
    ph -= ph[0]
    s = np.exp(1j * ph)
    if smooth > 1:
        h = np.hanning(smooth)
        h /= h.sum()
        s = (np.convolve(s.real, h, mode="same") + 1j * np.convolve(s.imag, h, mode="same"))
    return s.astype(np.complex64)


preamble = [i % 2 for i in range(32)]
sync1 = "10110011100010110100110011010011"
payload = [1, 0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1, 1]
bits = preamble + [int(c) for c in sync1] + payload

noise = ((rng.standard_normal(20000) + 1j * rng.standard_normal(20000)) * 0.4).astype(np.complex64)
sig = np.concatenate([noise, make_packet(bits), noise])

runs, med = mod.flrc_bursts(sig)
s, e, peak = runs[0]
s2 = max(0, s - 15 * mod.FLRC_SPB)
e2 = min(len(sig), e + 2 * mod.FLRC_ENV_DEC)
seg = sig[s2:e2]
print("packet starts at segment offset", len(noise) - s2)

d = np.angle(seg[1:] * np.conj(seg[:-1])) * (FS / (2 * np.pi))
d = d - np.median(d)
span = 32 * mod.FLRC_SPB
ref1 = np.repeat(np.tile([1.0, -1.0], 16), mod.FLRC_SPB) * 325_000.0
for name, ref in (("ref1", ref1), ("ref2", -ref1)):
    c = np.correlate(d, ref, mode="valid") / span
    k = int(np.argmax(c))
    print(name, "peak k", k, "value", round(float(c[k]), 0), "len(d)", len(d))
    start = k + span + 2
    samples = d[start + np.arange(32) * mod.FLRC_SPB]
    word = "".join("1" if x > 0 else "0" for x in samples)
    print("  word from that peak:", word)
print("expected sync:", sync1)
print("sign stream of d around true sync (offset 55+160=215..375):")
seg_bits = "".join("1" if x > 0 else "0" for x in d[215:375:5])
print("  d[215:375:5]:", seg_bits)
