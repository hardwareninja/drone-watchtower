"""Station position comes from GPS, then from the network, never from config."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp" / "backend"))

from app import parse_network_geo, station_dict, state


def test_parse_network_shapes():
    assert parse_network_geo({"latitude": 22.3, "longitude": 114.1}) == {
        "lat": 22.3, "lon": 114.1,
    }
    assert parse_network_geo({"loc": "37.4,-122.1"}) == {"lat": 37.4, "lon": -122.1}
    assert parse_network_geo({"success": False, "latitude": 1, "longitude": 2}) is None
    assert parse_network_geo({"latitude": 0, "longitude": 0}) is None
    assert parse_network_geo({"latitude": "nope", "longitude": 1}) is None


def test_gps_overrides_network_and_config_has_no_coordinates():
    saved_gps = state.get("gps")
    saved_net = state.get("network_geo")
    try:
        state["gps"] = {}
        state["network_geo"] = {"lat": 1.5, "lon": 2.5}
        net = station_dict()
        assert net["source"] == "network"
        assert (net["lat"], net["lon"]) == (1.5, 2.5)

        state["gps"] = {"lat": 10.25, "lon": 20.5, "fix": True}
        gps = station_dict()
        assert gps["source"] == "gps"
        assert (gps["lat"], gps["lon"]) == (10.25, 20.5)

        state["gps"] = {}
        state["network_geo"] = {}
        missing = station_dict()
        assert missing["source"] == "none"
        assert missing["lat"] is None and missing["lon"] is None
    finally:
        state["gps"] = saved_gps
        state["network_geo"] = saved_net
