"""The ZC gate must not lose real DroneID frames while cutting junk."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

import zc_gate

CORPUS = ROOT / "tests" / "data" / "zc_score_corpus.json"


def _rows() -> list[dict]:
    return zc_gate.load_corpus(CORPUS)


def test_corpus_is_the_expected_shape():
    rows = _rows()
    assert len(rows) == 162
    valid = [r for r in rows if r["ok"]]
    invalid = [r for r in rows if not r["ok"]]
    assert len(valid) == 111
    assert len(invalid) == 51


def test_untuned_gate_rejects_nothing():
    """The old defaults let every bad candidate through to the decoder."""
    result = zc_gate.sweep(_rows(), zc_gate.DEFAULT_ROOT600, zc_gate.DEFAULT_ROOT147)
    assert result["valid_kept"] == result["valid_total"]
    assert result["invalid_kept"] == result["invalid_total"]
    assert result["false_positive_rate"] == 1.0


def test_tuned_gate_keeps_sensitivity_and_cuts_the_decoder_work():
    result = zc_gate.sweep(_rows(), zc_gate.TUNED_ROOT600, zc_gate.TUNED_ROOT147)
    # At most two genuine frames are lost out of 111.
    assert result["valid_kept"] >= 109
    assert result["sensitivity"] >= 0.98
    # And the candidates that reach the decoder drop by well over 80%.
    assert result["invalid_kept"] <= 8
    assert result["false_positive_rate"] <= 0.15


def test_best_gate_is_not_worse_than_the_tuned_one():
    rows = _rows()
    best = zc_gate.best_gate(rows)
    tuned = zc_gate.sweep(rows, zc_gate.TUNED_ROOT600, zc_gate.TUNED_ROOT147)
    assert best["sensitivity"] >= tuned["sensitivity"]
    assert best["invalid_rejected"] >= tuned["invalid_rejected"]


def test_gate_needs_both_references():
    """A candidate strong on root600 but weak on root147 is the classic
    noise false positive, and must not pass."""
    assert not zc_gate.passes_gate(0.99, 0.10)
    assert not zc_gate.passes_gate(0.10, 0.99)
    assert zc_gate.passes_gate(0.60, 0.60)


def test_thresholds_come_from_config_with_sane_defaults():
    assert zc_gate.thresholds({}) == (zc_gate.TUNED_ROOT600, zc_gate.TUNED_ROOT147)
    assert zc_gate.thresholds({"scanner_root600_threshold": 0.7}) == (
        0.7, zc_gate.TUNED_ROOT147)
    # A malformed value falls back rather than breaking the scanner launch.
    assert zc_gate.thresholds({"scanner_root147_threshold": "nope"}) == (
        zc_gate.TUNED_ROOT600, zc_gate.TUNED_ROOT147)


def test_cli_args_carry_both_thresholds():
    args = zc_gate.cli_args({"scanner_root600_threshold": 0.6,
                             "scanner_root147_threshold": 0.5})
    assert args == ["--root600-threshold", "0.6", "--root147-threshold", "0.5"]
