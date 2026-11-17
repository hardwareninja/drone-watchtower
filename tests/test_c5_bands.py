"""C5 coverage is one band at a time, and 5 GHz is optional."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys_path = ROOT / "webapp" / "backend"
import sys

sys.path.insert(0, str(sys_path))

import c5_rid

FIRMWARE = (ROOT / "firmware" / "esp32c5-rid" / "main" / "main.c").read_text()
NOW = 1_000.0


def _section(begin: str, end: str) -> str:
    return FIRMWARE.split(begin, 1)[1].split(end, 1)[0]


def _channels(section: str) -> list[int]:
    return [int(n) for n in re.findall(r"\{(\d+)\s*,", section)]


def _health(band: str, wifi5_on: bool, age: float = 1.0) -> dict:
    return c5_rid.health_from_stats(
        {
            "stats": True,
            "band": band,
            "wifi5_on": wifi5_on,
            "ble": 0,
            "wifi24": 3,
            "wifi5": 0,
            "nan": 0,
            "dfs": 1 if band == "5" else 0,
            "t": NOW - age,
        },
        now=NOW - age,
    )


def test_default_hop_is_2_4_only_and_5_ghz_is_gated():
    hop24 = _channels(_section("RID_HOP24_BEGIN", "RID_HOP24_END"))
    hop5 = _channels(_section("RID_HOP5_BEGIN", "RID_HOP5_END"))
    assert hop24 == [6, 1, 6, 11]
    assert 149 in hop5 and 36 in hop5 and 165 in hop5
    assert 52 in hop5 and 100 in hop5 and 144 in hop5
    assert "static volatile int wifi5_enabled;" in FIRMWARE
    assert "wifi5=1" in FIRMWARE and "wifi5=0" in FIRMWARE
    assert "esp_wifi_80211_tx" not in FIRMWARE
    # Boot leaves the flag at zero. wifi5=1 is only the host command.
    assert "static volatile int wifi5_enabled = 1" not in FIRMWARE


def test_5_ghz_channel_149_is_remote_id_not_droneid():
    evt = c5_rid.event_from_firmware(
        {"serial": "SN1", "via": "nan", "ch": 149, "lat": 22.3, "lon": 114.1},
        "2026-11-17T00:00:00.000+00:00",
    )
    assert evt is not None
    assert evt["freq"] == pytest.approx(5745.0)
    assert evt["status"] == "c5 nan 5"
    assert "DroneID" not in evt["status"]
    assert "FPV" not in evt["status"]
    beacon = c5_rid.event_from_firmware(
        {"serial": "SN1", "via": "wifi", "ch": 165},
        "2026-11-17T00:00:00.000+00:00",
    )
    assert beacon["freq"] == pytest.approx(5825.0)
    assert beacon["status"] == "c5 beacon 5"


def test_config_defaults_5_ghz_off_and_the_command_is_receive_side_only():
    cfg = json.loads((ROOT / "webapp" / "backend" / "config.json").read_text())
    assert cfg["c5_wifi5"] is False
    assert cfg["hackrf_serials"]["droneid"] == ""
    assert cfg["hackrf_serials"]["hop"] == ""
    assert c5_rid.wifi5_from_config({}) is False
    assert c5_rid.wifi5_from_config({"c5_wifi5": False}) is False
    assert c5_rid.wifi5_command(False) == b"wifi5=0\n"
    assert c5_rid.wifi5_command(True) == b"wifi5=1\n"
    readme = (ROOT / "README.md").read_text()
    assert "SignalSDR" not in readme
    assert "not dual-concurrent" in readme
    assert "HackRF Pro" in readme


def test_only_one_band_is_marked_receiving():
    cases = [
        ("24", False, "wifi24"),
        ("5", True, "wifi5"),
        ("ble", False, "ble"),
    ]
    for band, wifi5_on, active in cases:
        health = _health(band, wifi5_on)
        view = c5_rid.band_status(True, health, NOW, wifi5_configured=wifi5_on)
        oks = [name for name in ("wifi24", "wifi5", "ble") if view[name] == "ok"]
        assert oks == [active]
        if not wifi5_on and band != "5":
            assert view["wifi5"] == "off"
        blob = " ".join(view["detail"].values()).lower()
        assert "simultaneous" not in blob
        assert "one band at a time" in blob


def test_a_legacy_heartbeat_does_not_paint_two_bands_green():
    health = c5_rid.health_from_stats({"stats": True, "ble": 1, "wifi": 4}, now=NOW)
    view = c5_rid.band_status(True, health, NOW + 1, wifi5_configured=False)
    assert view["wifi5"] == "off"
    assert "ok" not in (view["wifi24"], view["wifi5"], view["ble"])


def test_band_dot_drops_the_previous_state_class():
    """A hop from away/off to ok must not leave the old class on the dot."""
    js = (ROOT / "webapp" / "frontend" / "app.js").read_text()
    assert 'classList.remove("ok", "down", "away", "off", "offline")' in js


def test_5_ghz_stays_off_when_the_radio_is_quiet_and_coverage_is_disabled():
    view = c5_rid.band_status(True, None, NOW, wifi5_configured=False)
    assert view["wifi5"] == "off"
    assert view["wifi24"] == "down"
    assert view["wifi5"] != "ok"
