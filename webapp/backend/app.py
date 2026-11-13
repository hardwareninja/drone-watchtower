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
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import usb_locate

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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
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
CONFIG = json.loads((BASE / "config.json").read_text())

SCANNER = "/opt/droneid-repo/src/droneid_hackrf_scanner.py"
TURBO = "/opt/droneid-tools/remove_turbo_soft"
LIBHACKRF = "/usr/lib/aarch64-linux-gnu/libhackrf.so.0"
# Pro stays on DroneID. One sweeps ELRS and TBS. One tuner each.
HACKRF_DRONEID = "0000000000000000147865de331550df"
HACKRF_HOP = "0000000000000000a06063c82451835f"

app = FastAPI(title="Drone Watchtower")

state = {
    "running": False,
    "armed": True,         # master switch for DroneID, ELRS, and TBS
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


def parse_line(line: str) -> dict | None:
    try:
        j = json.loads(line)
        p = bytes.fromhex(j["payload_hex"])
        classification = j.get("payload_classification")
        evt = {
            "time": j["event_time_utc"],
            "freq": j["frequency_mhz"],
            "marker": p[2:6].decode("ascii", "replace"),
            "session": p[6:10].hex(),
            "rssi": j.get("dwell_average_power_dbfs"),
            "crc": bool(j.get("crc24a_ok") and j.get("dji_crc16_ok")),
            "sha": (j.get("payload_sha256") or "")[:12],
            "mode": state["mode"],
            "pclass": classification,
        }
        drone = j.get("drone_id")
        if isinstance(drone, dict):
            # classic O2/O3 carries plaintext telemetry: use the real serial
            # as the session so it can line up with a Remote ID later
            serial = str(drone.get("serial") or "").strip()
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


DB = Path("/opt/watchtower-web/data/history.db")
DB.parent.mkdir(parents=True, exist_ok=True)


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
    row = {k: evt.get(k) for k in EVENT_COLS}
    # a position-less event still needs to know where the station was, so the
    # history map can place it correctly after the station moves on GPS
    if not row.get("lat") and not row.get("lon"):
        gps = state.get("gps") or {}
        if gps.get("lat") is not None and gps.get("lon") is not None:
            row["station_lat"] = gps["lat"]
            row["station_lon"] = gps["lon"]
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
    if evt.get("source") == "remoteid" and _rid_dup(evt):
        return
    with lock:
        state["events"] += 1
    # Attach known-aircraft model for both RID and O2 plaintext sessions
    # (O2 classic uses the real serial as session — same key as Remote ID).
    if evt.get("session"):
        model = CONFIG.get("aircraft", {}).get(evt.get("session"))
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


def station_dict() -> dict:
    station = dict(CONFIG["station"])
    gps = state.get("gps") or {}
    if gps.get("lat") is not None and gps.get("lon") is not None:
        station["lat"] = gps["lat"]
        station["lon"] = gps["lon"]
        station["source"] = "gps"
    else:
        station["source"] = "approx"
    return station


def readiness() -> dict:
    """Boot/deploy self-check: missing demods and radios surface here, not as mystery silence."""
    problems: list[str] = []
    for name, mod in (("elrs_demod", DEMOD), ("mavlink_demod", MAV)):
        if mod is None:
            problems.append(f"{name} import failed — packet path disabled")
        path = BASE / f"{name}.py"
        if not path.exists():
            problems.append(f"{name}.py missing under {BASE}")
    present = {d["id"]: d for d in dongle_status()}
    if not present.get("pro", {}).get("attached"):
        problems.append("HackRF Pro (DroneID) not attached")
    if not present.get("one", {}).get("attached"):
        problems.append("HackRF One (ELRS/hop) not attached")
    if not present.get("c5", {}).get("attached"):
        problems.append("ESP32-C5 (RemoteID) not attached")
    return {"ok": not problems, "problems": problems}


def status_dict() -> dict:
    started = state.get("started")
    ready = readiness()
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
        "dongles": dongle_status(),
        "internet": state.get("internet"),
        "adsb_error": state.get("adsb_error"),
        "build": BUILD,
        "ready": ready["ok"],
        "problems": ready["problems"],
        "demods": {"elrs": DEMOD is not None, "mavlink": MAV is not None},
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
    if not state.get("started"):
        state["started"] = time.time()
    broadcast({"type": "status", "status": status_dict()})
    return {"ok": True}


@app.post("/api/scan/stop")
def scan_stop() -> dict:
    stop_everything()
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

    # registry lookup for RemoteID sessions
    aircraft = CONFIG.get("aircraft", {})
    rid_rows = []
    for r in result:
        if "remoteid" in (r.get("sources") or ""):
            rid_rows.append(r)
            m = aircraft.get(r["session"])
            if m:
                r["model"] = m
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
    if frm:  # ISO date or datetime, lexicographic compare works on our stamps
        q += " AND time>=?"
        args.append(frm if "T" in frm else frm + "T00:00:00")
    if to:
        q += " AND time<=?"
        args.append(to if "T" in to else to + "T23:59:59.999999")
    return q, args


@app.get("/api/history/events")
def history_events(session: str = "", limit: int = 500, offset: int = 0,
                   source: str = "", frm: str = "", to: str = "") -> list:
    q, args = _history_where(session, source, frm, to)
    q += " ORDER BY time DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with db_conn() as c:
        rows = c.execute(q, args).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/history/export")
def history_export(session: str = "", source: str = "", frm: str = "",
                   to: str = "") -> StreamingResponse:
    """Stream the filtered event history as CSV, row by row."""
    q, args = _history_where(session, source, frm, to)
    q += " ORDER BY time DESC"

    def rows():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(EVENT_COLS)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        with db_conn() as c:
            cursor = c.execute(q, args)
            while True:
                batch = cursor.fetchmany(500)
                if not batch:
                    break
                for r in batch:
                    w.writerow([r[k] for k in EVENT_COLS])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=watchtower-events.csv"},
    )


@app.websocket("/ws")
async def ws(ws: WebSocket) -> None:
    global loop
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
    """Map a bkerler/DroneID macdb entry to a watchtower event."""
    mac, serial, pos, sysinfo, selfid = None, None, None, None, None
    for item in drone:
        if isinstance(item, dict):
            if "MAC" in item:
                mac = item["MAC"]
            bid = item.get("Basic ID") or {}
            uas = bid.get("id") or bid.get("uas_id")
            if uas:
                kind = str(bid.get("id_type", ""))
                if serial is None or "Serial" in kind:
                    serial = str(uas).strip()
            if "Location Vector" in item:
                c = item["Location Vector"].get("coord") or {}
                if c.get("latitude") or c.get("longitude"):
                    pos = {
                        "lat": c["latitude"] / 1e7,
                        "lon": c["longitude"] / 1e7,
                        "alt": (c.get("geodetic_altitude") or 0) / 1000,
                        "speed": c.get("speed", 0),
                        "dir": c.get("direction"),
                        "status": item["Location Vector"].get("op_status"),
                    }
            if "System" in item:
                s = item["System"]
                if s.get("latitude") or s.get("longitude"):
                    sysinfo = {"op_lat": s["latitude"] / 1e7, "op_lon": s["longitude"] / 1e7}
            if "Self ID" in item:
                selfid = item["Self ID"].get("text")
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
            "hex": (a.get("hex") or "").upper(),
            "flight": (a.get("flight") or a.get("callsign") or "").strip(),
            "type": a.get("aircraft_type") or a.get("category_label") or "",
            "category": a.get("category") or "",
            "alt_ft": a.get("alt_baro"),
            "gs_kt": a.get("gs"),
            "track": a.get("track"),
            "lat": a.get("lat"),
            "lon": a.get("lon"),
            "squawk": a.get("squawk") or "",
            "seen": a.get("seen"),
            "messages": a.get("messages"),
            "distance_nm": a.get("distance_nm"),
            "country": a.get("country") or "",
            "last_seen": a.get("lastSeen") or "",
        })
    return [c for c in craft if c["hex"]]


def hop_survey(freq_span: str, lo: float, hi: float, min_span: float, min_bursts: int, band: str) -> dict:
    """Burst-hop presence inside one band. No control-channel contents."""
    if not state.get("armed"):
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

    The sweep gate is only a trigger — Wi-Fi energy can satisfy it on its own.
    Once the demod has run, only verified ELRS packets make the row appear.
    """
    gate = elrs_survey()
    if not gate.get("present") or DEMOD is None:
        return {"present": False, "band": "2.4 GHz"}
    try:
        center = DEMOD.sweep_best_center()
        if center is None:
            return {"present": False, "band": "2.4 GHz"}
        iq_path = DEMOD.capture(center, 8)
        if not iq_path:
            return gate                            # radio busy; keep last word
        try:
            sig = DEMOD.load_baseband(iq_path)
        finally:
            os.remove(iq_path)
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
            return {"present": False, "band": "2.4 GHz"}   # energy, but not ELRS
        sf = max(counts, key=lambda k: counts[k])
        out = {
            "present": True,
            "band": "2.4 GHz",
            "modulation": f"LoRa SF{sf}",
            "rate": ELRS_RATES.get(sf, "ELRS"),
            "packets": sum(counts.values()),
            "snr_db": best_snr,
            "downlink": downs,
        }
        try:
            insert_event({
                "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "session": "elrs-2g4",
                "source": "elrs",
                "marker": f"ELRS {out['rate']}",
                "freq": round(center / 1e6, 1),
                "rssi": best_snr,
                "lat": None, "lon": None, "alt": None, "crc": True,
            })
        except Exception as exc:
            print(f"elrs history insert failed: {exc}", flush=True)
        return out
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
        pro_present = any(d["serial"] == HACKRF_DRONEID for d in usb_devices())
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


def adsb_loop() -> None:
    """Push the 1090 MHz picture to the dashboard. Failures just wait and retry."""
    while True:
        beat("adsb")
        try:
            craft = adsb_snapshot()
            state["adsb_ok"] = True
            state["adsb_error"] = None
        except Exception as exc:
            state["adsb_ok"] = False
            state["adsb_error"] = str(exc)
            time.sleep(5)
            continue
        broadcast({"type": "adsb", "aircraft": craft})
        time.sleep(2)


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


@app.on_event("startup")
def startup() -> None:
    init_db()
    ready = readiness()
    if ready["ok"]:
        print("watchtower ready: demods + core radios OK", flush=True)
    else:
        print("watchtower NOT ready:", "; ".join(ready["problems"]), flush=True)
    threading.Thread(target=dongle_loop, daemon=True).start()
    threading.Thread(target=gps_loop, daemon=True).start()
    threading.Thread(target=usb_cam_loop, daemon=True).start()
    threading.Thread(target=adsb_loop, daemon=True).start()
    threading.Thread(target=droneid_loop, daemon=True).start()
    threading.Thread(target=hop_loop, daemon=True).start()
    threading.Thread(target=mavlink_udp_loop, daemon=True).start()
    threading.Thread(target=retention_loop, daemon=True).start()
    endpoints = CONFIG.get("remoteid_zmq") or REMOTEID_ZMQ
    for endpoint in endpoints:
        threading.Thread(target=remoteid_loop, args=(endpoint,), daemon=True).start()


app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
