#!/usr/bin/env python3
"""Plot hackrf_sweep CSV output (power vs frequency).

Usage: python3 plot_sweep.py <sweep.csv> [--peak]
       --peak overlays a marker on the strongest bin per sweep (signal finder)
"""
import argparse
import sys

import matplotlib.pyplot as plt
import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--peak", action="store_true", help="mark strongest bin")
    args = ap.parse_args()

    # hackrf_sweep CSV: date,time,low_hz,high_hz,bin_hz,num_samples,dB,dB,...
    freqs, powers = [], []
    with open(args.csv) as fh:
        for line in fh:
            parts = line.rstrip("\n").split(", ")
            if len(parts) < 7:
                continue
            low, high = int(parts[2]), int(parts[3])
            binw = int(float(parts[4]))
            db = [float(x) for x in parts[6:] if x.strip()]
            f = np.arange(low, high, binw)[: len(db)]
            freqs.extend(f)
            powers.extend(db)

    if not freqs:
        print("no sweep data parsed", file=sys.stderr)
        return 1

    freqs = np.array(freqs) / 1e6
    powers = np.array(powers)

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(freqs, powers, ".", ms=1, alpha=0.3)
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("Power (dB)")
    ax.set_title(f"hackrf_sweep: {args.csv}")

    if args.peak:
        # strongest bin across the whole capture
        i = int(np.argmax(powers))
        ax.plot(freqs[i], powers[i], "r^", ms=10)
        ax.annotate(
            f"{freqs[i]:.2f} MHz @ {powers[i]:.1f} dB",
            (freqs[i], powers[i]),
            textcoords="offset points",
            xytext=(10, 10),
            color="red",
        )

    out = args.csv.rsplit(".", 1)[0] + ".png"
    fig.savefig(out, dpi=120, bbox_inches="tight")
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
