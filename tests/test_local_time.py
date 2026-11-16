"""Event times are stored in UTC and shown in the station's local time."""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

import app
import adsb_network

HKT = datetime.timezone(datetime.timedelta(hours=8))


def test_stored_timestamps_are_utc():
    """Every event stamp carries an explicit UTC offset, so a timezone
    change can never shift what is stored."""
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds")
    assert stamp.endswith("+00:00")


def test_to_local_iso_converts_utc_to_local():
    tz = app.local_timezone()
    local = app.to_local_iso("2026-11-14T01:13:46.304868+00:00", tz)
    parsed = datetime.datetime.fromisoformat(local)
    assert parsed.utcoffset() == datetime.timedelta(hours=8)
    assert parsed.hour == 9
    assert parsed.minute == 13
    assert parsed.second == 46


def test_to_local_iso_leaves_blank_and_broken_values_alone():
    assert app.to_local_iso("", None) == ""
    assert app.to_local_iso(None, None) is None
    assert app.to_local_iso("not-a-time", None) == "not-a-time"


def test_a_local_day_maps_to_its_utc_window():
    """A user picking 2026-11-14 in the date picker means their local day,
    which is a different span of UTC than the same date in UTC."""
    tz = app.local_timezone()
    start = app._local_bound_to_utc("2026-11-14", tz, "start")
    end = app._local_bound_to_utc("2026-11-14", tz, "end")
    # Local midnight is 16:00 UTC the day before.
    assert start.startswith("2026-11-13T16:00:00")
    assert end.startswith("2026-11-14T15:59:59")
    # And a stamp inside the local day falls between the two bounds.
    inside = "2026-11-14T01:13:46.304868+00:00"
    assert start <= inside <= end
    # A stamp from the previous local day does not.
    outside = "2026-11-13T14:11:34.328000+00:00"
    assert not (start <= outside <= end)


def test_a_full_datetime_bound_is_taken_as_given():
    tz = app.local_timezone()
    assert app._local_bound_to_utc("2026-11-13T10:00:00", tz, "start") == (
        "2026-11-13T02:00:00+00:00")


def test_network_tracks_carry_a_utc_last_seen():
    """The aggregator reports an age in seconds, not a timestamp. Turning
    it into a UTC stamp lets the dashboard show a local clock time."""
    row = {
        "hex": "780F7F", "lat": 10.1, "lon": 20.1,
        "alt_baro": 13800, "gs": 300.0, "seen": 12.5,
    }
    track = adsb_network.normalize_track(row, "adsb.lol", 10.0, 20.0)
    assert track["last_seen"].endswith("+00:00")
    stamp = datetime.datetime.fromisoformat(track["last_seen"])
    age = (datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds()
    assert 10 < age < 20
