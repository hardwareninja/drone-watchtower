import importlib.util
import numpy as np

spec = importlib.util.spec_from_file_location("demod", "/tmp/elrs_demod.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

rng = np.random.default_rng(3)
FS = mod.FS_CAP
DEV = 325_000.0


def make_packet(bits, smooth=4):
    """GFSK-ish: +/-325 kHz FSK with a short smoothing filter."""
    phase = np.zeros(len(bits) * mod.FLRC_SPB)
    for k, bit in enumerate(bits):
        f = DEV if bit else -DEV
        phase[k * mod.FLRC_SPB:(k + 1) * mod.FLRC_SPB] = 2 * np.pi * f / FS
    ph = np.cumsum(phase)
    ph -= ph[0]          # continuous phase
    s = np.exp(1j * ph)
    if smooth > 1:
        h = np.hanning(smooth)
        h /= h.sum()
        i = np.convolve(s.real, h, mode="same")
        q = np.convolve(s.imag, h, mode="same")
        s = i + 1j * q
    return s.astype(np.complex64)


def bits_of(word):
    return [int(c) for c in word]


preamble = [i % 2 for i in range(32)]
sync1 = "10110011100010110100110011010011"
sync2 = "01001100011101001011001100101100"

sig_parts = []
noise = (rng.standard_normal(1_000_000) + 1j * rng.standard_normal(1_000_000)) * 0.4
sig_parts.append(noise)
for word, n in ((sync1, 6), (sync2, 3)):
    for _ in range(n):
        payload = [int(x) for x in rng.integers(0, 2, 20)]
        pkt = make_packet(preamble + bits_of(word) + payload)
        sig_parts.append(pkt * 1.0)
        sig_parts.append(noise[:60000].copy())
sig = np.concatenate(sig_parts)

words = mod.flrc_sync_words(sig)
found = {w: s["count"] for w, s in words.items()}
print("extracted words:", found)
ok1 = found.get(sync1, 0) >= 5
ok2 = found.get(sync2, 0) >= 2
print("word1", "PASS" if ok1 else f"FAIL want {sync1}")
print("word2", "PASS" if ok2 else f"FAIL want {sync2}")
print("ALL-PASS" if ok1 and ok2 else "FAIL")
