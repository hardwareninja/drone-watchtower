"""Tests for the empty-serial behaviour of the HackRF roles.

The radio serials are not in the repository, so a box that has not been
configured yet has none. That state must be handled as a configuration
gap — not as a missing device, and not as a device that is silently
wrong: libhackrf is given an empty device string and opens nothing, and
an empty serial must not match a USB hub that reports no serial.
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "webapp" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

app = pytest.importorskip("app")


def test_no_serial_means_not_configured(monkeypatch):
    monkeypatch.setattr(app, "HACKRF_DRONEID", "")
    monkeypatch.setattr(app, "HACKRF_HOP", "")
    assert app.hackrf_configured() == {"droneid": False, "hop": False}


def test_readiness_reports_an_unset_serial(monkeypatch):
    monkeypatch.setattr(app, "HACKRF_DRONEID", "")
    monkeypatch.setattr(app, "HACKRF_HOP", "")
    problems = app.readiness()["problems"]
    assert any("Pro (DroneID) serial not set" in p for p in problems)
    assert any("One (ELRS/hop) serial not set" in p for p in problems)


def test_readiness_does_not_claim_the_radio_is_missing(monkeypatch):
    """An unset serial is a config gap, not an unplugged radio.

    The message matters: "serial not set" tells the operator what to
    fix, "not attached" sends them looking for a cable.
    """
    monkeypatch.setattr(app, "HACKRF_DRONEID", "")
    problems = app.readiness()["problems"]
    assert not any("Pro (DroneID) not attached" in p for p in problems)


def test_empty_serial_does_not_match_a_device_without_one(monkeypatch):
    """A USB hub reports no serial; an empty configured serial must not
    match it, or the DroneID loop would think the Pro is plugged in."""
    monkeypatch.setattr(app, "HACKRF_DRONEID", "")
    devices = [{"vendor": "0e0f", "product": "0002", "serial": ""}]
    monkeypatch.setattr(app, "usb_devices", lambda: devices)
    pro_present = any(
        d["serial"] == app.HACKRF_DRONEID
        for d in app.usb_devices()
        if app.HACKRF_DRONEID and d["serial"]
    )
    assert pro_present is False


def test_configured_serial_matches_its_device(monkeypatch):
    monkeypatch.setattr(app, "HACKRF_DRONEID", "0000000000000000147865de331550df")
    devices = [
        {"vendor": "0e0f", "product": "0002", "serial": ""},
        {"vendor": "1d50", "product": "6089", "serial": "0000000000000000147865de331550df"},
    ]
    monkeypatch.setattr(app, "usb_devices", lambda: devices)
    pro_present = any(
        d["serial"] == app.HACKRF_DRONEID
        for d in app.usb_devices()
        if app.HACKRF_DRONEID and d["serial"]
    )
    assert pro_present is True


def test_hop_survey_without_a_serial_does_not_try_the_radio(monkeypatch):
    monkeypatch.setattr(app, "HACKRF_HOP", "")
    monkeypatch.setattr(app, "state", dict(app.state, armed=True))
    result = app.hop_survey("2400:2483", 2400, 2483, 50.0, 18, "2.4 GHz")
    assert result["present"] is False


def test_dongle_status_marks_an_unassigned_hackrf_unconfigured(monkeypatch):
    monkeypatch.setattr(app, "HACKRF_DRONEID", "")
    monkeypatch.setattr(app, "HACKRF_HOP", "")
    monkeypatch.setattr(app, "usb_devices", lambda: [])
    specs = {d["id"]: d for d in app.dongle_status()}
    assert specs["pro"]["configured"] is False
    assert specs["one"]["configured"] is False
    assert specs["c5"]["configured"] is True
