#!/usr/bin/env python3
"""Analyze a raw .cs8 IQ capture: power envelope, burst statistics,
average spectrum, spectrogram. Memory-safe (chunked processing).

Usage: python3 analyze_capture.py <file.cs8> [sample_rate]
Outputs <file>.envelope.png, <file>.spectrum.png, <file>.specgram.png + stats.
"""
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

path = sys.argv[1]
rate = int(sys.argv[2]) if len(sys.argv) > 2 else 20_000_000
CHUNK = 10_000_000  # samples per chunk

def chunks(path):
    raw = np.memmap(path, dtype=np.int8, mode="r")
    total = len(raw) // 2
    for start in range(0, total, CHUNK):
        seg = raw[start * 2 : (start + CHUNK) * 2]
        yield ((seg[0::2].astype(np.float32) + 1j * seg[1::2].astype(np.float32)) / 127.0)

def main():
    dur = (len(np.memmap(path, dtype=np.int8, mode="r")) // 2) / rate
    print(f"capture: {path}  {dur:.1f}s @ {rate/1e6} Msps")

    # --- envelope: mean dB per 1 ms ---
    per_ms = []
    fft_acc = None
    count = 0
    for c in chunks(path):
        ms_bins = len(c) // (rate // 1000)
        c2 = c[: ms_bins * (rate // 1000)]
        mag = np.abs(c2.reshape(ms_bins, rate // 1000))
        per_ms.append(20 * np.log10(mag.mean(axis=1) + 1e-12))
        # --- average spectrum (50 ffts per chunk) ---
        nfft = 2048
        idx = np.linspace(0, len(c) - nfft, 50, dtype=int)
        frames = np.stack([c[i : i + nfft] for i in idx])
        F = np.abs(np.fft.fftshift(np.fft.fft(frames, nfft, axis=1), axes=1)).mean(axis=0)
        fft_acc = F if fft_acc is None else fft_acc + F
        count += 1

    env = np.concatenate(per_ms)
    spec = fft_acc / count
    db_spec = 20 * np.log10(spec + 1e-12)

    # --- stats: burst duty cycle ---
    thr = env.mean() + 6
    bursts = env > thr
    duty = bursts.mean() * 100
    print(f"envelope: mean {env.mean():.1f} dB, peak {env.max():.1f} dB, "
          f"duty-cycle(>mean+6dB) {duty:.1f}% over {len(env)/1000:.1f}s")

    fig, ax = plt.subplots(figsize=(12, 3))
    ax.plot(env, lw=0.4)
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("mean power (dBFS)")
    ax.set_title(f"Envelope — {path}")
    fig.savefig(path + ".envelope.png", dpi=110, bbox_inches="tight")

    f = np.fft.fftshift(np.fft.fftfreq(2048, 1 / rate)) / 1e6
    fig, ax = plt.subplots(figsize=(12, 3))
    ax.plot(f, db_spec, lw=0.7)
    ax.set_xlabel("offset from center (MHz)")
    ax.set_ylabel("power (dBFS)")
    ax.set_title("Average spectrum")
    fig.savefig(path + ".spectrum.png", dpi=110, bbox_inches="tight")

    # spectrogram of first 0.25 s (first chunk only — memory-safe)
    first = next(chunks(path))[: rate // 4]
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.specgram(first, NFFT=512, Fs=rate / 1e6, noverlap=384, xextent=(0, 0.25))
    ax.set_xlabel("time (s)")
    ax.set_ylabel("offset (MHz)")
    ax.set_title("Spectrogram (first 0.25 s)")
    fig.savefig(path + ".specgram.png", dpi=110, bbox_inches="tight")

    print("saved: .envelope.png .spectrum.png .specgram.png")

if __name__ == "__main__":
    main()
