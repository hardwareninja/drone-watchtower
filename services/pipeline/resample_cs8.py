#!/usr/bin/env python3
"""Resample a raw .cs8 IQ file between sample rates (chunked, memory-safe).

Usage: python3 resample_cs8.py <in.cs8> <out.cs8> <in_rate> <out_rate>
Example: python3 resample_cs8.py cap.cs8 cap-15m36.cs8 20000000 15360000
"""
import sys

import numpy as np
from scipy.signal import resample_poly
from math import gcd


def main() -> int:
    src, dst = sys.argv[1], sys.argv[2]
    rate_in, rate_out = int(sys.argv[3]), int(sys.argv[4])
    g = gcd(rate_in, rate_out)
    up, down = rate_out // g, rate_in // g

    raw = np.memmap(src, dtype=np.int8, mode="r")
    total = len(raw) // 2

    CH = 50_000_000          # input samples per chunk
    PAD = 200_000            # overlap to cover FIR transients
    trim_out = int(PAD * rate_out / rate_in)

    print(f"resample {rate_in/1e6:g} -> {rate_out/1e6:g} MS/s "
          f"(up={up}, down={down}), {total/1e6:.1f}M samples")

    # first pass to size output precisely, write incrementally
    n_full = total // CH
    written = 0
    with open(dst, "wb") as out:
        for i in range(n_full + 1):
            start = i * CH
            stop = min(total, start + CH + (PAD if i else 0))
            if stop <= start:
                break
            begin = max(0, start - (PAD if i else 0))
            seg = raw[begin * 2 : stop * 2]
            c = (seg[0::2].astype(np.float32) + 1j * seg[1::2].astype(np.float32)) / 127.0
            r = resample_poly(c, up, down)
            # drop transient region from the second chunk onward
            lo = trim_out if i else 0
            hi = int((stop - start) * rate_out / rate_in) + trim_out
            r = r[lo:hi]
            q = np.clip(np.round(np.real(r) * 127), -128, 127).astype(np.int8)
            qq = np.clip(np.round(np.imag(r) * 127), -128, 127).astype(np.int8)
            inter = np.empty(2 * len(q), np.int8)
            inter[0::2] = q
            inter[1::2] = qq
            out.write(inter.tobytes())
            written += len(q)
            if i % 4 == 0:
                print(f"  chunk {i+1}/{n_full+1}: {written/1e6:.0f}M out-samples")

    print(f"done: {written} samples @ {rate_out/1e6:g} MS/s -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
