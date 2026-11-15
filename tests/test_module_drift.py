"""Guard against drift between the capture demods and their backend copies.

The deploy scripts copy scripts/capture/{elrs,mavlink}_demod.py over
webapp/backend/, so a fix applied to only one copy is silently reverted
the next time the box is deployed. These tests pin the two copies
together, so a divergence fails CI instead of the field.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "scripts" / "capture"
BACKEND = ROOT / "webapp" / "backend"


def test_elrs_demod_copies_match():
    a = (CAPTURE / "elrs_demod.py").read_text()
    b = (BACKEND / "elrs_demod.py").read_text()
    assert a == b, (
        "scripts/capture/elrs_demod.py and webapp/backend/elrs_demod.py have "
        "drifted; the deploy script copies the first over the second, so a "
        "fix applied to only one copy is reverted on the next deploy"
    )


def test_mavlink_demod_copies_match():
    a = (CAPTURE / "mavlink_demod.py").read_text()
    b = (BACKEND / "mavlink_demod.py").read_text()
    assert a == b, (
        "scripts/capture/mavlink_demod.py and webapp/backend/mavlink_demod.py "
        "have drifted; the deploy script copies the first over the second"
    )


def test_demods_do_not_hardcode_a_radio_serial():
    """The HackRF serial identifies the operator's hardware.

    It comes from HACKRF_HOP (or config.json) so a published repo does
    not carry someone's device identifier.
    """
    for path in (CAPTURE / "elrs_demod.py", CAPTURE / "mavlink_demod.py",
                 BACKEND / "elrs_demod.py", BACKEND / "mavlink_demod.py"):
        text = path.read_text()
        assert "0000000000000000" not in text, f"{path} hardcodes a radio serial"


def test_backend_does_not_hardcode_a_radio_serial():
    text = (BACKEND / "app.py").read_text()
    assert "0000000000000000" not in text, "app.py hardcodes a radio serial"


def test_config_json_does_not_carry_a_radio_serial():
    text = (BACKEND / "config.json").read_text()
    assert "0000000000000000" not in text, "config.json carries a radio serial"


if __name__ == "__main__":
    sys.exit(0)
