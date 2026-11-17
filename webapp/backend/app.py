#!/usr/bin/env python3
"""Drone Watchtower — live detection webapp backend.

Runs on the Kali sensor box. Controls the luyii DroneID scanner as a
subprocess, tails its detections.jsonl, parses packets, and pushes events
over WebSocket to the frontend. Also serves the static frontend.

Usage: uvicorn app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import asyncio
import base64
import csv
import datetime
import io
import json
import os
import select
import secrets
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import usb_locate
import adsb_classify
import adsb_network
import aircraft_registry
import c5_rid
import textsan
import track_export
import zc_gate

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

try:
    import cv2
    HAVE_CV2 = True
except ImportError:
    HAVE_CV2 = False

from fastapi import Body, FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

try:
    import zmq
    HAVE_ZMQ = True
except ImportError:
    HAVE_ZMQ = False

REMOTEID_ZMQ = [
    "tcp://127.0.0.1:4226",  # ESP32-C5 bridge: Wi-Fi beacons and BLE Remote ID
    "tcp://127.0.0.1:4225",  # rid_beacon.py: Wi-Fi monitor RID decoder
    "tcp://127.0.0.1:4223",  # bkerler Wi-Fi receiver (macdb format)
    "tcp://127.0.0.1:4222",  # Sniffle BLE receiver (macdb format)
]
ADSB_URL = "http://127.0.0.1:8091/api/aircraft"  # ADSB1090, behind the map proxy on :8090
ADSB_NETWORK_FEEDS = list(adsb_network.DEFAULT_FEEDS)
ADSB_NETWORK_RADIUS_NM = 100
ADSB_NETWORK_INTERVAL_S = 20
ADSB_POLL_INTERVAL_S = 2

# The shortest possible DroneID payload. The classic O2/O3 frame is 91
# bytes; the newer O4 frames are 131, 163 and 166. Anything shorter is a
# decode that failed, not a frame -- see parse_line.
MIN_DRONEID_PAYLOAD_BYTES = 91

try:
    import elrs_demod as DEMOD          # packet-level ELRS LoRa demodulator
except Exception as _exc:
    DEMOD = None
    print(f"elrs demod unavailable: {_exc}", flush=True)

try:
    import mavlink_demod as MAV         # passive MAVLink (SiK 433/915 + UDP)
except Exception as _exc:
    MAV = None
    print(f"mavlink demod unavailable: {_exc}", flush=True)

ELRS_RATES = {5: "500 Hz", 6: "250 Hz", 7: "150/100 Hz", 8: "50 Hz"}

BASE = Path(__file__).resolve().parent
BUILD = str(max(int(p.stat().st_mtime) for p in [
    BASE.joinpath("app.py"),
    BASE.joinpath("elrs_demod.py"),
    BASE.joinpath("mavlink_demod.py"),
    BASE.parent.joinpath("frontend", "app.js"),
    BASE.parent.joinpath("frontend", "index.html"),
    BASE.parent.joinpath("frontend", "style.css"),
] if p.exists()))
FRONTEND = BASE.parent / "frontend"
RESULTS = Path("/home/drone/droneid-results")
CONFIG_PATH = BASE / "config.json"
CONFIG = json.loads(CONFIG_PATH.read_text())
_config_lock = threading.Lock()


def save_config() -> None:
    """Persist CONFIG to disk. Keeps secrets like FR24 token server-side only."""
    with _config_lock:
        CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2) + "\n")


def reload_config() -> None:
    with _config_lock:
        CONFIG.clear()
        CONFIG.update(json.loads(CONFIG_PATH.read_text()))

SCANNER = "/opt/droneid-repo/src/droneid_hackrf_scanner.py"
TURBO = "/opt/droneid-tools/remove_turbo_soft"
LIBHACKRF = "/usr/lib/aarch64-linux-gnu/libhackrf.so.0"
# Pro stays on DroneID. One sweeps ELRS and TBS. One tuner each.
#
# The serials identify the operator's own hardware, so they are not
# baked into the source: they come from the environment or from
# config.json ("hackrf_serials": {"droneid": ..., "hop": ...}). A fresh
# clone has none and the box reports the radios as unassigned until
# they are set.
HACKRF_DRONEID = os.environ.get(
    "HACKRF_DRONEID", str((CONFIG.get("hackrf_serials") or {}).get("droneid") or ""))
HACKRF_HOP = os.environ.get(
    "HACKRF_HOP", str((CONFIG.get("hackrf_serials") or {}).get("hop") or ""))
# Demod modules read HACKRF_HOP at import time; push the resolved serial so
# config.json-only stations still open the One, not whichever radio is first.
if DEMOD is not None and HACKRF_HOP:
    DEMOD.ONE = HACKRF_HOP
if MAV is not None and HACKRF_HOP:
    MAV.ONE = HACKRF_HOP

app = FastAPI(title="Drone Watchtower")


# ---------------------------------------------------------------------------
# API token
# ---------------------------------------------------------------------------
# The dashboard is reachable from the operator's machine over the LAN, so
# it is a control plane on an untrusted network: without a token any
# neighbour can read the detection history (a geolocated log of every
# drone and crewed aircraft seen near the station) or stop the scan.
#
# The token is optional — when it is not configured the API stays open,
# which is only reasonable on a loopback bind or a trusted segment. Set
# WATCHTOWER_TOKEN in the environment, or "api_token" in config.json.
# ---------------------------------------------------------------------------
TOKEN_ENV = "WATCHTOWER_TOKEN"
TOKEN_COOKIE = "watchtower_token"

# Paths that stay reachable without a token: the page itself (so the
# browser can ask for a token), the static assets, and the token check.
PUBLIC_PATHS = {"/api/auth/status", "/api/auth/login", "/favicon.ico"}


def api_token() -> str:
    """The configured API token, or "" when the API is unauthenticated."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        token = str(CONFIG.get("api_token") or "").strip()
    return token


def presented_token(headers, cookies=None, query=None) -> str:
    """Token from a header, a bearer value, or the session cookie.

    A query-string token is accepted only as a last resort. Callers that
    can set headers must not put the token in the URL: the access log
    records the URL.
    """
    header = ""
    auth = ""
    if headers is not None:
        header = headers.get("x-watchtower-token") or ""
        auth = headers.get("authorization") or ""
    if header:
        return header
    if auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
        if bearer:
            return bearer
    if cookies is not None:
        cookie = cookies.get(TOKEN_COOKIE) or ""
        if cookie:
            return cookie
    if query is not None:
        return query.get("token") or ""
    return ""


def token_ok(request) -> bool:
    """True when the request carries the configured token."""
    expected = api_token()
    if not expected:
        return True
    supplied = presented_token(request.headers, request.cookies, request.query_params)
    return bool(supplied) and secrets.compare_digest(supplied, expected)


@app.middleware("http")
async def require_token(request, call_next):
    """Reject API calls that do not carry the configured token.

    Only ``/api/*`` is guarded. The static page stays reachable so the
    browser can ask for the token and so a wrong URL gives a clear
    answer rather than a blank page.
    """
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_PATHS:
        if not token_ok(request):
            return JSONResponse({"ok": False, "error": "token required"}, status_code=401)
    return await call_next(request)


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    """Whether a token is required, and whether this request carries one."""
    return {
        "required": bool(api_token()),
        "ok": token_ok(request),
    }


@app.post("/api/auth/login")
def auth_login(payload: dict = Body(default_factory=dict)) -> Response:
    """Exchange the token for a session cookie."""
    expected = api_token()
    if not expected:
        return JSONResponse({"ok": True, "required": False})
    supplied = str((payload or {}).get("token") or "")
    if not supplied or not secrets.compare_digest(supplied, expected):
        return JSONResponse({"ok": False, "error": "invalid token"}, status_code=401)
    response = JSONResponse({"ok": True, "required": True})
    # Not HttpOnly: the frontend reads it to send on the websocket and
    # to keep the page working after a reload. It is the same secret the
    # operator already types, on a LAN-only box.
    response.set_cookie(
        TOKEN_COOKIE, expected, max_age=60 * 60 * 24 * 30,
        samesite="lax", path="/")
    return response

def _armed_path() -> Path:
    return Path(os.environ.get("WATCHTOWER_DB", "/opt/watchtower-web/data/history.db")).parent / "armed"


def load_armed() -> bool:
    """Last Start/Stop choice — survives deploy restarts so refresh isn't blank."""
    try:
        return _armed_path().read_text().strip() != "0"
    except OSError:
        return True


def save_armed(armed: bool) -> None:
    try:
        path = _armed_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1" if armed else "0")
    except OSError as exc:
        print(f"armed persist failed: {exc}", flush=True)


state = {
    "running": False,
    "armed": load_armed(),  # master switch for DroneID, ELRS, and TBS
    "mode": None,          # "live" | None
    "proc": None,
    "sweep": None,
    "run_dir": None,
    "events": 0,
    "started": None,
}
loop: asyncio.AbstractEventLoop | None = None
clients: set[WebSocket] = set()
lock = threading.Lock()


# O3+ / newer air units often ZC-lock and pass transport CRCs while the
# turbo decode only recovers a 3-byte stub (length byte zero → truncated).
# That is RF presence, not telemetry — publish as ENCR so the dashboard
# shows the aircraft without inventing a serial or position.
_ENCR_STUB_CLASSES = {
    "decode_truncated_too_short",
    "crc_valid_nonclassic_or_encrypted",
}
_encr_stub_at: dict[str, float] = {}


def parse_line(line: str) -> dict | None:
    try:
        j = json.loads(line)
        p = bytes.fromhex(j["payload_hex"])
        classification = j.get("payload_classification")
        crc_ok = bool(j.get("crc24a_ok") and j.get("dji_crc16_ok"))
        # A decoded DroneID burst is at least the 91-byte classic O2 frame.
        # A shorter payload is the head of a burst that failed to decode:
        # the transport length byte was zero, so the "payload" is three
        # bytes. The DJI CRC-16 over those three bytes can be zero by
        # accident, and the CRC-24 covers the transport rather than the
        # payload, so both checks pass and the stub looks like a valid
        # detection. Every repeat of the same failed burst produces the
        # same three bytes, so it passes every time.
        if len(p) < MIN_DRONEID_PAYLOAD_BYTES:
            if not crc_ok or classification not in _ENCR_STUB_CLASSES:
                return None
            sha = (j.get("payload_sha256") or "")[:12] or p.hex()
            # Throttle identical stubs — O3+ can emit dozens per minute.
            now = time.time()
            last = _encr_stub_at.get(sha) or 0.0
            if now - last < 2.0:
                return None
            _encr_stub_at[sha] = now
            if len(_encr_stub_at) > 256:
                for k, t in list(_encr_stub_at.items()):
                    if now - t > 60:
                        _encr_stub_at.pop(k, None)
            return {
                "time": j["event_time_utc"],
                "freq": j["frequency_mhz"],
                "marker": "ENCR",
                "session": sha[:8],
                "rssi": j.get("dwell_average_power_dbfs"),
                "crc": True,
                "sha": sha,
                "mode": state["mode"],
                "pclass": textsan.safe_label(classification),
            }
        marker = textsan.safe_label(p[2:6].decode("ascii", "replace")) or "DJI"
        evt = {
            "time": j["event_time_utc"],
            "freq": j["frequency_mhz"],
            "marker": marker,
            "session": p[6:10].hex(),
            "rssi": j.get("dwell_average_power_dbfs"),
            "crc": crc_ok,
            "sha": (j.get("payload_sha256") or "")[:12],
            "mode": state["mode"],
            "pclass": textsan.safe_label(classification),
        }
        drone = j.get("drone_id")
        if isinstance(drone, dict):
            # classic O2/O3 carries plaintext telemetry: use the real serial
            # as the session so it can line up with a Remote ID later.
            # The serial is chosen by the aircraft, so it is constrained
            # before it becomes the session id.
            serial = textsan.safe_label(drone.get("serial"))
            if classification == "classic_o2_plaintext" and serial:
                evt["session"] = serial
                evt["marker"] = "O2PT"
            lat, lon = drone.get("latitude"), drone.get("longitude")
            if lat and lon:  # 0,0 means invalid per the spec
                evt["lat"] = lat
                evt["lon"] = lon
                evt["alt"] = drone.get("altitude")
                evt["yaw"] = drone.get("yaw_degrees")
                if drone.get("pilot_latitude") and drone.get("pilot_longitude"):
                    evt["op_lat"] = drone["pilot_latitude"]
                    evt["op_lon"] = drone["pilot_longitude"]
                if drone.get("home_latitude") and drone.get("home_longitude"):
                    evt["home_lat"] = drone["home_latitude"]
                    evt["home_lon"] = drone["home_longitude"]
        return evt
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


DB = Path(os.environ.get("WATCHTOWER_DB", "/opt/watchtower-web/data/history.db"))


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


BEATS: dict[str, float] = {}


def beat(name: str) -> None:
    BEATS[name] = time.time()


def retention_days() -> int:
    try:
        return max(1, int(CONFIG.get("retention_days", 90)))
    except (TypeError, ValueError):
        return 90


def retention_loop() -> None:
    """Prune events older than the configured window, whole days at a time."""
    while True:
        beat("retention")
        try:
            cutoff = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=retention_days())
            ).isoformat(timespec="seconds")
            with db_conn() as c:
                cur = c.execute("DELETE FROM events WHERE time < ?", (cutoff,))
                if cur.rowcount:
                    print(f"retention: pruned {cur.rowcount} events before {cutoff}",
                          flush=True)
        except sqlite3.Error as e:
            print(f"retention failed: {e}", flush=True)
        for _ in range(60):  # minute slices so /api/health sees a live thread
            time.sleep(60)
            beat("retention")


EVENT_COLS = (
    "time", "session", "source", "marker", "freq", "rssi",
    "lat", "lon", "alt", "crc",
    "op_lat", "op_lon", "home_lat", "home_lon", "yaw", "pclass",
    "station_lat", "station_lon",
)

EXTRA_COLS = (("op_lat", "REAL"), ("op_lon", "REAL"),
              ("home_lat", "REAL"), ("home_lon", "REAL"),
              ("yaw", "REAL"), ("pclass", "TEXT"),
              ("station_lat", "REAL"), ("station_lon", "REAL"))


def insert_event(evt: dict) -> None:
    clean = textsan.sanitize_text_fields(evt)
    row = {k: clean.get(k) for k in EVENT_COLS}
    # a position-less event still needs to know where the station was, so the
    # history map can place it correctly after the station moves on GPS
    if not row.get("lat") and not row.get("lon"):
        here = station_dict()
        if here.get("lat") is not None and here.get("lon") is not None:
            row["station_lat"] = here["lat"]
            row["station_lon"] = here["lon"]
    try:
        with db_conn() as c:
            c.execute(
                f"INSERT INTO events ({','.join(EVENT_COLS)}) VALUES "
                f"({','.join('?' * len(EVENT_COLS))})",
                tuple(row[k] for k in EVENT_COLS),
            )
    except sqlite3.Error as e:  # storage must never break detection
        print(f"history insert failed: {e}", flush=True)


def init_db() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    with db_conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS events (
                time TEXT, session TEXT, source TEXT, marker TEXT,
                freq REAL, rssi REAL, lat REAL, lon REAL, alt REAL, crc INTEGER)"""
        )
        for col, typ in EXTRA_COLS:
            try:
                c.execute(f"ALTER TABLE events ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # column already exists
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(session)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(time)")
    marker = DB.parent / ".imported"
    if marker.exists():
        return
    imported = 0
    for path in RESULTS.glob("*/detections.jsonl"):  # one-time archive import
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            evt = parse_line(line)
            if evt and evt.get("crc"):
                evt["source"] = "droneid"
                insert_event(evt)
                imported += 1
    marker.write_text(f"{imported} imported at {time.ctime()}")
    print(f"history: imported {imported} archived events", flush=True)


def broadcast(msg: dict) -> None:
    if loop is None or not clients:
        return
    data = json.dumps(msg)
    for ws in list(clients):
        asyncio.run_coroutine_threadsafe(ws.send_text(data), loop)


_rid_seen: dict[tuple[str, str], float] = {}


def _rid_dup(evt: dict) -> bool:
    """The same serial in the same second from two receivers is one beacon."""
    key = (str(evt.get("session") or ""), str(evt.get("time") or "")[:19])
    now = time.time()
    if key in _rid_seen:
        return True
    _rid_seen[key] = now
    if len(_rid_seen) > 4096:
        for k in [k for k, t in _rid_seen.items() if t < now - 10]:
            _rid_seen.pop(k, None)
    return False


def publish_event(evt: dict) -> None:
    if not isinstance(evt, dict):
        return
    evt = textsan.sanitize_text_fields(evt)
    if evt.get("source") == "remoteid" and _rid_dup(evt):
        return
    with lock:
        state["events"] += 1
    # One lookup per new serial against the local aircraft list. O2 classic
    # uses the real serial as session, the same key as Remote ID.
    if evt.get("session"):
        model = aircraft_registry.registry.resolve(
            evt.get("session"), CONFIG.get("aircraft") or {})
        if model:
            evt["model"] = model
    insert_event(evt)
    typ = "remoteid" if evt.get("source") == "remoteid" else "detection"
    broadcast({"type": typ, "event": evt})


def public_gps() -> dict:
    gps = state.get("gps") or {}
    return {
        "attached": bool(gps.get("attached")),
        "fix": bool(gps.get("fix")),
        "sats": gps.get("sats") or 0,
        "seen": gps.get("seen") or 0,
        "lat": gps.get("lat"),
        "lon": gps.get("lon"),
    }


def _finite_lat_lon(lat, lon) -> tuple[float, float] | None:
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return None
    if lat_f == 0 and lon_f == 0:
        return None
    return round(lat_f, 6), round(lon_f, 6)


def parse_network_geo(payload: dict) -> dict | None:
    """Pull a position out of a public IP-geolocation response."""
    if not isinstance(payload, dict) or payload.get("success") is False:
        return None
    lat, lon = payload.get("latitude", payload.get("lat")), payload.get("longitude", payload.get("lon"))
    loc = payload.get("loc")
    if (lat is None or lon is None) and isinstance(loc, str) and "," in loc:
        lat, lon = loc.split(",", 1)
    parsed = _finite_lat_lon(lat, lon)
    if parsed is None:
        return None
    return {"lat": parsed[0], "lon": parsed[1]}


def lookup_network_geo() -> dict | None:
    """Where this machine appears on the public network. No API key."""
    for url in (
        "https://ipwho.is/",
        "https://ipinfo.io/json",
        "https://ipapi.co/json/",
    ):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "drone-watchtower"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                payload = json.loads(resp.read().decode())
        except (OSError, json.JSONDecodeError, ValueError, UnicodeError):
            continue
        geo = parse_network_geo(payload)
        if geo:
            return geo
    return None


def station_dict() -> dict:
    """GPS fix wins. Until the receiver has one, use this machine's network location."""
    raw = CONFIG.get("station") or {}
    station = {"name": raw.get("name") or "Watchtower"}
    gps = state.get("gps") or {}
    parsed = _finite_lat_lon(gps.get("lat"), gps.get("lon"))
    if parsed is not None:
        station["lat"], station["lon"] = parsed
        station["source"] = "gps"
        return station
    net = state.get("network_geo") or {}
    parsed = _finite_lat_lon(net.get("lat"), net.get("lon"))
    if parsed is not None:
        station["lat"], station["lon"] = parsed
        station["source"] = "network"
        return station
    station["lat"] = None
    station["lon"] = None
    station["source"] = "none"
    return station


def hackrf_configured() -> dict:
    """Which HackRF roles have a serial assigned.

    The serials identify the operator's own hardware, so they are not in
    the repository. A role with no serial cannot be addressed: libhackrf
    is given an empty device string and opens nothing, so the loop must
    not pretend the radio is there.
    """
    return {
        "droneid": bool(HACKRF_DRONEID),
        "hop": bool(HACKRF_HOP),
    }


def readiness() -> dict:
    """Boot/deploy self-check: missing demods and radios surface here, not as mystery silence."""
    problems: list[str] = []
    for name, mod in (("elrs_demod", DEMOD), ("mavlink_demod", MAV)):
        if mod is None:
            problems.append(f"{name} import failed — packet path disabled")
        path = BASE / f"{name}.py"
        if not path.exists():
            problems.append(f"{name}.py missing under {BASE}")
    configured = hackrf_configured()
    present = {d["id"]: d for d in dongle_status()}
    if not configured["droneid"]:
        problems.append(
            "HackRF Pro (DroneID) serial not set — set hackrf_serials.droneid "
            "in config.json or HACKRF_DRONEID in the environment")
    elif not present.get("pro", {}).get("attached"):
        problems.append("HackRF Pro (DroneID) not attached")
    if not configured["hop"]:
        problems.append(
            "HackRF One (ELRS/hop) serial not set — set hackrf_serials.hop "
            "in config.json or HACKRF_HOP in the environment")
    elif not present.get("one", {}).get("attached"):
        problems.append("HackRF One (ELRS/hop) not attached")
    if not present.get("c5", {}).get("attached"):
        problems.append("ESP32-C5 (RemoteID) not attached")
    return {"ok": not problems, "problems": problems}


def c5_band_report(dongles: list[dict] | None = None) -> dict:
    """2.4 / 5 / BLE dots for the ESP32-C5. HackRF 5.8 is not part of this."""
    if dongles is None:
        dongles = dongle_status()
    attached = any(d.get("id") == "c5" and d.get("attached") for d in dongles)
    return c5_rid.band_status(
        attached, state.get("c5_health"), time.time(),
        wifi5_configured=bool(CONFIG.get("c5_wifi5")),
    )


def status_dict() -> dict:
    started = state.get("started")
    ready = readiness()
    dongles = dongle_status()
    return {
        "running": state["running"],
        "armed": state.get("armed", True),
        "mode": state["mode"],
        "events": state["events"],
        "started": started,
        "uptime_s": round(time.time() - started, 1) if started else None,
        "phase": state.get("phase"),
        "phase_droneid": state.get("phase_droneid"),
        "phase_hop": state.get("phase_hop"),  # HackRF One: ELRS | TBS | MAVLink | VTX
        "phase_hop_s": (
            round(time.time() - state["phase_hop_started"], 1)
            if state.get("phase_hop_started") and state.get("phase_hop") else None
        ),
        "station": station_dict(),
        "gps": public_gps(),
        "dongles": dongles,
        "internet": state.get("internet"),
        "adsb_error": state.get("adsb_error"),
        "adsb_network_error": state.get("adsb_network_error"),
        "adsb_local_count": len(state.get("adsb_local") or []),
        "adsb_network_count": len(state.get("adsb_network") or []),
        "adsb_provider": state.get("adsb_provider") or (
            "flightradar24" if str(CONFIG.get("fr24_api_token") or "").strip() else "adsb.lol"
        ),
        "replay_running": bool(state.get("replay_proc")),
        "replay_published": state.get("replay_published") or 0,
        "zc_thresholds": dict(zip(
            ("root600", "root147"), zc_gate.thresholds(CONFIG))),
        "build": BUILD,
        "ready": ready["ok"],
        "problems": ready["problems"],
        "demods": {"elrs": DEMOD is not None, "mavlink": MAV is not None},
        "c5_bands": c5_band_report(dongles),
    }


DONGLE_SPECS = (
    {"id": "pro", "label": "DroneID", "role": "DroneID", "serial": HACKRF_DRONEID},
    {"id": "c5", "label": "RemoteID", "role": "RemoteID", "vendor": "303a", "product": "1001"},
    {"id": "one", "label": "ELRS", "role": "ELRS", "serial": HACKRF_HOP},
    {"id": "rtl", "label": "ADS-B", "role": "ADS-B", "vendor": "0bda", "product": "2838"},
    {"id": "gps", "label": "GPS", "role": "u-blox 7 · GPS", "vendor": "1546", "product": "01a7"},
)


def usb_devices() -> list[dict]:
    found = []
    root = Path("/sys/bus/usb/devices")
    if not root.exists():
        return found
    for dev in root.iterdir():
        vendor_path = dev / "idVendor"
        if not vendor_path.exists():
            continue
        try:
            serial_path = dev / "serial"
            found.append({
                "vendor": vendor_path.read_text().strip().lower(),
                "product": (dev / "idProduct").read_text().strip().lower(),
                "serial": serial_path.read_text().strip().lower() if serial_path.exists() else "",
            })
        except OSError:
            continue
    return found


def dongle_specs() -> list[dict]:
    """The dongle list with roles overridden by config.json's hackrf_roles."""
    specs = [dict(s) for s in DONGLE_SPECS]
    roles = CONFIG.get("hackrf_roles")
    if isinstance(roles, dict):
        for s in specs:
            serial = s.get("serial")
            if serial and serial in roles:
                s["role"] = roles[serial]
    return specs


def dongle_status() -> list[dict]:
    present = usb_devices()
    gps = state.get("gps") or {}
    c5_tty = usb_acm_port("303a", "1001")
    out = []
    for spec in dongle_specs():
        attached = False
        for dev in present:
            if spec.get("serial") and dev["serial"] == spec["serial"].lower():
                attached = True
            elif spec.get("vendor") and dev["vendor"] == spec["vendor"] and dev["product"] == spec.get("product"):
                attached = True
        live = attached
        if spec["id"] == "rtl":
            live = attached and bool(state.get("adsb_ok"))
        elif spec["id"] == "c5":
            live = attached and bool(c5_tty)
        elif spec["id"] == "gps":
            live = attached and (bool(gps.get("fix")) or (gps.get("sats") or 0) > 0)
        out.append({
            "id": spec["id"], "label": spec["label"], "role": spec["role"],
            "attached": attached, "live": live,
            # A HackRF with no serial assigned is a configuration gap, not
            # a missing device: the dashboard shows it differently.
            "configured": bool(spec.get("serial")) if spec["id"] in ("pro", "one") else True,
        })
    return out


def usb_acm_port(vendor: str, product: str) -> str | None:
    """Serial node for a USB vendor:product, on any bus and any tty number."""
    return usb_locate.tty_for(vendor, product)


def gps_port() -> str | None:
    """u-blox 7 USB receiver, 1546:01a7, wherever it enumerates."""
    return usb_acm_port("1546", "01a7")


def nmea_deg(raw: str, hemi: str) -> float | None:
    if not raw or "." not in raw:
        return None
    whole, frac = raw.split(".", 1)
    if len(whole) < 3:
        return None
    try:
        minutes = float(whole[-2:] + "." + frac)
        degrees = float(whole[:-2])
    except ValueError:
        return None
    value = degrees + minutes / 60.0
    if hemi in ("S", "W"):
        value = -value
    return value


def apply_gsv(line: str) -> bool:
    parts = line.split("*", 1)[0].split(",")
    if len(parts) < 4 or not parts[3]:
        return False
    try:
        seen = int(parts[3])
    except ValueError:
        return False
    gps = state.setdefault("gps", {"attached": True, "fix": False, "sats": 0})
    if gps.get("seen") == seen:
        return False
    gps["seen"] = seen
    return True


def apply_gga(line: str) -> bool:
    parts = line.split("*", 1)[0].split(",")
    if len(parts) < 8:
        return False
    try:
        quality = int(parts[6] or "0")
        sats = int(parts[7] or "0")
    except ValueError:
        return False
    gps = state.setdefault("gps", {"attached": True, "fix": False, "sats": 0})
    gps["attached"] = True
    gps["sats"] = sats
    gps["fix"] = quality > 0
    if quality > 0:
        lat = nmea_deg(parts[2], parts[3])
        lon = nmea_deg(parts[4], parts[5])
        if lat is None or lon is None:
            return False
        moved = gps.get("lat") is None or abs(gps["lat"] - lat) > 1e-5 or abs(gps["lon"] - lon) > 1e-5
        gps["lat"] = round(lat, 6)
        gps["lon"] = round(lon, 6)
        return moved or gps.get("_quality") != quality
    gps["_quality"] = quality
    return gps.get("_sats_sent") != sats


def gps_loop() -> None:
    """Read the u-blox 7 and move the station onto a real fix."""
    fd = None
    port = None
    buf = b""
    last_sent = None
    while True:
        beat("gps")
        path = gps_port()
        if path != port:
            if fd is not None:
                os.close(fd)
                fd = None
            port = path
            buf = b""
            if not path:
                state["gps"] = {"attached": False, "fix": False, "sats": 0,
                                "lat": (state.get("gps") or {}).get("lat"),
                                "lon": (state.get("gps") or {}).get("lon")}
                broadcast({"type": "status", "status": status_dict()})
                time.sleep(2)
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
            except OSError as exc:
                print(f"gps open failed: {exc}", flush=True)
                port = None
                time.sleep(2)
                continue
            state["gps"] = {"attached": True, "fix": False, "sats": 0,
                            "lat": (state.get("gps") or {}).get("lat"),
                            "lon": (state.get("gps") or {}).get("lon")}
            broadcast({"type": "status", "status": status_dict()})
        if fd is None:
            time.sleep(1)
            continue
        ready, _, _ = select.select([fd], [], [], 1.0)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            os.close(fd)
            fd = None
            port = None
            continue
        if not chunk:
            os.close(fd)
            fd = None
            port = None
            continue
        buf = (buf + chunk)[-8192:]
        changed = False
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            text = raw.decode("ascii", "replace").strip()
            if "GGA" in text[:6]:
                changed = apply_gga(text) or changed
            elif "GSV" in text[:6] and text.split(",")[2] == "1":
                changed = apply_gsv(text) or changed
        if not changed:
            continue
        gps = state.get("gps") or {}
        snapshot = (gps.get("fix"), gps.get("sats"), gps.get("seen"), gps.get("lat"), gps.get("lon"))
        if snapshot == last_sent:
            continue
        last_sent = snapshot
        gps["_sats_sent"] = gps.get("sats")
        broadcast({"type": "status", "status": status_dict()})


def internet_up() -> bool:
    """True when this sensor can open a connection to the public internet."""
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=2):
            return True
    except OSError:
        return False


def dongle_loop() -> None:
    """Tell the page when a radio is plugged in or unplugged, and when the link drops."""
    last = None
    last_net = None
    last_err = None
    next_net = 0.0
    while True:
        beat("dongles")
        current = dongle_status()
        now = time.time()
        if now >= next_net:
            state["internet"] = internet_up()
            next_net = now + 5
        net = state.get("internet")
        err = state.get("adsb_error")
        if current != last or net != last_net or err != last_err:
            last = current
            last_net = net
            last_err = err
            broadcast({"type": "status", "status": status_dict()})
        time.sleep(2)


def kill_proc(proc) -> None:
    if proc and proc.poll() is None:
        proc.send_signal(2)  # SIGINT — scanner clean shutdown
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def refresh_phase() -> None:
    parts = [p for p in (state.get("phase_droneid"), state.get("phase_hop")) if p]
    state["phase"] = " + ".join(parts) if parts else None
    hop = state.get("phase_hop")
    if hop != state.get("_phase_hop_seen"):
        state["_phase_hop_seen"] = hop
        state["phase_hop_started"] = time.time() if hop else None
    broadcast({"type": "status", "status": status_dict()})


def stop_everything() -> None:
    state["armed"] = False
    save_armed(False)
    kill_proc(state.get("proc"))
    kill_proc(state.get("sweep"))
    state.update(running=False, mode=None, proc=None, sweep=None,
                 phase_droneid=None, phase_hop=None)
    refresh_phase()
    broadcast({"type": "elrs", "link": {"present": False}})
    broadcast({"type": "tbs", "link": {"present": False}})


def tail_loop() -> None:
    """Poll the active run's detections.jsonl for new lines."""
    seen = 0
    while state["running"] and state["mode"] == "live":
        path = state["run_dir"] / "detections.jsonl" if state["run_dir"] else None
        if path and path.exists():
            lines = path.read_text().splitlines()
            for line in lines[seen:]:
                evt = parse_line(line)
                if evt and evt["crc"]:
                    evt["source"] = "droneid"
                    publish_event(evt)
            seen = len(lines)
        time.sleep(0.4)


@app.post("/api/scan/start")
def scan_start() -> dict:
    """Resume DroneID on the Pro and ELRS/TBS on the One."""
    state["armed"] = True
    save_armed(True)
    if not state.get("started"):
        state["started"] = time.time()
    broadcast({"type": "status", "status": status_dict()})
    return {"ok": True}


@app.post("/api/scan/stop")
def scan_stop() -> dict:
    stop_everything()
    broadcast({"type": "status", "status": status_dict()})
    return {"ok": True}


@app.get("/api/config/adsb")
def get_adsb_config() -> dict:
    """Public ADS-B network settings (FR24 token is never returned in full)."""
    return adsb_config_public()


@app.post("/api/config/adsb")
async def set_adsb_config(body: dict) -> dict:
    """Save an optional FlightRadar24 API token.

    Non-empty token → network ADS-B uses FR24 only.
    Empty token → fall back to adsb.lol / adsb.fi.
    """
    if "fr24_api_token" in body:
        token = str(body.get("fr24_api_token") or "").strip()
        CONFIG["fr24_api_token"] = token
        save_config()
        state["adsb_provider"] = adsb_provider()
        # Force a fresh network pull on the next loop tick.
        state["adsb_network"] = []
        state["adsb_network_error"] = None
        broadcast({"type": "status", "status": status_dict()})
    return adsb_config_public()


REPLAY_SCAN = Path("/opt/droneid-repo/src/droneid_hackrf_scanner.py")
REPLAY_CAP = Path("/home/drone/droneid-results/replay")
ELRS_CAPTURES = Path("/home/drone/elrs-captures")


def replay_stop() -> None:
    """Stop a running replay, if any."""
    proc = state.get("replay_proc")
    if proc and proc.poll() is None:
        proc.send_signal(2)  # SIGINT — scanner clean shutdown
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    state["replay_proc"] = None
    state["replay_run"] = None


def replay_worker(path: Path, freq_hz: int, root600: float, root147: float) -> None:
    """Run the scanner in replay mode and publish whatever it decodes.

    This is the offline path: a recorded IQ file goes through exactly the
    same detector and decoder as live air, so a threshold change can be
    judged against real captures without any radio attached.
    """
    beat("replay")
    run_dir = REPLAY_CAP / time.strftime("replay-%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "python3", str(REPLAY_SCAN),
        "--replay", str(path),
        "--replay-freq", str(freq_hz),
        "--turbo-binary", TURBO,
        "--root600-threshold", str(root600),
        "--root147-threshold", str(root147),
        "--output", str(run_dir),
    ]
    state["replay_run"] = run_dir
    log = open(run_dir / "scanner.log", "w")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    state["replay_proc"] = proc
    detections = run_dir / "detections.jsonl"
    seen = 0
    published = 0
    try:
        while proc.poll() is None:
            beat("replay")
            if detections.exists():
                lines = detections.read_text().splitlines()
                for line in lines[seen:]:
                    evt = parse_line(line)
                    if evt and evt.get("crc"):
                        evt["source"] = "droneid"
                        evt["replay"] = True
                        publish_event(evt)
                        published += 1
                seen = len(lines)
            time.sleep(0.4)
        # The scanner writes its last lines on the way out.
        if detections.exists():
            lines = detections.read_text().splitlines()
            for line in lines[seen:]:
                evt = parse_line(line)
                if evt and evt.get("crc"):
                    evt["source"] = "droneid"
                    evt["replay"] = True
                    publish_event(evt)
                    published += 1
    finally:
        log.close()
        if state.get("replay_proc") is proc:
            state["replay_proc"] = None
        state["replay_published"] = published
        state["replay_run"] = None
        beat("replay")
        broadcast({"type": "status", "status": status_dict()})


REPLAY_DENY_NAMES = frozenset({
    ".ssh", ".aws", ".gnupg", ".config", ".hermes", ".kube", ".docker",
    ".netrc", ".env", "auth.json", "config.json", "id_rsa", "id_ed25519",
    "credentials", "known_hosts", ".git-credentials", "shadow", "passwd",
})

# A replay source is a recorded IQ capture, so it has a capture
# extension. Requiring one keeps the home directory in the allowed roots
# (the documented workflow drops a capture there) without turning the
# endpoint into a reader for everything else in the home directory.
REPLAY_EXTENSIONS = frozenset({
    ".cs8", ".cf32", ".cfile", ".raw", ".iq", ".bin", ".npy",
})


def replay_allowed_roots() -> list[Path]:
    """Directories a replay source may live in.

    The replay endpoint takes a path from the client, so it must be
    confined: without this, any caller can point the scanner at an
    arbitrary file on the box and use the endpoint as a file-existence
    oracle.

    The capture roots are where the receivers actually write: the DroneID
    results tree, the replay inbox, and the ELRS capture directory. The
    station user's home is included because the documented workflow drops
    a capture file there; sensitive names inside it are denied below.
    """
    roots = [RESULTS, REPLAY_CAP, ELRS_CAPTURES, Path.home()]
    configured = CONFIG.get("replay_roots")
    if isinstance(configured, list):
        roots += [Path(p).expanduser() for p in configured if isinstance(p, str) and p]
    return roots


def _replay_denied(source: Path) -> bool:
    """True when *source* touches a sensitive path or is not a capture."""
    parts = source.parts
    for part in parts:
        if part.lower() in REPLAY_DENY_NAMES:
            return True
    name = source.name.lower()
    if name.endswith((".pem", ".key", ".pfx", ".p12")):
        return True
    if name.startswith(".") and name not in (".cs8",):
        return True
    if source.suffix.lower() not in REPLAY_EXTENSIONS:
        return True
    return False


def resolve_replay_source(raw: str) -> tuple[Path | None, str]:
    """Resolve *raw* to a readable capture file inside an allowed root.

    Returns ``(path, "")`` on success or ``(None, reason)`` on failure.
    The reason never echoes a resolved filesystem path back to the caller.
    """
    if not raw or not raw.strip():
        return None, "path is required"
    try:
        source = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        return None, "path could not be resolved"
    allowed = []
    for root in replay_allowed_roots():
        try:
            allowed.append(root.expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    if not any(source == root or source.is_relative_to(root) for root in allowed):
        return None, "path is outside the allowed capture directories"
    if _replay_denied(source):
        return None, "path is not permitted"
    if not source.is_file():
        return None, "not a regular file"
    return source, ""


@app.post("/api/replay/start")
def replay_start(path: str = "", freq_hz: int = 5756500000,
                 root600: float = 0.0, root147: float = 0.0) -> dict:
    """Replay a recorded IQ file through the live DroneID decoder.

    ``path`` is an IQ file on the sensor box, confined to the capture
    directories. ``freq_hz`` is the centre frequency the capture was taken
    at. The threshold overrides let the same capture be judged against
    several gates.
    """
    source, reason = resolve_replay_source(path)
    if source is None:
        return {"ok": False, "error": reason}
    if not REPLAY_SCAN.exists():
        return {"ok": False, "error": f"scanner not found: {REPLAY_SCAN}"}
    default600, default147 = zc_gate.thresholds(CONFIG)
    root600 = root600 if root600 > 0 else default600
    root147 = root147 if root147 > 0 else default147
    replay_stop()
    state["replay_published"] = 0
    threading.Thread(
        target=replay_worker,
        args=(source, int(freq_hz), float(root600), float(root147)),
        daemon=True,
    ).start()
    return {
        "ok": True,
        "path": str(source),
        "freq_hz": int(freq_hz),
        "root600_threshold": float(root600),
        "root147_threshold": float(root147),
    }


@app.post("/api/replay/stop")
def replay_stop_endpoint() -> dict:
    replay_stop()
    broadcast({"type": "status", "status": status_dict()})
    return {"ok": True}


@app.get("/api/status")
def status() -> dict:
    return status_dict()


# Hop RF turns (esp. MAVLink wideband demod) routinely exceed 60s on ARM.
BEAT_BUDGET_S = {
    "hop": 300,
    "droneid": 90,
    "retention": 120,
    "netgeo": 120,
    "adsb_net": 90,
    "replay": 300,
}
BEAT_BUDGET_DEFAULT_S = 60


@app.get("/api/ready")
def ready() -> dict:
    return readiness()


@app.get("/api/health")
def health() -> dict:
    """Liveness of every background loop plus storage, demods, and radio state."""
    now = time.time()
    beats = {k: round(now - v, 1) for k, v in sorted(BEATS.items())}
    stale = [
        name for name, age in beats.items()
        if age > BEAT_BUDGET_S.get(name, BEAT_BUDGET_DEFAULT_S)
    ]
    ready = readiness()
    try:
        db_bytes = DB.stat().st_size
    except OSError:
        db_bytes = 0
    return {
        "ok": not stale and ready["ok"],
        "beats": beats,
        "stale": stale,
        "ready": ready["ok"],
        "problems": ready["problems"],
        "demods": {"elrs": DEMOD is not None, "mavlink": MAV is not None},
        "db_bytes": db_bytes,
        "dongles": dongle_status(),
        "internet": state.get("internet"),
    }


@app.get("/api/history/sessions")
def history_sessions(limit: int = 100) -> list:
    with db_conn() as c:
        rows = c.execute(
            """SELECT session, MIN(time) first, MAX(time) last, COUNT(*) n,
                      MAX(rssi) best, GROUP_CONCAT(DISTINCT source) sources,
                      GROUP_CONCAT(DISTINCT marker) markers
               FROM events GROUP BY session ORDER BY last DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    result = [dict(r) for r in rows]

    def span(r):
        return (datetime.datetime.fromisoformat(r["first"]),
                datetime.datetime.fromisoformat(r["last"]))

    def _tokens(value: str) -> str:
        parts = [textsan.safe_label(part) for part in str(value or "").split(",")]
        return ",".join(part for part in parts if part)

    # One local-registry lookup per serial. Repeats of a serial already
    # seen hit the cache instead of scanning the list again.
    aircraft = CONFIG.get("aircraft") or {}
    rid_rows = []
    for r in result:
        r["session"] = textsan.safe_label(r.get("session"))
        r["sources"] = _tokens(r.get("sources") or "")
        r["markers"] = _tokens(r.get("markers") or "")
        model = aircraft_registry.registry.resolve(r.get("session"), aircraft)
        if model:
            r["model"] = model
        if "remoteid" in (r.get("sources") or ""):
            rid_rows.append(r)
    # propagate model to DroneID sessions active at the same time
    for r in result:
        if r.get("model") or r in rid_rows:
            continue
        try:
            a, b = span(r)
        except (KeyError, ValueError):
            continue
        for q in rid_rows:
            if "model" not in q:
                continue
            try:
                c1, c2 = span(q)
            except (KeyError, ValueError):
                continue
            if a <= c2 and c1 <= b:
                r["model"] = q["model"]
                break
    return result


def _history_where(session: str, source: str, frm: str, to: str) -> tuple[str, list]:
    q, args = "SELECT * FROM events WHERE 1=1", []
    if session:
        q += " AND session=?"
        args.append(session)
    if source:
        q += " AND source=?"
        args.append(source)
    # The date pickers show dates in the viewer's local time, so the day
    # boundaries have to be local too. Stored timestamps are UTC, so the
    # bounds are converted before the comparison.
    tz = local_timezone()
    if frm:  # ISO date or datetime, lexicographic compare works on our stamps
        q += " AND time>=?"
        args.append(_local_bound_to_utc(frm, tz, "start"))
    if to:
        q += " AND time<=?"
        args.append(_local_bound_to_utc(to, tz, "end"))
    return q, args


def _local_bound_to_utc(bound: str, tz, edge: str) -> str:
    """Turn a local date (or datetime) boundary into the matching UTC stamp.

    A bare date means the whole local day: "start" is midnight local,
    "end" is the last instant of that day. A full datetime is taken as
    given.
    """
    text = bound.strip()
    if "T" in text:
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError:
            return text
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(datetime.timezone.utc).isoformat()
    try:
        day = datetime.date.fromisoformat(text)
    except ValueError:
        return text
    if edge == "start":
        local = datetime.datetime.combine(day, datetime.time.min, tzinfo=tz)
    else:
        local = datetime.datetime.combine(
            day, datetime.time.max, tzinfo=tz)
    return local.astimezone(datetime.timezone.utc).isoformat()


def local_timezone():
    """The station's timezone, as a tzinfo. Falls back to the system local
    zone when the database is unavailable."""
    try:
        import zoneinfo
        name = Path("/etc/timezone")
        if name.exists():
            return zoneinfo.ZoneInfo(name.read_text().strip())
        return datetime.datetime.now().astimezone().tzinfo
    except Exception:
        return datetime.datetime.now().astimezone().tzinfo


def to_local_iso(stamp: str | None, tz=None) -> str | None:
    """Rewrite a stored UTC timestamp as an ISO string in local time."""
    if not stamp:
        return stamp
    tz = tz or local_timezone()
    try:
        parsed = datetime.datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return stamp
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(tz).isoformat()


@app.get("/api/history/events")
def history_events(session: str = "", limit: int = 500, offset: int = 0,
                   source: str = "", frm: str = "", to: str = "") -> list:
    q, args = _history_where(session, source, frm, to)
    q += " ORDER BY time DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with db_conn() as c:
        rows = c.execute(q, args).fetchall()
    return [textsan.sanitize_text_fields(dict(r)) for r in rows]


def _history_fetched(session: str, source: str, frm: str, to: str) -> list[dict]:
    q, args = _history_where(session, source, frm, to)
    q += " ORDER BY time DESC"
    local_tz = local_timezone()
    out = []
    with db_conn() as c:
        for r in c.execute(q, args):
            row = track_export.public_row(dict(r))
            row["time"] = to_local_iso(row.get("time"), local_tz)
            out.append(row)
    return out


@app.get("/api/history/export")
def history_export(session: str = "", source: str = "", frm: str = "",
                   to: str = "", fmt: str = "csv"):
    """Export the filtered event history.

    ``fmt`` is ``csv`` (default), ``kml``, or ``gpx``. Text fields are
    constrained and then encoded for that format. The CSV ``time`` column
    is the station's local time. A cell that would be a spreadsheet
    formula is prefixed so it stays text.
    """
    kind = (fmt or "csv").strip().lower()
    if kind in ("kml", "gpx"):
        rows = _history_fetched(session, source, frm, to)
        if kind == "kml":
            body = track_export.to_kml(rows)
            media = "application/vnd.google-earth.kml+xml"
            filename = "watchtower-events.kml"
        else:
            body = track_export.to_gpx(rows)
            media = "application/gpx+xml"
            filename = "watchtower-events.gpx"
        return Response(
            content=body,
            media_type=media,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    q, args = _history_where(session, source, frm, to)
    q += " ORDER BY time DESC"
    local_tz = local_timezone()

    def rows():
        header = list(EVENT_COLS)
        if header and header[0] == "time":
            header[0] = "time_local"
        yield track_export.csv_document(header, [])
        with db_conn() as c:
            cursor = c.execute(q, args)
            while True:
                batch = cursor.fetchmany(500)
                if not batch:
                    break
                prepared = []
                for r in batch:
                    values = []
                    for key in EVENT_COLS:
                        value = r[key]
                        if key in track_export.TEXT_FIELDS and isinstance(value, str):
                            value = textsan.safe_label(value)
                        values.append(value)
                    values[0] = to_local_iso(values[0], local_tz)
                    prepared.append(values)
                # Header was already written. Append data rows only.
                buf = io.StringIO()
                writer = csv.writer(buf)
                for values in prepared:
                    writer.writerow([textsan.csv_cell(value) for value in values])
                yield buf.getvalue()

    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=watchtower-events.csv"},
    )


@app.websocket("/ws")
async def ws(ws: WebSocket) -> None:
    global loop
    # The browser sends the cookie. A Python client can also send
    # X-Watchtower-Token or Authorization: Bearer. The token does not
    # belong in the URL: that line is written to the access log.
    #
    # The socket is accepted first and then closed with 4401: closing
    # before the handshake completes is delivered to the browser as a
    # plain HTTP 403 rejection, and the client cannot tell that apart
    # from a network fault — so it would reconnect in a loop.
    expected = api_token()
    if expected:
        supplied = presented_token(ws.headers, ws.cookies, ws.query_params)
        if not supplied or not secrets.compare_digest(supplied, expected):
            await ws.accept()
            await ws.close(code=4401)
            return
    await ws.accept()
    loop = asyncio.get_event_loop()
    clients.add(ws)
    await ws.send_text(json.dumps({"type": "status", "status": status_dict()}))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)


def remoteid_event(drone: list) -> dict | None:
    """Map a bkerler/DroneID macdb entry to a watchtower event.

    Every string here is broadcast by the drone: the Basic ID serial, the
    operator status, and the Self ID free text. They all reach the
    dashboard, so they are constrained to a markup-free charset here,
    at the trust boundary.
    """
    mac, serial, pos, sysinfo, selfid, operator = None, None, None, None, None, None
    for item in drone:
        if isinstance(item, dict):
            if "MAC" in item:
                mac = textsan.safe_label(item["MAC"])
            bid = item.get("Basic ID") or {}
            uas = bid.get("id") or bid.get("uas_id")
            if uas:
                kind = str(bid.get("id_type", ""))
                if serial is None or "Serial" in kind:
                    serial = textsan.safe_label(uas)
            if "Location Vector" in item:
                c = item["Location Vector"].get("coord") or {}
                if c.get("latitude") or c.get("longitude"):
                    pos = {
                        "lat": c["latitude"] / 1e7,
                        "lon": c["longitude"] / 1e7,
                        "alt": (c.get("geodetic_altitude") or 0) / 1000,
                        "speed": c.get("speed", 0),
                        "dir": c.get("direction"),
                        "status": textsan.safe_label(item["Location Vector"].get("op_status")),
                    }
            if "System" in item:
                s = item["System"]
                if s.get("latitude") or s.get("longitude"):
                    sysinfo = {"op_lat": s["latitude"] / 1e7, "op_lon": s["longitude"] / 1e7}
            if "Self ID" in item:
                selfid = textsan.safe_label(item["Self ID"].get("text"))
            if "Operator ID" in item and isinstance(item["Operator ID"], dict):
                block = item["Operator ID"]
                operator = textsan.safe_label(
                    block.get("id") or block.get("operator_id") or block.get("text"))
    ident = serial or mac
    if not ident:
        return None
    evt = {
        "source": "remoteid",
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
        "session": ident,
        "marker": "RemoteID",
        "mac": mac,
        "selfid": selfid,
        "operator": operator or "",
        "crc": True,
        "freq": 2437.0,
        "rssi": 0,
    }
    if pos:
        evt.update(pos)
    if sysinfo:
        evt.update(sysinfo)
    return evt


def adsb_snapshot() -> list[dict]:
    """One poll of the local 1090 MHz receiver. Positions only."""
    url = CONFIG.get("adsb_url") or ADSB_URL
    with urllib.request.urlopen(url, timeout=4) as resp:
        data = json.loads(resp.read().decode())
    craft = []
    for a in data.get("aircraft") or []:
        if a.get("lat") is None or a.get("lon") is None:
            continue
        if a.get("display_valid") is False:
            continue
        craft.append({
            "hex": textsan.safe_hex(a.get("hex")),
            "flight": textsan.safe_label(a.get("flight") or a.get("callsign")),
            "type": textsan.safe_label(a.get("aircraft_type") or a.get("category_label")),
            "category": textsan.safe_label(a.get("category")),
            "alt_ft": a.get("alt_baro"),
            "gs_kt": a.get("gs"),
            "track": a.get("track"),
            "lat": a.get("lat"),
            "lon": a.get("lon"),
            "squawk": textsan.safe_label(a.get("squawk")),
            "seen": a.get("seen"),
            "messages": a.get("messages"),
            "distance_nm": a.get("distance_nm"),
            "country": textsan.safe_label(a.get("country") or ""),
            "last_seen": a.get("lastSeen") or "",
            # This is what the box itself heard over the air.
            "source": "1090",
            "feed": "local",
        })
    return [c for c in craft if c["hex"]]


def fr24_api_token() -> str:
    return str(CONFIG.get("fr24_api_token") or "").strip()


def adsb_provider() -> str:
    return "flightradar24" if fr24_api_token() else "adsb.lol"


def adsb_network_feeds() -> list[dict]:
    """Aggregator feeds to poll.

    A FlightRadar24 API token replaces the public adsb.lol / adsb.fi feeds.
    With no token, the free aggregators stay in use.
    """
    token = fr24_api_token()
    if token:
        return [{
            "name": "flightradar24",
            "kind": "fr24",
            "token": token,
        }]
    configured = CONFIG.get("adsb_network_feeds")
    if configured is None:
        return list(ADSB_NETWORK_FEEDS)
    if not isinstance(configured, list):
        return list(ADSB_NETWORK_FEEDS)
    feeds = []
    for entry in configured:
        if isinstance(entry, dict) and entry.get("url"):
            feeds.append({
                "name": entry.get("name") or "net",
                "url": entry["url"],
                "key": entry.get("key") or "aircraft",
            })
    return feeds


def adsb_config_public() -> dict:
    token = fr24_api_token()
    hint = ""
    if token:
        hint = ("*" * max(0, len(token) - 4)) + token[-4:]
    return {
        "provider": adsb_provider(),
        "fr24_configured": bool(token),
        "fr24_token_hint": hint,
        "radius_nm": adsb_network_radius(),
    }


def adsb_network_radius() -> int:
    try:
        return max(1, int(CONFIG.get("adsb_network_radius_nm", ADSB_NETWORK_RADIUS_NM)))
    except (TypeError, ValueError):
        return ADSB_NETWORK_RADIUS_NM


def annotate_adsb(tracks: list[dict]) -> list[dict]:
    """Attach distance-from-station and the small-UAS heuristic to tracks."""
    station = station_dict()
    lat = station.get("lat")
    lon = station.get("lon")
    for track in tracks:
        if lat is not None and lon is not None:
            try:
                track["distance_nm"] = round(
                    adsb_network.haversine_nm(
                        lat, lon, float(track["lat"]), float(track["lon"])), 2)
            except (KeyError, TypeError, ValueError):
                pass
        track.update(adsb_classify.classify(track))
    return tracks


def adsb_snapshot_all() -> list[dict]:
    """Local 1090 tracks merged with the network feeds.

    The local receiver always wins a tie on ICAO hex: a track this box
    received over the air is more trustworthy than the same aircraft seen
    by an aggregator.
    """
    local = state.get("adsb_local") or []
    network = state.get("adsb_network") or []
    merged = adsb_network.merge_tracks(local, network)
    return annotate_adsb(merged)


def adsb_local_loop() -> None:
    """Poll the local 1090 MHz receiver that feeds adsb1090."""
    while True:
        beat("adsb_local")
        try:
            local = adsb_snapshot()
            state["adsb_local"] = local
            state["adsb_ok"] = True
            state["adsb_error"] = None
        except Exception as exc:
            state["adsb_ok"] = False
            state["adsb_error"] = str(exc)
        time.sleep(ADSB_POLL_INTERVAL_S)


def adsb_network_loop() -> None:
    """Poll network ADS-B (adsb.lol by default, or FlightRadar24 with a key).

    These are not what this station received — they are what the
    aggregator networks heard. Every track from here is tagged "net" so
    the dashboard can keep the two apart. Feeds are re-read each cycle so
    a newly saved FR24 token takes effect without a restart.
    """
    while True:
        beat("adsb_net")
        feeds = adsb_network_feeds()
        state["adsb_provider"] = adsb_provider()
        if not feeds:
            state["adsb_network"] = []
            time.sleep(ADSB_NETWORK_INTERVAL_S)
            continue
        station = station_dict()
        lat, lon = station.get("lat"), station.get("lon")
        if lat is None or lon is None:
            state["adsb_network"] = []
            time.sleep(ADSB_NETWORK_INTERVAL_S)
            continue
        try:
            tracks = adsb_network.collect(
                lat, lon, adsb_network_radius(), feeds)
            state["adsb_network"] = tracks
            state["adsb_network_error"] = None
        except Exception as exc:
            state["adsb_network_error"] = str(exc)
        time.sleep(ADSB_NETWORK_INTERVAL_S)


def adsb_loop() -> None:
    """Push the merged ADS-B picture to the dashboard."""
    while True:
        beat("adsb")
        tracks = adsb_snapshot_all()
        broadcast({"type": "adsb", "aircraft": tracks})
        time.sleep(ADSB_POLL_INTERVAL_S)


def hop_survey(freq_span: str, lo: float, hi: float, min_span: float, min_bursts: int, band: str) -> dict:
    """Burst-hop presence inside one band. No control-channel contents."""
    if not state.get("armed"):
        return {"present": False, "busy": False, "band": band}
    if not HACKRF_HOP:
        # No serial assigned to the hopping tuner, so libhackrf would be
        # handed an empty device string and open nothing.
        return {"present": False, "busy": False, "band": band}
    try:
        proc = subprocess.Popen(
            ["hackrf_sweep", "-d", HACKRF_HOP, "-f", freq_span, "-w", "250000", "-l", "32", "-g", "24", "-N", "3", "-r", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return {"present": False, "busy": False, "band": band}
    state["sweep"] = proc
    try:
        stdout, _ = proc.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        kill_proc(proc)
        stdout = ""
    finally:
        if state.get("sweep") is proc:
            state["sweep"] = None
    if not state.get("armed"):
        return {"present": False, "busy": False, "band": band}
    if proc.returncode != 0:
        return {"present": False, "busy": True, "band": band}
    peaks: dict[float, list[float]] = {}
    noise: list[float] = []
    for line in (stdout or "").splitlines():
        parts = line.split(", ")
        if len(parts) < 7:
            continue
        try:
            hz_low = float(parts[2])
            bw = float(parts[4])
            vals = [float(x) for x in parts[6:] if x.strip()]
        except ValueError:
            continue
        for i, db in enumerate(vals):
            mhz = round((hz_low + i * bw) / 1e6, 1)
            if mhz < lo or mhz > hi:
                continue
            peaks.setdefault(mhz, []).append(db)
            noise.append(db)
    if not noise:
        return {"present": False, "busy": False, "band": band}
    noise.sort()
    floor = noise[len(noise) // 2]
    bursty = []
    for mhz, vals in peaks.items():
        peak = max(vals)
        avg = sum(vals) / len(vals)
        if peak > floor + 15 and (peak - avg) > 18:
            bursty.append((mhz, peak))
    bursty.sort()
    span = (bursty[-1][0] - bursty[0][0]) if len(bursty) >= 2 else 0.0
    present = len(bursty) >= min_bursts and span >= min_span
    peak_db = max((p for _, p in bursty), default=floor)
    return {
        "present": present,
        "busy": False,
        "band": band,
        "bursts": len(bursty),
        "span_mhz": round(span, 1),
        "peak_db": round(peak_db, 1),
        "floor_db": round(floor, 1),
    }


def elrs_survey() -> dict:
    return hop_survey("2400:2483", 2400, 2483, 50.0, 18, "2.4 GHz")


def elrs_demod_pass() -> dict:
    """ELRS turn: fast sweep gate, then packet-level demod on the HackRF One.

    The sweep gate is only a trigger — Wi-Fi / O3 energy can satisfy it on its
    own. Try several candidate centers (last verified first) so continuous
    OcuSync does not permanently steal the capture frequency. Only verified
    ELRS packets make the live row appear.
    """
    gate = elrs_survey()
    if not gate.get("present") or DEMOD is None:
        return {"present": False, "band": "2.4 GHz"}
    try:
        centers = DEMOD.sweep_candidate_centers(3)
        if not centers:
            return {"present": False, "band": "2.4 GHz"}
        radio_busy = False
        for center in centers:
            iq_path = DEMOD.capture(center, 5)
            if not iq_path:
                radio_busy = True
                continue
            try:
                sig = DEMOD.load_baseband(iq_path)
            finally:
                try:
                    os.remove(iq_path)
                except OSError:
                    pass
            counts: dict[int, int] = {}
            best_snr = None
            downs = 0
            for sf in DEMOD.SF_LIST:
                for inverted in (False, True):
                    for p in DEMOD.scan_polarity(sig, sf, inverted):
                        if p["sync"] != [8, 16] or not p["downchirps_ok"]:
                            continue
                        if inverted:
                            downs += 1
                        counts[sf] = counts.get(sf, 0) + 1
                        if best_snr is None or p["snr_db"] > best_snr:
                            best_snr = p["snr_db"]
            del sig
            if not counts:
                print(
                    f"elrs: energy at {round(center / 1e6, 1)} MHz but no LoRa sync",
                    flush=True,
                )
                continue
            sf = max(counts, key=lambda k: counts[k])
            DEMOD.note_center(center)
            out = {
                "present": True,
                "band": "2.4 GHz",
                "modulation": f"LoRa SF{sf}",
                "rate": ELRS_RATES.get(sf, "ELRS"),
                "packets": sum(counts.values()),
                "snr_db": best_snr,
                "downlink": downs,
                "freq_mhz": round(center / 1e6, 1),
            }
            try:
                insert_event({
                    "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "session": "elrs-2g4",
                    "source": "elrs",
                    "marker": f"ELRS {out['rate']}",
                    "freq": out["freq_mhz"],
                    "rssi": best_snr,
                    "lat": None, "lon": None, "alt": None, "crc": True,
                })
            except Exception as exc:
                print(f"elrs history insert failed: {exc}", flush=True)
            return out
        if radio_busy:
            return gate                            # radio busy; keep last word
        return {"present": False, "band": "2.4 GHz"}   # energy, but not ELRS
    except Exception as exc:
        print(f"elrs demod pass failed: {exc}", flush=True)
        return gate


def upsert_mavlink(vehicles: list[dict]) -> None:
    """Merge decoded vehicles into live state and record a throttled history row."""
    now = time.time()
    store = state.setdefault("mavlink", {})
    hist_at = state.setdefault("mavlink_hist_at", {})
    for v in vehicles:
        sid = v.get("sysid")
        if not sid:
            continue
        prev = store.get(sid) or {}
        merged = dict(prev)
        for key, val in v.items():
            if val is not None:
                merged[key] = val
        merged["last"] = now
        merged["packets"] = (prev.get("packets") or 0) + (v.get("packets") or 1)
        merged["present"] = True
        store[sid] = merged
        if now - (hist_at.get(sid) or 0) < 3:
            continue
        hist_at[sid] = now
        try:
            insert_event({
                "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "session": f"mavlink-{sid}",
                "source": "mavlink",
                "marker": merged.get("type_name") or "MAVLink",
                "freq": merged.get("freq"),
                "rssi": merged.get("link_rssi"),
                "lat": merged.get("lat"),
                "lon": merged.get("lon"),
                "alt": merged.get("alt_m"),
                "crc": True,
            })
        except Exception as exc:
            print(f"mavlink history insert failed: {exc}", flush=True)


def mavlink_snapshot() -> list[dict]:
    now = time.time()
    store = state.get("mavlink") or {}
    out = []
    for sid, v in list(store.items()):
        if now - (v.get("last") or 0) > 90:
            store.pop(sid, None)
            continue
        out.append(dict(v, present=True))
    return out


def ingest_mavlink_frames(frames: list, freq=None, via: str = "udp") -> None:
    if not frames or MAV is None:
        return
    acc: dict = {}
    for fr in frames:
        MAV.merge_vehicle(acc, fr, freq=freq, via=via)
    upsert_mavlink([MAV.public_vehicle(v) for v in acc.values()])


def mavlink_udp_loop() -> None:
    """Listen for MAVLink already arriving as UDP (Wi-Fi telemetry, companion GCS)."""
    if MAV is None:
        return
    socks = []
    for port in (14550, 14555):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
            sock.setblocking(False)
            socks.append(sock)
            print(f"mavlink udp listening :{port}", flush=True)
        except OSError as exc:
            print(f"mavlink udp {port}: {exc}", flush=True)
            sock.close()
    if not socks:
        return
    last_emit = 0.0
    while True:
        beat("mavlink_udp")
        ready, _, _ = select.select(socks, [], [], 1.0)
        changed = False
        for sock in ready:
            try:
                data, _addr = sock.recvfrom(4096)
            except OSError:
                continue
            frames = MAV.extract_frames(data)
            if frames:
                ingest_mavlink_frames(frames, via="udp")
                changed = True
        now = time.time()
        if changed and now - last_emit >= 0.4:
            last_emit = now
            broadcast({"type": "mavlink", "vehicles": mavlink_snapshot()})


def mavlink_rf_pass() -> list[dict]:
    """HackRF One turn: narrow 433/915 FSK → MAVLink frames."""
    if not state.get("armed") or MAV is None:
        return mavlink_snapshot()
    if not HACKRF_HOP:
        # No serial assigned to the hopping tuner.
        return mavlink_snapshot()
    try:
        found = MAV.pass_rf(HACKRF_HOP)
    except Exception as exc:
        print(f"mavlink rf pass failed: {exc}", flush=True)
        return mavlink_snapshot()
    if found:
        upsert_mavlink(found)
    return mavlink_snapshot()


def tbs_survey() -> dict:
    """Crossfire-style hop. 868 is a narrow plan; 915 is wider. Steady carriers are ignored."""
    eu = hop_survey("863:870", 863, 870, 1.0, 4, "868 MHz")
    fcc = hop_survey("902:928", 903, 927, 8.0, 8, "915 MHz")
    if fcc.get("present"):
        return fcc
    if eu.get("present"):
        return eu
    stronger = fcc if fcc.get("bursts", 0) >= eu.get("bursts", 0) else eu
    stronger["present"] = False
    return stronger


def remember(link: dict, last: dict | None, last_at: float, hold: float = 20.0) -> tuple[dict, dict | None, float]:
    now = time.time()
    if link.get("present"):
        return link, link, now
    if last and now - last_at < hold:
        return last, last, last_at
    return link, None, 0.0


def droneid_loop() -> None:
    """HackRF Pro stays on the 2.4 and 5.8 GHz DroneID rasters."""
    while True:
        beat("droneid")
        if not state.get("armed"):
            time.sleep(0.3)
            continue
        pro_present = any(
            d["serial"] == HACKRF_DRONEID
            for d in usb_devices()
            if HACKRF_DRONEID and d["serial"]
        )
        if not pro_present:
            state["phase_droneid"] = None
            refresh_phase()
            time.sleep(20)
            continue
        ts = time.strftime("%Y%m%d_%H%M%S")
        out = RESULTS / f"cycle-{ts}"
        out.mkdir(parents=True, exist_ok=True)
        band = str(CONFIG.get("scanner_band") or "both")
        preset = str(CONFIG.get("scanner_preset") or "baseline")
        cmd = [
            "python3", SCANNER,
            "--band", band, "--preset", preset, "--cycles", "0",
            "--lna", "32", "--vga", "24",
            "--serial", HACKRF_DRONEID,
            "--libhackrf", LIBHACKRF,
            "--turbo-binary", TURBO,
            "--output", str(out),
        ]
        freqs = CONFIG.get("scanner_freqs")
        if isinstance(freqs, list) and freqs:
            cmd += ["--freqs", ",".join(str(f) for f in freqs)]
        elif isinstance(freqs, str) and freqs.strip():
            cmd += ["--freqs", freqs.strip()]
        dwell = CONFIG.get("scanner_dwell")
        if dwell:
            cmd += ["--dwell", str(dwell)]
        cmd += zc_gate.cli_args(CONFIG)
        state.update(running=True, mode="live", phase_droneid="DroneID", run_dir=out)
        refresh_phase()
        log = open(out / "scanner.log", "w")
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        state["proc"] = proc
        seen = 0
        path = out / "detections.jsonl"
        while proc.poll() is None and state.get("armed"):
            beat("droneid")
            if path.exists():
                lines = path.read_text().splitlines()
                for line in lines[seen:]:
                    evt = parse_line(line)
                    if evt and evt["crc"]:
                        evt["source"] = "droneid"
                        publish_event(evt)
                seen = len(lines)
            time.sleep(0.4)
        if proc.poll() is None:
            kill_proc(proc)
        log.close()
        if state.get("proc") is proc:
            state["proc"] = None
        state["phase_droneid"] = None
        refresh_phase()
        time.sleep(1)


VTX_TABLE = {
    5865: "A1", 5845: "A2", 5825: "A3", 5805: "A4", 5785: "A5", 5765: "A6", 5745: "A7", 5725: "A8",
    5733: "B1", 5752: "B2", 5771: "B3", 5790: "B4", 5809: "B5", 5828: "B6", 5847: "B7", 5866: "B8",
    5705: "E1", 5685: "E2", 5665: "E3", 5645: "E4", 5885: "E5", 5905: "E6",
    5740: "F1", 5760: "F2", 5780: "F3", 5800: "F4", 5820: "F5", 5840: "F6", 5860: "F7", 5880: "F8",
    5658: "R1", 5695: "R2", 5732: "R3", 5769: "R4", 5806: "R5", 5843: "R6", 5917: "R8",
}


def vtx_survey() -> list[dict]:
    """Analog 5.8 GHz FPV video carriers: sustained, strong, channel-mapped.

    Analog VTX transmits continuously while powered, so the carrier is
    elevated in every sweep pass. Wi-Fi shares this band, so only strong
    sustained clusters are reported, mapped to the 40-channel FPV table.
    """
    if not state.get("armed"):
        return []
    try:
        proc = subprocess.run(
            ["hackrf_sweep", "-d", HACKRF_HOP, "-f", "5600:5920", "-w", "250000",
             "-l", "32", "-g", "24", "-N", "2", "-r", "-"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    sweeps: dict[float, list[float]] = {}
    allv: list[float] = []
    for line in proc.stdout.splitlines():
        parts = line.split(", ")
        if len(parts) < 7:
            continue
        try:
            hz_low = float(parts[2])
            bw = float(parts[4])
            vals = [float(x) for x in parts[6:] if x.strip()]
        except ValueError:
            continue
        for i, db in enumerate(vals):
            mhz = round((hz_low + i * bw) / 1e6, 1)
            if not (5600 <= mhz <= 5920):
                continue
            sweeps.setdefault(mhz, []).append(db)
            allv.append(db)
    if not allv:
        return []
    allv.sort()
    floor = allv[len(allv) // 2]
    hot = {m: max(dbs) for m, dbs in sweeps.items()
           if len(dbs) >= 2 and min(dbs) > floor + 15}
    if not hot:
        return []
    keys = sorted(hot)
    clusters, cur = [], [keys[0]]
    for m in keys[1:]:
        if m - cur[-1] <= 0.6:
            cur.append(m)
        else:
            clusters.append(cur)
            cur = [m]
    clusters.append(cur)
    entries = []
    for c in clusters:
        peak_mhz = max(c, key=lambda m: hot[m])
        peak = hot[peak_mhz]
        width = round(c[-1] - c[0] + 0.25, 1)
        if peak < floor + 20 or width < 0.5 or width > 30:
            continue
        # energy centroid over +-8 MHz: FM video peaks sit off the channel center
        wsum = csum = 0.0
        for m, dbs in sweeps.items():
            if abs(m - peak_mhz) <= 8:
                w = max(0.0, max(dbs) - floor)
                wsum += w
                csum += w * m
        centroid = csum / wsum if wsum else peak_mhz
        entries.append({"peak_mhz": peak_mhz, "peak": peak, "width": width, "centroid": centroid})
    # merge spurs: a weak narrow carrier within 30 MHz of a much stronger one is
    # a sideband or spur of the same transmitter, not a second camera
    entries.sort(key=lambda e: -e["peak"])
    merged = []
    for e in entries:
        if any(m["peak"] - e["peak"] >= 15 and abs(m["centroid"] - e["centroid"]) <= 30
               for m in merged):
            continue
        merged.append(e)
    links = []
    for e in merged:
        centroid = round(e["centroid"], 1)
        nearest = min(VTX_TABLE, key=lambda f: abs(f - centroid))
        channel = VTX_TABLE[nearest] if abs(nearest - centroid) <= 2.5 else None
        links.append({
            "freq": centroid,
            "channel": channel,
            "peak_db": round(e["peak"], 1),
            "width_mhz": e["width"],
            "floor_db": round(floor, 1),
        })
    links.sort(key=lambda l: -l["peak_db"])
    return links[:8]


def usb_cam_loop() -> None:
    """Stream the USB FPV receiver's capture into the video panel.

    The receiver does the RF work; this just relays its picture. Snow means
    no VTX is transmitting on the receiver's channel.
    """
    if not HAVE_CV2:
        return
    while True:
        beat("vtx_video")
        index = usb_locate.uvc_index()
        if index is None:
            time.sleep(2)
            continue
        cap = cv2.VideoCapture(index + cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FPS, 15)
        if not cap.isOpened():
            time.sleep(5)
            continue
        prev_small = None
        last_emit = 0.0
        while True:
            try:
                ok, frame = cap.read()
            except Exception:
                break
            if not ok:
                break
            if time.time() - last_emit >= 0.2:
                small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (64, 48))
                if prev_small is None:
                    snow = False
                else:
                    diff = float(cv2.absdiff(small, prev_small).mean())
                    snow = diff > 18.0          # full-frame decorrelation = noise
                prev_small = small
                ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 55])
                if ok2:
                    b64 = base64.b64encode(buf.tobytes()).decode()
                    broadcast({"type": "vtx_frame", "jpeg": b64, "source": "usb",
                               "no_signal": snow})
                    last_emit = time.time()
        try:
            cap.release()
        except Exception:
            pass
        time.sleep(3)


def with_hop_beat(fn, *args, **kwargs):
    """Keep the hop watchdog fresh while a long RF/demod turn runs."""
    stop = threading.Event()

    def _keep() -> None:
        beat("hop")
        while not stop.wait(5.0):
            beat("hop")

    keeper = threading.Thread(target=_keep, daemon=True, name="hop-beat")
    keeper.start()
    try:
        return fn(*args, **kwargs)
    finally:
        stop.set()
        keeper.join(timeout=1.0)
        beat("hop")


def hop_loop() -> None:
    """HackRF One alternates ELRS 2.4 GHz, TBS 868/915, MAVLink 433/915, and VTX 5.8 GHz."""
    last_elrs: dict | None = None
    last_elrs_at = 0.0
    last_tbs: dict | None = None
    last_tbs_at = 0.0
    vtx_last: dict[str, tuple[dict, float]] = {}
    while True:
        beat("hop")
        if not state.get("armed"):
            if state.get("phase_hop"):
                state["phase_hop"] = None
                refresh_phase()
            time.sleep(0.3)
            continue
        state["phase_hop"] = "ELRS"
        refresh_phase()
        beat("hop")
        shown, last_elrs, last_elrs_at = remember(
            with_hop_beat(elrs_demod_pass), last_elrs, last_elrs_at, 120)
        if not state.get("armed"):
            continue
        broadcast({"type": "elrs", "link": shown})
        state["phase_hop"] = "TBS"
        refresh_phase()
        beat("hop")
        shown, last_tbs, last_tbs_at = remember(
            with_hop_beat(tbs_survey), last_tbs, last_tbs_at, 60)
        if not state.get("armed"):
            continue
        broadcast({"type": "tbs", "link": shown})
        state["phase_hop"] = "MAVLink"
        refresh_phase()
        beat("hop")
        vehicles = with_hop_beat(mavlink_rf_pass)
        if not state.get("armed"):
            continue
        broadcast({"type": "mavlink", "vehicles": vehicles})
        state["phase_hop"] = "VTX"
        refresh_phase()
        beat("hop")
        found = with_hop_beat(vtx_survey)
        now = time.time()
        for l in found:
            key = l["channel"] or str(l["freq"])
            vtx_last[key] = (l, now)
            try:
                insert_event({
                    "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "session": f"vtx-{key}",
                    "source": "vtx",
                    "marker": f"VTX {l['channel']}" if l["channel"] else "VTX",
                    "freq": l["freq"],
                    "rssi": l["peak_db"],
                    "lat": None, "lon": None, "alt": None, "crc": True,
                })
            except Exception as exc:
                print(f"vtx history insert failed: {exc}", flush=True)
        shown_links = []
        for key in list(vtx_last):
            l, t = vtx_last[key]
            if now - t > 180:
                vtx_last.pop(key)
            else:
                shown_links.append(l)
        if not state.get("armed"):
            continue
        broadcast({"type": "vtx", "links": shown_links})


def remoteid_loop(endpoint: str) -> None:
    if not HAVE_ZMQ:
        return
    name = "rid:" + endpoint.rsplit(":", 1)[-1]
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt_string(zmq.SUBSCRIBE, "")
    sub.setsockopt(zmq.RCVTIMEO, 15000)  # raise instead of blocking forever
    sub.connect(endpoint)
    while True:
        beat(name)
        try:
            msg = sub.recv_string()
        except zmq.Again:
            continue
        try:
            data = json.loads(msg)
            if isinstance(data, dict) and isinstance(data.get("c5_health"), dict):
                state["c5_health"] = data["c5_health"]
                continue
            if isinstance(data, dict) and "watchtower_event" in data:
                publish_event(data["watchtower_event"])
                continue
            for drone in data:
                evt = remoteid_event(drone)
                if evt:
                    publish_event(evt)
        except json.JSONDecodeError:
            continue
        except Exception:
            time.sleep(1)


def network_geo_loop() -> None:
    """Keep a network position for the station until a GPS receiver has a fix."""
    while True:
        beat("netgeo")
        geo = lookup_network_geo()
        if geo:
            prev = state.get("network_geo") or {}
            state["network_geo"] = geo
            state["network_geo_error"] = None
            if (prev.get("lat"), prev.get("lon")) != (geo["lat"], geo["lon"]):
                print(f"network location {geo['lat']}, {geo['lon']}", flush=True)
                broadcast({"type": "status", "status": status_dict()})
            wait = 900
        else:
            state["network_geo_error"] = "lookup failed"
            wait = 30
        for _ in range(max(1, wait // 30)):
            time.sleep(30)
            beat("netgeo")


@app.on_event("startup")
def startup() -> None:
    init_db()
    ready = readiness()
    if ready["ok"]:
        print("watchtower ready: demods + core radios OK", flush=True)
    else:
        print("watchtower NOT ready:", "; ".join(ready["problems"]), flush=True)
    threading.Thread(target=network_geo_loop, daemon=True).start()
    threading.Thread(target=dongle_loop, daemon=True).start()
    threading.Thread(target=gps_loop, daemon=True).start()
    threading.Thread(target=usb_cam_loop, daemon=True).start()
    threading.Thread(target=adsb_loop, daemon=True).start()
    threading.Thread(target=adsb_local_loop, daemon=True).start()
    threading.Thread(target=adsb_network_loop, daemon=True).start()
    threading.Thread(target=droneid_loop, daemon=True).start()
    threading.Thread(target=hop_loop, daemon=True).start()
    threading.Thread(target=mavlink_udp_loop, daemon=True).start()
    threading.Thread(target=retention_loop, daemon=True).start()
    endpoints = CONFIG.get("remoteid_zmq") or REMOTEID_ZMQ
    for endpoint in endpoints:
        threading.Thread(target=remoteid_loop, args=(endpoint,), daemon=True).start()


class NoCacheStaticFiles(StaticFiles):
    """Static files without caching.

    The dashboard reloads itself when the server reports a newer build,
    but a browser that kept the old app.js in memory would never see
    that. Serving the assets without cache headers means a reload
    always picks up the code that is actually deployed.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response


app.mount("/", NoCacheStaticFiles(directory=str(FRONTEND), html=True), name="frontend")
