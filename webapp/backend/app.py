#!/usr/bin/env python3
"""Drone Watchtower — live detection webapp backend.

Runs on the Kali sensor box. Controls the luyii DroneID scanner as a
subprocess, tails its detections.jsonl, parses packets, and pushes events
over WebSocket to the frontend. Also serves the static frontend.

Usage: uvicorn app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import asyncio
import datetime
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

try:
    import zmq
    HAVE_ZMQ = True
except ImportError:
    HAVE_ZMQ = False

REMOTEID_ZMQ = ["tcp://127.0.0.1:4223", "tcp://127.0.0.1:4222", "tcp://127.0.0.1:4225"]  # wifi / ble / dji-beacon

BASE = Path(__file__).resolve().parent
BUILD = str(int(BASE.joinpath("app.py").stat().st_mtime))
FRONTEND = BASE.parent / "frontend"
RESULTS = Path("/home/drone/droneid-results")
ARCHIVE_SESSION = RESULTS / "live-162835" / "detections.jsonl"
CONFIG = json.loads((BASE / "config.json").read_text())

SCANNER = "/opt/droneid-repo/src/droneid_hackrf_scanner.py"
TURBO = "/opt/droneid-tools/remove_turbo_soft"
LIBHACKRF = "/usr/lib/aarch64-linux-gnu/libhackrf.so.0"

app = FastAPI(title="Drone Watchtower")

state = {
    "running": False,
    "mode": None,          # "live" | "replay" | None
    "proc": None,
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
        return {
            "time": j["event_time_utc"],
            "freq": j["frequency_mhz"],
            "marker": p[2:6].decode("ascii", "replace"),
            "session": p[6:10].hex(),
            "rssi": j.get("dwell_average_power_dbfs"),
            "crc": bool(j.get("crc24a_ok") and j.get("dji_crc16_ok")),
            "sha": (j.get("payload_sha256") or "")[:12],
            "mode": state["mode"],
        }
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


DB = Path("/opt/watchtower-web/data/history.db")
DB.parent.mkdir(parents=True, exist_ok=True)


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def insert_event(evt: dict) -> None:
    try:
        with db_conn() as c:
            c.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (evt.get("time"), evt.get("session"), evt.get("source"),
                 evt.get("marker"), evt.get("freq"), evt.get("rssi"),
                 evt.get("lat"), evt.get("lon"), evt.get("alt"),
                 1 if evt.get("crc") else 0),
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


def publish_event(evt: dict) -> None:
    with lock:
        state["events"] += 1
    if evt.get("source") == "remoteid":
        model = CONFIG.get("aircraft", {}).get(evt.get("session"))
        if model:
            evt["model"] = model
    insert_event(evt)
    typ = "remoteid" if evt.get("source") == "remoteid" else "detection"
    broadcast({"type": typ, "event": evt})


def status_dict() -> dict:
    return {
        "running": state["running"],
        "mode": state["mode"],
        "events": state["events"],
        "started": state["started"],
        "station": CONFIG["station"],
        "archive_available": ARCHIVE_SESSION.exists(),
        "build": BUILD,
    }


def stop_everything() -> None:
    proc = state.get("proc")
    if proc and proc.poll() is None:
        proc.send_signal(2)  # SIGINT — scanner clean shutdown
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    state.update(running=False, mode=None, proc=None)


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
                    publish_event(evt)
            seen = len(lines)
        time.sleep(0.4)


def replay_loop() -> None:
    lines = ARCHIVE_SESSION.read_text().splitlines()
    for line in lines:
        if state["mode"] != "replay":
            return
        evt = parse_line(line)
        if evt and evt["crc"]:
            publish_event(evt)
        time.sleep(0.15)
    state.update(running=False, mode=None)
    broadcast({"type": "status", "status": status_dict()})


@app.post("/api/scan/start")
def scan_start(band: str = "5g", lna: int = 32, vga: int = 24) -> dict:
    if state["running"]:
        return {"ok": False, "error": "already running"}
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = RESULTS / f"web-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    cmd = [
        "python3", SCANNER,
        "--band", band, "--preset", "baseline",
        "--lna", str(lna), "--vga", str(vga),
        "--libhackrf", LIBHACKRF,
        "--turbo-binary", TURBO,
        "--output", str(out),
    ]
    log = open(out / "scanner.log", "w")
    state.update(running=True, mode="live", proc=None, run_dir=out,
                 events=0, started=time.time())
    state["proc"] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    threading.Thread(target=tail_loop, daemon=True).start()
    broadcast({"type": "status", "status": status_dict()})
    return {"ok": True, "run": str(out)}


@app.post("/api/scan/stop")
def scan_stop() -> dict:
    stop_everything()
    broadcast({"type": "status", "status": status_dict()})
    return {"ok": True}


@app.post("/api/replay/start")
def replay_start() -> dict:
    if state["running"]:
        return {"ok": False, "error": "already running"}
    if not ARCHIVE_SESSION.exists():
        return {"ok": False, "error": "archive session not found"}
    state.update(running=True, mode="replay", events=0, started=time.time())
    threading.Thread(target=replay_loop, daemon=True).start()
    broadcast({"type": "status", "status": status_dict()})
    return {"ok": True}


@app.get("/api/status")
def status() -> dict:
    return status_dict()


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


@app.get("/api/history/events")
def history_events(session: str, limit: int = 500) -> list:
    with db_conn() as c:
        rows = c.execute(
            "SELECT * FROM events WHERE session=? ORDER BY time DESC LIMIT ?",
            (session, limit),
        ).fetchall()
    return [dict(r) for r in rows]


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
            if bid.get("id") and "Serial" in str(bid.get("id_type", "")):
                serial = bid["id"]
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
    if not ident or not pos:
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
    evt.update(pos)
    if sysinfo:
        evt.update(sysinfo)
    return evt


def remoteid_loop(endpoint: str) -> None:
    if not HAVE_ZMQ:
        return
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt_string(zmq.SUBSCRIBE, "")
    sub.connect(endpoint)
    while True:
        try:
            raw = sub.recv_string()
            data = json.loads(raw)
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
    for endpoint in REMOTEID_ZMQ:
        threading.Thread(target=remoteid_loop, args=(endpoint,), daemon=True).start()


app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
