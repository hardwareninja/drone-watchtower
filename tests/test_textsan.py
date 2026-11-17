"""Tests for the trust-boundary sanitizers and the replay path confinement."""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "webapp" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import textsan  # noqa: E402


def test_safe_label_drops_markup():
    assert "<script>" not in textsan.safe_label("<script>alert(1)</script>")
    # Angle brackets and equals signs are stripped, so no tag and no
    # attribute can be formed.
    assert textsan.safe_label("<img src=x onerror=alert(1)>") == "img srcx onerroralert(1)"


def test_safe_label_keeps_real_callsigns():
    assert textsan.safe_label("B-HXX") == "B-HXX"
    assert textsan.safe_label("GFS28") == "GFS28"
    assert textsan.safe_label("  AAL123  ") == "AAL123"


def test_safe_label_drops_control_characters():
    assert "\n" not in textsan.safe_label("A\nB")
    assert "\x00" not in textsan.safe_label("A\x00B")


def test_safe_label_caps_length():
    assert len(textsan.safe_label("A" * 500)) <= textsan.MAX_LABEL_LEN


def test_safe_hex_only_hex_digits():
    assert textsan.safe_hex("780f7f") == "780F7F"
    # Anything outside A-F is dropped, so a spoofed hex cannot carry a
    # payload into the dashboard.
    assert textsan.safe_hex("78<script>") == "78C"
    assert textsan.safe_hex("'onmouseover=1'") == "EE1"


def test_safe_hex_preserves_non_icao_marker():
    # The local receiver marks a non-ICAO transmitter with "~"; the
    # classifier depends on seeing it.
    assert textsan.safe_hex("~7b1004") == "~7B1004"


def test_safe_hex_caps_length():
    assert len(textsan.safe_hex("A" * 40)) == textsan.MAX_HEX_LEN


def test_csv_cell_neutralizes_formulas_and_keeps_numbers():
    assert textsan.csv_cell("=1+1").startswith("'")
    assert textsan.csv_cell("+cmd").startswith("'")
    assert textsan.csv_cell("-2+3").startswith("'")
    assert textsan.csv_cell("@sum").startswith("'")
    assert textsan.csv_cell(5745.0) == 5745.0
    assert textsan.csv_cell(None) == ""


def test_xml_escape_encodes_markup_and_quotes():
    text = textsan.xml_escape("""Tom & <Jerry> "q" 'x'""")
    assert "<Jerry" not in text
    assert "&amp;" in text
    assert "&lt;" in text
    assert "&quot;" in text
    assert "&apos;" in text
