"""Companion runner: proximity, CoT XML, and token handling."""
from __future__ import annotations

import importlib.util
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load():
    path = ROOT / "scripts" / "watchtower_enhancer.py"
    spec = importlib.util.spec_from_file_location("watchtower_enhancer", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["watchtower_enhancer"] = module
    spec.loader.exec_module(module)
    return module


ENH = load()


def test_one_degree_of_latitude_is_about_111_km():
    distance = ENH.haversine_m(0.0, 0.0, 1.0, 0.0)
    assert 110_500 < distance < 111_700


def test_proximity_thresholds_and_empty_fix():
    monitor = ENH.ProximityMonitor(horiz_warn_m=500.0, vert_warn_m=100.0)
    assert monitor.update_drone(
        {"source": "remoteid", "session": "ZERO", "lat": 0, "lon": 0, "alt": 10},
        "remoteid",
    ) is None
    monitor.update_drone(
        {"source": "remoteid", "session": "NEAR", "lat": 10.0, "lon": 20.0, "alt": 100.0},
        "remoteid",
    )
    monitor.update_adsb({
        "hex": "AAA111", "flight": "CLOSE1", "lat": 10.001, "lon": 20.0,
        "alt_ft": 100.0 / ENH.FT_TO_M, "gs_kt": 80, "track": 10,
    })
    close = monitor.check_conflicts()
    assert len(close) == 1
    assert close[0]["severity"] == "CRITICAL"
    assert close[0]["horizontal_m"] < 500
    assert "TCAS" in close[0]["altitude_note"]

    far = ENH.ProximityMonitor(horiz_warn_m=500.0, vert_warn_m=100.0)
    far.update_drone(
        {"source": "droneid", "session": "FAR", "lat": 10.0, "lon": 20.0, "alt": 100.0},
        "detection",
    )
    assert far.drones["FAR"].kind == "droneid"
    far.update_adsb({
        "hex": "BBB222", "flight": "FAR01", "lat": 10.2, "lon": 20.0, "alt_ft": 300,
    })
    assert far.check_conflicts() == []

    high = ENH.ProximityMonitor(horiz_warn_m=5000.0, vert_warn_m=50.0)
    high.update_drone(
        {"source": "remoteid", "session": "LOW", "lat": 10.0, "lon": 20.0, "alt": 100.0},
        "remoteid",
    )
    high.update_adsb({
        "hex": "CCC333", "flight": "HIGH1", "lat": 10.001, "lon": 20.0, "alt_ft": 5000,
    })
    assert high.check_conflicts() == []


def test_live_message_schema_feeds_proximity():
    monitor = ENH.ProximityMonitor()
    alerts = ENH.process_message(
        {"type": "remoteid", "event": {
            "source": "remoteid", "session": "1581F5FJD2300001", "model": "DJI Mavic 3",
            "lat": 22.3193, "lon": 114.1694, "alt": 120.0, "speed": 11.5, "dir": 85.0,
        }},
        monitor, None, None,
    )
    assert alerts == []
    alerts = ENH.process_message(
        {"type": "adsb", "aircraft": [{
            "hex": "780A1B", "flight": "HKSAR01", "lat": 22.3240, "lon": 114.1750,
            "alt_ft": 850, "gs_kt": 110, "track": 240, "source": "1090",
        }]},
        monitor, None, None,
    )
    assert alerts
    assert alerts[0]["aircraft_callsign"] == "HKSAR01"
    assert alerts[0]["drone_id"] == "1581F5FJD2300001"


def test_cot_xml_escapes_callsign_remarks_and_uid():
    hostile = """"><script>alert(1)</script>&'q"""
    track = ENH.Track(
        track_id=hostile,
        kind="remoteid",
        lat=22.3,
        lon=114.1,
        alt_m=40.0,
        label=hostile,
        extra={"selfid": hostile},
    )
    raw = ENH.build_cot_xml(
        track, now=datetime(2026, 11, 17, tzinfo=timezone.utc),
    ).decode("utf-8")
    assert "<script>" not in raw
    assert "alert(1)" in raw
    assert "&lt;" in raw and "&amp;" in raw
    assert "'><script" not in raw
    root = ET.fromstring(raw)
    assert root.get("uid") == "UAS-" + hostile
    assert root.find("detail/contact").get("callsign") == hostile
    assert hostile in root.find("detail/remarks").text
    assert root.find("point").get("lat") == "22.300000"


def test_token_is_not_placed_in_the_websocket_url():
    url, headers = ENH.connection_plan(
        "ws://127.0.0.1:8080/ws?token=s3cret-token&stay=1",
        "s3cret-token",
    )
    assert "s3cret-token" not in url
    assert "token=" not in url
    assert url.startswith("ws://127.0.0.1:8080/ws")
    assert headers["X-Watchtower-Token"] == "s3cret-token"
    assert headers["Authorization"] == "Bearer s3cret-token"
    assert headers["Cookie"] == "watchtower_token=s3cret-token"


def test_zmq_bind_must_be_localhost():
    assert ENH.zmq_endpoint("tcp://127.0.0.1:4224") == "tcp://127.0.0.1:4224"
    assert ENH.zmq_endpoint("") == ""
    with pytest.raises(ValueError):
        ENH.zmq_endpoint("tcp://*:4224")
    with pytest.raises(ValueError):
        ENH.zmq_endpoint("tcp://0.0.0.0:4224")


def test_omitted_flags_do_not_construct_emitters():
    args = ENH.parse_args(["--demo"])
    assert args.cot_host == ""
    assert args.zmq_bind == ""
    assert not (ROOT / "webapp" / "backend" / "cot_publish.py").exists()
