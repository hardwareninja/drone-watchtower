#!/usr/bin/env python3
"""Network ADS-B ingestion.

The local 1090 MHz receiver (an RTL-SDR feeding adsb1090) is the only
ADS-B source that reflects what this station actually heard. When no
RTL-SDR is attached that panel stays empty, even though the sky above
Hong Kong is busy.

This module pulls the public aggregator feeds and normalizes them into
the same track shape the local receiver produces. Two rules keep the
dashboard honest:

  * every track is tagged with where it came from ("1090" for the local
    receiver, "net" for an aggregator), and
  * the local receiver always wins a tie on ICAO hex.

So a network track can never be mistaken for something this box received
over the air.
"""
from __future__ import annotations

import datetime
import json
import math
import urllib.parse
import urllib.request

import textsan

USER_AGENT = "drone-watchtower"

# Public aggregator feeds. Both expose the same per-aircraft field names
# under different top-level keys ("ac" vs "aircraft").
DEFAULT_FEEDS = (
    {
        "name": "adsb.lol",
        "url": "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}",
        "key": "ac",
    },
    {
        "name": "adsb.fi",
        "url": "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{dist}",
        "key": "aircraft",
    },
)

FR24_LIVE_URL = (
    "https://fr24api.flightradar24.com/api/live/flight-positions/full"
)

EARTH_RADIUS_NM = 3440.065


def bounds_from_radius(lat: float, lon: float, dist_nm: float) -> str:
    """FR24 bounds string: north,south,west,east."""
    dlat = float(dist_nm) / 60.0
    cos_lat = max(0.2, math.cos(math.radians(lat)))
    dlon = float(dist_nm) / (60.0 * cos_lat)
    north = lat + dlat
    south = lat - dlat
    east = lon + dlon
    west = lon - dlon
    return f"{north},{south},{west},{east}"


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    h = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2)
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(h)))


def _number(value):
    """Coerce a feed value to float, or None. Feeds mix numbers and the
    string "ground" for altitude on non-ICAO aircraft."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(value) -> str:
    """Normalize a feed string. Feeds pad callsigns with spaces, and some
    report a callsign as whitespace only, which means "not reported".

    The value is also constrained to a markup-free charset: a feed row is
    attacker-influenced text (the callsign is broadcast by the aircraft),
    and it reaches the dashboard.
    """
    text = str(value or "").strip()
    return textsan.safe_label(text)


def fetch_feed(feed: dict, lat: float, lon: float, dist_nm: int,
               timeout: float = 8.0) -> list[dict]:
    """Fetch one aggregator feed and return normalized tracks."""
    if feed.get("kind") == "fr24":
        return fetch_fr24(feed.get("token") or "", lat, lon, dist_nm, timeout=timeout)
    url = feed["url"].format(lat=lat, lon=lon, dist=int(dist_nm))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode())
    rows = payload.get(feed.get("key") or "aircraft") or []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        track = normalize_track(row, feed["name"], lat, lon)
        if track:
            out.append(track)
    return out


def _fr24_rows(payload) -> list:
    """FR24 wraps positions under data / aircraft / or a bare list."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "aircraft", "flights", "result"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return rows
    return []


def normalize_fr24_track(row: dict, station_lat: float, station_lon: float) -> dict | None:
    """Map one FlightRadar24 live-position row onto the local track shape."""
    hex_code = textsan.safe_hex(row.get("hex") or row.get("icao_24") or row.get("icao24"))
    lat = _number(row.get("lat"))
    lon = _number(row.get("lon"))
    if not hex_code or lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    alt_ft = _number(row.get("alt") if row.get("alt") is not None else row.get("alt_baro"))
    gs_kt = _number(row.get("gspeed") if row.get("gspeed") is not None else row.get("gs"))
    track_deg = _number(row.get("track"))
    distance = haversine_nm(station_lat, station_lon, lat, lon)
    # FR24 category codes: H = helicopters, D = drones, …
    fr24_cat = row.get("category") or row.get("categories") or ""
    if isinstance(fr24_cat, list):
        fr24_cats = [str(c).upper() for c in fr24_cat]
        fr24_cat = fr24_cats[0] if fr24_cats else ""
    else:
        fr24_cat = _text(fr24_cat).upper()
        fr24_cats = [fr24_cat] if fr24_cat else []
    # Map FR24 heli/drone codes onto ADS-B-ish categories our classifier knows.
    if "H" in fr24_cats:
        category = "A7"
    elif "D" in fr24_cats:
        category = "A1"
    else:
        category = fr24_cat
    type_code = _text(row.get("type") or row.get("aircraft") or row.get("aircraft_type"))
    return {
        "hex": hex_code,
        "flight": _text(row.get("callsign") or row.get("flight")),
        "type": type_code,
        "category": category,
        "alt_ft": int(alt_ft) if alt_ft is not None else None,
        "gs_kt": gs_kt,
        "track": track_deg,
        "lat": lat,
        "lon": lon,
        "squawk": _text(row.get("squawk")),
        "seen": None,
        "messages": None,
        "distance_nm": round(distance, 2),
        "country": _text(row.get("orig_iata") or row.get("country")),
        "last_seen": _text(row.get("timestamp")),
        "source": "net",
        "feed": "flightradar24",
        "registration": _text(row.get("reg") or row.get("registration")),
        "description": type_code,
        "emergency": "",
        "fr24_category": ",".join(fr24_cats),
    }


def fetch_fr24(token: str, lat: float, lon: float, dist_nm: int,
               timeout: float = 8.0) -> list[dict]:
    """Pull FlightRadar24 live positions inside a radius of the station."""
    token = str(token or "").strip()
    if not token:
        return []
    bounds = bounds_from_radius(lat, lon, dist_nm)
    url = f"{FR24_LIVE_URL}?bounds={urllib.parse.quote(bounds)}"
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Version": "v1",
        "Authorization": f"Bearer {token}",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode())
    out = []
    for row in _fr24_rows(payload):
        if not isinstance(row, dict):
            continue
        track = normalize_fr24_track(row, lat, lon)
        if track:
            out.append(track)
    return out


def normalize_track(row: dict, feed_name: str,
                    station_lat: float, station_lon: float) -> dict | None:
    """Map one aggregator row onto the local receiver's track shape."""
    hex_code = textsan.safe_hex(row.get("hex"))
    lat = _number(row.get("lat"))
    lon = _number(row.get("lon"))
    if not hex_code or lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    alt_ft = _number(row.get("alt_baro"))
    gs_kt = _number(row.get("gs"))
    track_deg = _number(row.get("track"))
    # The feed's own "dst" is measured from its own station reference, so
    # it does not line up with where this station is. Compute our own.
    distance = haversine_nm(station_lat, station_lon, lat, lon)
    # The feed reports how many seconds ago it last heard the aircraft.
    # Turn that into a UTC timestamp so the dashboard can show a local
    # clock time the same way it does for every other event.
    seen = _number(row.get("seen"))
    last_seen = ""
    if seen is not None:
        stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seen)
        last_seen = stamp.isoformat(timespec="seconds")
    return {
        "hex": hex_code,
        "flight": _text(row.get("flight")),
        "type": _text(row.get("t")) or _text(row.get("desc")),
        "category": _text(row.get("category")),
        "alt_ft": int(alt_ft) if alt_ft is not None else None,
        "gs_kt": gs_kt,
        "track": track_deg,
        "lat": lat,
        "lon": lon,
        "squawk": _text(row.get("squawk")),
        "seen": seen,
        "messages": row.get("messages"),
        "distance_nm": round(distance, 2),
        "country": _text(row.get("country")),
        "last_seen": last_seen,
        "source": "net",
        "feed": feed_name,
        # Kept so the heuristic can tell a small UAS from a light aircraft.
        "registration": _text(row.get("r")),
        "description": _text(row.get("desc")),
        "emergency": _text(row.get("emergency")),
    }


def merge_tracks(local: list[dict], network: list[dict]) -> list[dict]:
    """Combine local and network tracks, local winning on ICAO hex."""
    merged: dict[str, dict] = {}
    for track in network:
        merged[track["hex"]] = track
    for track in local:
        merged[track["hex"]] = track
    return list(merged.values())


def collect(lat: float, lon: float, dist_nm: int = 100,
            feeds: list[dict] | None = None,
            timeout: float = 8.0) -> list[dict]:
    """Pull every configured feed and merge them by ICAO hex.

    One feed failing must not lose the others, so each is fetched
    independently and errors are swallowed per feed.
    """
    feeds = feeds if feeds is not None else list(DEFAULT_FEEDS)
    tracks: dict[str, dict] = {}
    for feed in feeds:
        try:
            for track in fetch_feed(feed, lat, lon, dist_nm, timeout=timeout):
                # First feed to report a track wins; later feeds fill gaps
                # for hex codes the earlier feed did not carry.
                tracks.setdefault(track["hex"], track)
        except Exception:
            continue
    return list(tracks.values())
