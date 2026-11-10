#!/usr/bin/env python3
"""Prepare training data from raw IQ files (Task 3.1)."""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np


def prepare_training_data(data_dir: str | Path = "data/raw"):
    """Prepare training data from raw IQ files.

    Expected layout:
      data/raw/{o4,wifi,bluetooth,elrs,noise}/*.iq  (complex64)
    """
    training_data = []
    labels = []
    root = Path(data_dir)
    for signal_type in ["o4", "wifi", "bluetooth", "elrs", "noise"]:
        signal_dir = root / signal_type
        if not signal_dir.exists():
            print(f"Warning: {signal_dir} not found, skipping")
            continue
        for iq_file in signal_dir.glob("*.iq"):
            iq_data = np.fromfile(iq_file, dtype=np.complex64)
            if iq_data.size > 10000:
                iq_data = iq_data[:10000]
            elif iq_data.size < 10000:
                iq_data = np.pad(iq_data, (0, 10000 - iq_data.size))
            training_data.append(iq_data)
            labels.append(signal_type)
    return training_data, labels


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data/raw")
    ap.add_argument("--out", default="data/training_cache.npz")
    args = ap.parse_args()
    data, labels = prepare_training_data(args.data_dir)
    print(f"Loaded {len(data)} samples across {sorted(set(labels)) or 'no labels'}")
    if data:
        np.savez_compressed(
            args.out,
            data=np.stack([np.asarray(x, dtype=np.complex64) for x in data]),
            labels=np.asarray(labels),
        )
        print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
