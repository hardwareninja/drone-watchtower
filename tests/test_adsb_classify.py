"""The small-UAS heuristic flags candidates without crying wolf."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

import adsb_classify


def _airliner():
    return {
        "hex": "780F7F",
        "flight": "CSN8697",
        "registration": "B-32FV",
        "type": "A20N",
        "category": "A3",
        "alt_ft": 13800,
        "gs_kt": 342.8,
    }


def test_airliner_is_not_flagged():
    result = adsb_classify.classify(_airliner())
    assert result["uas_candidate"] is False
    assert result["uas_flags"] == []


def test_the_live_small_track_is_flagged():
    """This track was in the live feed: no ICAO identity, no callsign, no
    registration, unknown type, near the ground, slow."""
    track = {
        "hex": "~7b1004",
        "flight": "",
        "registration": "",
        "type": "",
        "category": "C1",
        "alt_ft": 0,
        "gs_kt": 33.5,
    }
    result = adsb_classify.classify(track)
    assert result["uas_candidate"] is True
    assert result["uas_score"] >= adsb_classify.UAS_SCORE_THRESHOLD
    assert "no ICAO identity" in result["uas_flags"]
    assert "no callsign" in result["uas_flags"]


def test_one_odd_attribute_alone_is_not_enough():
    """A helicopter with no callsign is still a helicopter."""
    track = _airliner()
    track["flight"] = ""
    result = adsb_classify.classify(track)
    assert result["uas_candidate"] is False
    assert result["uas_flags"] == []


def test_icao_identity_detection():
    assert adsb_classify.has_icao_identity("780F7F") is True
    assert adsb_classify.has_icao_identity("a1b2c3") is True
    # The tilde marks a non-ICAO reconstructed address.
    assert adsb_classify.has_icao_identity("~7b1004") is False
    assert adsb_classify.has_icao_identity("0x7b1004") is False
    assert adsb_classify.has_icao_identity("") is False
    assert adsb_classify.has_icao_identity("780F7") is False
    assert adsb_classify.has_icao_identity("ZZZZZZ") is False


def test_flags_are_dropped_below_the_threshold():
    """A borderline score carries no flags, so the UI stays quiet."""
    track = _airliner()
    track["flight"] = ""            # +22
    track["registration"] = ""      # +16
    result = adsb_classify.classify(track)
    assert result["uas_score"] == 38
    assert result["uas_score"] < adsb_classify.UAS_SCORE_THRESHOLD
    assert result["uas_flags"] == []
    assert result["uas_candidate"] is False


def test_a_heavy_airliner_category_is_not_light():
    """A3 means 75,000 kg or more. Treating it as a light-category signal
    would flag every Boeing 777 in the sky."""
    heavy = _airliner()
    heavy["category"] = "A3"
    light = _airliner()
    light["category"] = "A1"
    assert adsb_classify.classify(light)["uas_score"] == (
        adsb_classify.classify(heavy)["uas_score"]
        + adsb_classify.WEIGHTS["light_category"])


def test_uncategorised_emitter_category_is_not_light():
    """A5 is the "category not reported" value. Airliners routinely carry
    it, and a live A333 at 5 kt on the ground was being flagged because of
    it."""
    track = {
        "hex": "780A82",
        "flight": "        ",     # the feed pads callsigns with spaces
        "registration": "B-LBH",
        "type": "A333",
        "category": "A5",
        "alt_ft": None,           # the feed reports "ground"
        "gs_kt": 0.5,
    }
    result = adsb_classify.classify(track)
    assert result["uas_candidate"] is False


def test_a_padded_or_blank_callsign_counts_as_missing():
    """A callsign of spaces is "not reported", not a callsign."""
    blank = adsb_classify.classify({
        "hex": "A1B2C3", "flight": "        ", "registration": "",
        "type": "", "category": "A1", "alt_ft": 200, "gs_kt": 15,
    })
    missing = adsb_classify.classify({
        "hex": "A1B2C3", "flight": "", "registration": "",
        "type": "", "category": "A1", "alt_ft": 200, "gs_kt": 15,
    })
    assert blank["uas_score"] == missing["uas_score"]


def test_altitude_and_speed_signals():
    track = {
        "hex": "A1B2C3", "flight": "", "registration": "", "type": "",
        "category": "A1", "alt_ft": 400, "gs_kt": 20,
    }
    result = adsb_classify.classify(track)
    flags = " ".join(result["uas_flags"])
    assert "low altitude" in flags
    assert "near-hover" in flags
    assert result["uas_candidate"] is True


def test_missing_values_do_not_crash_the_scorer():
    result = adsb_classify.classify({"hex": "A1B2C3"})
    assert isinstance(result["uas_score"], int)
    assert isinstance(result["uas_flags"], list)
    assert result["helicopter"] is False


def test_emitter_category_a7_is_helicopter():
    track = {
        "hex": "CABCDE", "flight": "Heli1", "registration": "B-HXX",
        "type": "", "category": "A7", "alt_ft": 800, "gs_kt": 90,
    }
    result = adsb_classify.classify(track)
    assert result["helicopter"] is True
    assert any("A7" in f for f in result["helicopter_flags"])


def test_known_heli_type_is_helicopter():
    track = {
        "hex": "CABCDE", "flight": "GFS28", "registration": "",
        "type": "H145", "category": "A1", "alt_ft": 1200, "gs_kt": 110,
    }
    result = adsb_classify.classify(track)
    assert result["helicopter"] is True
    assert result["uas_candidate"] is False


def test_heli_label_keyword_is_helicopter():
    track = {
        "hex": "CABCDE", "flight": "", "type": "HELICOPTER",
        "category": "", "alt_ft": 500, "gs_kt": 60,
    }
    result = adsb_classify.classify(track)
    assert result["helicopter"] is True


def test_airliner_is_not_helicopter():
    result = adsb_classify.classify(_airliner())
    assert result["helicopter"] is False
    assert result["helicopter_flags"] == []
