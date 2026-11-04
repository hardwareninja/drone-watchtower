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

REMOTEID_ZMQ = "tcp://127.0.0.1:4223"

BASE = Path(__file__).resolve().parent
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


def broadcast(msg: dict) -> None:
    if loop is None or not clients:
        return
    data = json.dumps(msg)
    for ws in list(clients):
        asyncio.run_coroutine_threadsafe(ws.send_text(data), loop)


def publish_event(evt: dict) -> None:
    with lock:
        state["events"] += 1
    broadcast({"type": "detection", "event": evt})


def status_dict() -> dict:
    return {
        "running": state["running"],
        "mode": state["mode"],
        "events": state["events"],
        "started": state["started"],
        "station": CONFIG["station"],
        "archive_available": ARCHIVE_SESSION.exists(),
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


def remoteid_loop() -> None:
    if not HAVE_ZMQ:
        return
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt_string(zmq.SUBSCRIBE, "")
    sub.connect(REMOTEID_ZMQ)
    while True:
        try:
            raw = sub.recv_string()
            for drone in json.loads(raw):
                evt = remoteid_event(drone)
                if evt:
                    publish_event(evt)
        except json.JSONDecodeError:
            continue
        except Exception:
            time.sleep(1)


@app.on_event("startup")
def startup() -> None:
    threading.Thread(target=remoteid_loop, daemon=True).start()


app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
