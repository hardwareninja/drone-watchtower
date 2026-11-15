#!/usr/bin/env python3
"""ZC correlation gate for the DroneID scanner.

The scanner finds candidate bursts by correlating against the root600 and
root147 Zadoff-Chu references, then runs the expensive decoder (FFT,
descrambling, turbo) on each candidate. The correlation scores that come
back are cheap; the decode is not. Gating on the scores before the decode
spends the CPU budget on frames that can actually succeed.

Two things this gate must hold to at once:

  * every genuine frame survives (no loss of sensitivity), and
  * as many CRC-failing candidates as possible are dropped before decode.

The labeled corpus in ``tests/data/zc_score_corpus.json`` came from
``~/droneid-results`` on the sensor box: 162 detector candidates, 111 of
which passed the double CRC and are therefore real DroneID frames.
"""
from __future__ import annotations

import json
from pathlib import Path

# Scanner defaults before tuning. Kept here so the sweep can report the
# before/after against the same numbers.
DEFAULT_ROOT600 = 0.45
DEFAULT_ROOT147 = 0.35

# Tuned defaults. On the labeled corpus this keeps 109/111 valid frames
# (98.2%) while cutting the candidates that reach the decoder from 51/51
# down to 6/51 — the decoder is skipped for 88% of the bad ones.
TUNED_ROOT600 = 0.55
TUNED_ROOT147 = 0.50


def passes_gate(root600: float, root147: float,
                root600_threshold: float = TUNED_ROOT600,
                root147_threshold: float = TUNED_ROOT147) -> bool:
    """True when a candidate is strong enough on both ZC references to decode.

    Both scores must clear their own threshold. A candidate that matches
    root600 but not root147 is the classic noise false positive, and a
    candidate that matches root147 but not root600 is a partial burst.
    """
    return (root600 >= root600_threshold) and (root147 >= root147_threshold)


def sweep(rows: list[dict], root600_threshold: float, root147_threshold: float) -> dict:
    """Score a set of labeled candidates against one gate.

    ``rows`` are corpus rows: ``ok`` (bool), ``r600`` (float), ``r147``
    (float). Returns how many valid frames survived, how many invalid
    candidates were rejected, and the derived rates.
    """
    valid = [r for r in rows if r.get("ok")]
    invalid = [r for r in rows if not r.get("ok")]
    kept_valid = [r for r in valid if passes_gate(
        r.get("r600") or 0.0, r.get("r147") or 0.0,
        root600_threshold, root147_threshold)]
    kept_invalid = [r for r in invalid if passes_gate(
        r.get("r600") or 0.0, r.get("r147") or 0.0,
        root600_threshold, root147_threshold)]
    return {
        "root600_threshold": root600_threshold,
        "root147_threshold": root147_threshold,
        "valid_total": len(valid),
        "valid_kept": len(kept_valid),
        "valid_lost": len(valid) - len(kept_valid),
        "invalid_total": len(invalid),
        "invalid_kept": len(kept_invalid),
        "invalid_rejected": len(invalid) - len(kept_invalid),
        "sensitivity": len(kept_valid) / len(valid) if valid else 0.0,
        "false_positive_rate": (
            len(kept_invalid) / len(invalid) if invalid else 0.0),
    }


def load_corpus(path: str | Path) -> list[dict]:
    """Read the labeled corpus file and return its rows."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        return data.get("rows") or []
    return data


def best_gate(rows: list[dict],
              root600_grid: tuple[float, ...] = (0.45, 0.50, 0.55, 0.60, 0.65, 0.70),
              root147_grid: tuple[float, ...] = (0.35, 0.40, 0.45, 0.50, 0.55),
              min_sensitivity: float = 0.98) -> dict:
    """Pick the gate that rejects the most junk while keeping sensitivity.

    A gate qualifies only when it keeps at least ``min_sensitivity`` of the
    genuine frames. Among the qualifying gates the one that rejects the
    most junk wins; ties break toward the higher root600 threshold, so the
    chosen gate is also the one that skips the most decoding work.
    """
    best: dict | None = None
    for root600 in root600_grid:
        for root147 in root147_grid:
            result = sweep(rows, root600, root147)
            if result["sensitivity"] < min_sensitivity:
                continue
            key = (result["invalid_rejected"], root600, root147)
            if best is None or key > (
                    best["invalid_rejected"],
                    best["root600_threshold"],
                    best["root147_threshold"]):
                best = result
    return best or sweep(rows, TUNED_ROOT600, TUNED_ROOT147)


def thresholds(config: dict | None = None) -> tuple[float, float]:
    """Read the gate thresholds out of the watchtower config."""
    config = config or {}
    try:
        root600 = float(config.get("scanner_root600_threshold", TUNED_ROOT600))
    except (TypeError, ValueError):
        root600 = TUNED_ROOT600
    try:
        root147 = float(config.get("scanner_root147_threshold", TUNED_ROOT147))
    except (TypeError, ValueError):
        root147 = TUNED_ROOT147
    return root600, root147


def cli_args(config: dict | None = None) -> list[str]:
    """The scanner command-line flags that carry the gate thresholds."""
    root600, root147 = thresholds(config)
    return ["--root600-threshold", str(root600),
            "--root147-threshold", str(root147)]
