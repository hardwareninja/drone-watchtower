"""Network ADS-B ingestion: shape, distance, and source labelling."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

import adsb_network

# The station sits in Hong Kong; the test aircraft is a known distance away.
STATION_LAT = 22.278317
STATION_LON = 114.174689


def test_haversine_matches_the_feed_distance():
    """adsb.lol reported dst=47.506 nm for this track; our own haversine
    from the station must agree closely."""
    lat, lon = 22.267365, 113.317832
    distance = adsb_network.haversine_nm(STATION_LAT, STATION_LON, lat, lon)
    assert 47.0 < distance < 48.5


def test_haversine_is_zero_for_the_same_point():
    assert adsb_network.haversine_nm(22.0, 114.0, 22.0, 114.0) == 0.0


def test_normalize_track_maps_feed_fields():
    row = {
        "hex": "780f7f",
        "flight": "CSN8697 ",
        "r": "B-32FV",
        "t": "A20N",
        "alt_baro": 13800,
        "gs": 342.8,
        "track": 213.46,
        "lat": 22.275339,
        "lon": 113.338267,
        "squawk": "1167",
        "seen": 1.2,
        "messages": 2535,
        "category": "A3",
    }
    track = adsb_network.normalize_track(row, "adsb.lol", STATION_LAT, STATION_LON)
    assert track["hex"] == "780F7F"
    assert track["flight"] == "CSN8697"
    assert track["type"] == "A20N"
    assert track["alt_ft"] == 13800
    assert track["gs_kt"] == 342.8
    assert track["lat"] == 22.275339
    # Distance is computed from this station, not taken from the feed.
    assert 46.0 < track["distance_nm"] < 48.0
    # Network tracks are always labelled as such.
    assert track["source"] == "net"
    assert track["feed"] == "adsb.lol"


def test_normalize_track_handles_ground_altitude():
    """Feeds report "ground" for the altitude of non-ICAO aircraft."""
    row = {
        "hex": "~7b1004",
        "alt_baro": "ground",
        "gs": 33.5,
        "lat": 22.315075,
        "lon": 113.91037,
        "category": "C2",
    }
    track = adsb_network.normalize_track(row, "adsb.lol", STATION_LAT, STATION_LON)
    assert track["alt_ft"] is None
    assert track["gs_kt"] == 33.5


def test_normalize_track_rejects_rows_without_a_position():
    assert adsb_network.normalize_track({"hex": "abc123"}, "f", 1, 2) is None
    assert adsb_network.normalize_track(
        {"hex": "abc123", "lat": 22.0}, "f", 1, 2) is None
    assert adsb_network.normalize_track(
        {"lat": 22.0, "lon": 114.0}, "f", 1, 2) is None
    assert adsb_network.normalize_track(
        {"hex": "abc123", "lat": 999.0, "lon": 114.0}, "f", 1, 2) is None


def test_local_receiver_wins_a_tie_on_icao_hex():
    local = [{"hex": "ABC123", "source": "1090", "feed": "local"}]
    network = [{"hex": "ABC123", "source": "net", "feed": "adsb.lol"},
               {"hex": "DEF456", "source": "net", "feed": "adsb.lol"}]
    merged = adsb_network.merge_tracks(local, network)
    by_hex = {t["hex"]: t for t in merged}
    assert len(merged) == 2
    assert by_hex["ABC123"]["source"] == "1090"
    assert by_hex["DEF456"]["source"] == "net"


def test_default_feeds_cover_both_key_shapes():
    keys = {feed["key"] for feed in adsb_network.DEFAULT_FEEDS}
    assert keys == {"ac", "aircraft"}
    for feed in adsb_network.DEFAULT_FEEDS:
        assert "{lat}" in feed["url"] and "{lon}" in feed["url"]
        assert "{dist}" in feed["url"]


def test_bounds_from_radius_are_north_south_west_east():
    bounds = adsb_network.bounds_from_radius(22.278, 114.175, 60)
    north, south, west, east = [float(x) for x in bounds.split(",")]
    assert north > 22.278 > south
    assert east > 114.175 > west


def test_normalize_fr24_track_maps_fields_and_heli_category():
    row = {
        "hex": "cabcde",
        "callsign": "GFS28 ",
        "reg": "B-HXX",
        "type": "H145",
        "category": "H",
        "alt": 900,
        "gspeed": 95.5,
        "track": 180,
        "lat": 22.3,
        "lon": 114.2,
        "squawk": "6001",
        "timestamp": "2026-11-14T12:00:00Z",
    }
    track = adsb_network.normalize_fr24_track(row, STATION_LAT, STATION_LON)
    assert track["hex"] == "CABCDE"
    assert track["flight"] == "GFS28"
    assert track["type"] == "H145"
    assert track["category"] == "A7"  # FR24 H → rotorcraft
    assert track["alt_ft"] == 900
    assert track["gs_kt"] == 95.5
    assert track["source"] == "net"
    assert track["feed"] == "flightradar24"
    assert track["registration"] == "B-HXX"


def test_fetch_fr24_without_token_returns_empty():
    assert adsb_network.fetch_fr24("", 22.0, 114.0, 50) == []
