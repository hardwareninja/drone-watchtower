#!/usr/bin/env python3
"""Companion runner for a watchtower that is already receiving.

Optional, and quiet unless you ask:

  * 3D proximity between live DroneID / Remote ID tracks and ADS-B
  * TAK Cursor-on-Target UDP (unicast or multicast) for those tracks
  * a ZeroMQ PUB bus for the same events
  * ``--demo`` with no radio and no server
  * ``--bench`` for the offline ZCDetector and ChannelEstimator

Nothing here transmits, jams, or injects. CoT and ZMQ stay off when
``--cot-host`` and ``--zmq-bind`` are omitted. This is the only CoT
emitter in the tree; the web app does not open a second one.

The station token is sent as ``X-Watchtower-Token``, ``Authorization:
Bearer``, and the ``watchtower_token`` cookie. It is not placed in the
WebSocket URL, and it is not logged.

Usage:
  python scripts/watchtower_enhancer.py --demo
  python scripts/watchtower_enhancer.py --bench
  python scripts/watchtower_enhancer.py --ws-url ws://127.0.0.1:8080/ws \\
      --token "$WATCHTOWER_TOKEN" --zmq-bind tcp://127.0.0.1:4224
  python scripts/watchtower_enhancer.py --cot-host 239.2.3.1 --cot-port 6969
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("watchtower-enhancer")

EARTH_RADIUS_M = 6_371_000.0
FT_TO_M = 0.3048
M_TO_NM = 1.0 / 1852.0
# RID altitude and ADS-B alt_ft are not the same reference. The check is
# a warning aid, not a certified separation system.
ALTITUDE_NOTE = (
    "RID altitude and ADS-B alt_ft may differ (AGL versus baro or HAE). "
    "3D separation is a warning aid, not certified TCAS."
)
_PUBLIC_ZMQ = {"*", "0.0.0.0", "::", "[::]"}


@dataclass
class Track:
    track_id: str
    kind: str  # "droneid", "remoteid", "adsb"
    lat: float
    lon: float
    alt_m: float | None
    label: str = ""
    speed_mps: float = 0.0
    heading_deg: float = 0.0
    updated_at: float = field(default_factory=time.time)
    extra: dict[str, Any] = field(default_factory=dict)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle horizontal distance in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _finite(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def valid_position(lat, lon):
    """A usable fix. (0, 0) is the decoder's empty position, not a track."""
    lat_f, lon_f = _finite(lat), _finite(lon)
    if lat_f is None or lon_f is None:
        return None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return None
    if lat_f == 0.0 and lon_f == 0.0:
        return None
    return lat_f, lon_f


def build_cot_xml(track: Track, stale_s: int = 30, now: dt.datetime | None = None) -> bytes:
    """CoT 2.0 for one already-decoded track.

    Callsign, remarks, and uid go through ElementTree, which escapes
    attribute and text metacharacters. A broadcast string cannot close
    the event tag.
    """
    import xml.etree.ElementTree as ET

    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    stale = moment + dt.timedelta(seconds=stale_s)
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    if track.kind == "adsb":
        cot_type = "a-n-A-C"
        uid = f"ADSB-{track.track_id}"
    else:
        cot_type = "a-u-A-M-F-Q"
        uid = f"UAS-{track.track_id}"
    hae = f"{track.alt_m:.1f}" if track.alt_m is not None else "9999999.0"
    event = ET.Element(
        "event",
        {
            "version": "2.0",
            "uid": uid,
            "type": cot_type,
            "how": "m-g",
            "time": moment.strftime(fmt),
            "start": moment.strftime(fmt),
            "stale": stale.strftime(fmt),
        },
    )
    ET.SubElement(
        event,
        "point",
        {
            "lat": f"{track.lat:.6f}",
            "lon": f"{track.lon:.6f}",
            "hae": hae,
            "ce": "9999999.0",
            "le": "9999999.0",
        },
    )
    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "contact", {"callsign": track.label or uid})
    ET.SubElement(
        detail,
        "track",
        {"speed": f"{track.speed_mps:.1f}", "course": f"{track.heading_deg:.1f}"},
    )
    remarks = ET.SubElement(detail, "remarks")
    payload = {"note": ALTITUDE_NOTE}
    if track.extra:
        payload["fields"] = track.extra
    remarks.text = json.dumps(payload, separators=(",", ":"), default=str)
    return ET.tostring(event, encoding="utf-8", xml_declaration=True)


class CoTSender:
    """UDP CoT. Unicast or multicast. Created only when a host is set."""

    def __init__(self, host: str, port: int, ttl: int = 2) -> None:
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        first = host.split(".")[0] if host.replace(".", "").isdigit() else ""
        try:
            octet = int(first) if first else 0
        except ValueError:
            octet = 0
        if 224 <= octet <= 239:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)

    def send(self, payload: bytes) -> None:
        self.sock.sendto(payload, (self.host, self.port))

    def close(self) -> None:
        self.sock.close()


def zmq_endpoint(bind_addr: str) -> str:
    """Accept a localhost PUB bind. Refuse every-interface binds.

    Detections are geolocated. ``tcp://*:4224`` would publish them on
    every interface. The supported example is ``tcp://127.0.0.1:4224``.
    """
    addr = (bind_addr or "").strip()
    if not addr:
        return ""
    if not addr.startswith("tcp://"):
        raise ValueError("ZMQ bind must look like tcp://127.0.0.1:4224")
    host, sep, _port = addr[len("tcp://"):].rpartition(":")
    if not sep or host in _PUBLIC_ZMQ:
        raise ValueError(
            "Refusing to publish geolocated detections on all interfaces. "
            "Use tcp://127.0.0.1:4224"
        )
    return addr


class ZMQPublisher:
    """Optional ZeroMQ PUB. Not constructed unless a bind address is set."""

    def __init__(self, bind_addr: str) -> None:
        import zmq

        self.ctx = zmq.Context.instance()
        self.sock = self.ctx.socket(zmq.PUB)
        self.sock.bind(bind_addr)
        log.info("ZMQ PUB bound on %s", bind_addr)

    def publish(self, topic: str, data: dict[str, Any]) -> None:
        self.sock.send_multipart([
            topic.encode("utf-8"),
            json.dumps(data, default=str).encode("utf-8"),
        ])

    def close(self) -> None:
        self.sock.close(linger=0)


class ProximityMonitor:
    """Live drone and ADS-B tracks, and a configurable 3D separation check."""

    def __init__(
        self,
        horiz_warn_m: float = 3000.0,
        vert_warn_m: float = 304.8,
        track_ttl_s: float = 45.0,
    ) -> None:
        self.horiz_warn_m = horiz_warn_m
        self.vert_warn_m = vert_warn_m
        self.track_ttl_s = track_ttl_s
        self.drones: dict[str, Track] = {}
        self.aircraft: dict[str, Track] = {}

    def prune(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self.drones = {k: v for k, v in self.drones.items() if now - v.updated_at <= self.track_ttl_s}
        self.aircraft = {k: v for k, v in self.aircraft.items() if now - v.updated_at <= self.track_ttl_s}

    def update_drone(self, evt: dict[str, Any], kind: str) -> Track | None:
        pos = valid_position(evt.get("lat"), evt.get("lon"))
        if pos is None:
            return None
        lat, lon = pos
        source = str(evt.get("source") or "").lower()
        if source in ("droneid", "remoteid"):
            kind = source
        elif kind == "detection":
            kind = "droneid"
        tid = str(evt.get("session") or evt.get("mac") or "unknown")
        label = evt.get("model") or evt.get("selfid") or evt.get("session") or tid
        tr = Track(
            track_id=tid,
            kind=kind,
            lat=lat,
            lon=lon,
            alt_m=_finite(evt.get("alt")),
            label=str(label),
            speed_mps=_finite(evt.get("speed")) or 0.0,
            heading_deg=_finite(evt.get("dir") if evt.get("dir") is not None else evt.get("yaw")) or 0.0,
            updated_at=time.time(),
            extra={
                k: evt[k]
                for k in ("freq", "rssi", "marker", "pclass", "op_lat", "op_lon", "status", "operator")
                if k in evt and evt[k] is not None
            },
        )
        self.drones[tid] = tr
        return tr

    def update_adsb(self, ac: dict[str, Any]) -> Track | None:
        pos = valid_position(ac.get("lat"), ac.get("lon"))
        if pos is None:
            return None
        lat, lon = pos
        hex_id = str(ac.get("hex") or "unknown")
        alt_ft = _finite(ac.get("alt_ft"))
        gs_kt = _finite(ac.get("gs_kt")) or 0.0
        tr = Track(
            track_id=hex_id,
            kind="adsb",
            lat=lat,
            lon=lon,
            alt_m=None if alt_ft is None else alt_ft * FT_TO_M,
            label=str(ac.get("flight") or hex_id).strip() or hex_id,
            speed_mps=gs_kt * 0.514444,
            heading_deg=_finite(ac.get("track")) or 0.0,
            updated_at=time.time(),
            extra={
                k: ac[k]
                for k in ("type", "category", "squawk", "feed", "source")
                if k in ac and ac[k] is not None
            },
        )
        self.aircraft[hex_id] = tr
        return tr

    def check_conflicts(self, now: float | None = None) -> list[dict[str, Any]]:
        self.prune(now)
        alerts: list[dict[str, Any]] = []
        for drone in self.drones.values():
            for ac in self.aircraft.values():
                horiz = haversine_m(drone.lat, drone.lon, ac.lat, ac.lon)
                if horiz > self.horiz_warn_m:
                    continue
                vertical = None
                if drone.alt_m is not None and ac.alt_m is not None:
                    vertical = abs(drone.alt_m - ac.alt_m)
                    if vertical > self.vert_warn_m:
                        continue
                critical_vert = vertical is not None and vertical <= 152.4
                severity = "CRITICAL" if horiz <= 1000.0 and critical_vert else "WARNING"
                alerts.append({
                    "severity": severity,
                    "drone_id": drone.track_id,
                    "drone_label": drone.label,
                    "aircraft_hex": ac.track_id,
                    "aircraft_callsign": ac.label,
                    "horizontal_m": round(horiz, 1),
                    "horizontal_nm": round(horiz * M_TO_NM, 2),
                    "vertical_m": None if vertical is None else round(vertical, 1),
                    "vertical_ft": None if vertical is None else round(vertical / FT_TO_M, 0),
                    "vertical_unknown": vertical is None,
                    "altitude_note": ALTITUDE_NOTE,
                })
        return alerts


def connection_plan(ws_url: str, token: str | None) -> tuple[str, dict[str, str]]:
    """URL with any token query removed, plus header and cookie credentials.

    The returned URL is safe to log. The token is only in the headers.
    """
    parsed = urlparse(ws_url)
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() != "token"]
    clean = urlunparse(parsed._replace(query=urlencode(kept)))
    headers: dict[str, str] = {}
    if token:
        headers["X-Watchtower-Token"] = token
        headers["Authorization"] = f"Bearer {token}"
        headers["Cookie"] = f"watchtower_token={token}"
    return clean, headers


def process_message(msg: dict[str, Any], monitor: ProximityMonitor,
                    cot: CoTSender | None, zmq_pub: ZMQPublisher | None) -> list[dict[str, Any]]:
    """Apply one dashboard websocket message. Returns proximity alerts."""
    mtype = msg.get("type")
    emitted: list[Track] = []
    if mtype in ("detection", "remoteid"):
        evt = msg.get("event") or {}
        if not isinstance(evt, dict):
            evt = {}
        tr = monitor.update_drone(evt, kind=str(mtype))
        if tr:
            emitted.append(tr)
            if zmq_pub:
                zmq_pub.publish(f"watchtower.{mtype}", evt)
    elif mtype == "adsb":
        aircraft = msg.get("aircraft") or []
        if not isinstance(aircraft, list):
            aircraft = []
        for ac in aircraft:
            if not isinstance(ac, dict):
                continue
            tr = monitor.update_adsb(ac)
            if tr:
                emitted.append(tr)
        if zmq_pub:
            zmq_pub.publish("watchtower.adsb", {"count": len(emitted), "aircraft": aircraft})
    if cot:
        for tr in emitted:
            cot.send(build_cot_xml(tr))
    alerts = monitor.check_conflicts()
    for alert in alerts:
        vert = "unknown" if alert["vertical_unknown"] else f"{alert['vertical_ft']:.0f}ft"
        log.warning(
            "[%s] Proximity: UAS %s within %.0fm (%.2f NM) and %s of %s (%s). %s",
            alert["severity"],
            alert["drone_label"],
            alert["horizontal_m"],
            alert["horizontal_nm"],
            vert,
            alert["aircraft_callsign"],
            alert["aircraft_hex"],
            ALTITUDE_NOTE,
        )
        if zmq_pub:
            zmq_pub.publish("watchtower.proximity", alert)
    return alerts


def _connect(websockets, url: str, headers: dict[str, str]):
    import inspect
    params = inspect.signature(websockets.connect).parameters
    if headers and "additional_headers" in params:
        return websockets.connect(url, additional_headers=headers)
    if headers and "extra_headers" in params:
        return websockets.connect(url, extra_headers=headers)
    return websockets.connect(url)


async def run_live(args: argparse.Namespace) -> None:
    try:
        import websockets
    except ImportError:
        log.error("The websockets package is required for live mode (pip install websockets).")
        sys.exit(1)
    monitor = ProximityMonitor(args.horiz_warn_m, args.vert_warn_m)
    cot = CoTSender(args.cot_host, args.cot_port) if args.cot_host else None
    zmq_pub = ZMQPublisher(zmq_endpoint(args.zmq_bind)) if args.zmq_bind else None
    url, headers = connection_plan(args.ws_url, args.token)
    try:
        while True:
            try:
                log.info("Connecting to %s ...", url)
                async with _connect(websockets, url, headers) as ws:
                    log.info("Connected to Watchtower WebSocket.")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(msg, dict):
                            process_message(msg, monitor, cot, zmq_pub)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("WebSocket disconnected (%s); retrying in 3s...", type(exc).__name__)
                await asyncio.sleep(3)
    finally:
        if cot:
            cot.close()
        if zmq_pub:
            zmq_pub.close()


def run_demo(args: argparse.Namespace) -> None:
    """DroneID, Remote ID, and ADS-B shapes the dashboard actually sends."""
    log.info("Running self-contained enhancement demo...")
    monitor = ProximityMonitor(args.horiz_warn_m, args.vert_warn_m)
    cot = CoTSender(args.cot_host, args.cot_port) if args.cot_host else None
    zmq_pub = ZMQPublisher(zmq_endpoint(args.zmq_bind)) if args.zmq_bind else None
    sample_messages = [
        {
            "type": "remoteid",
            "event": {
                "source": "remoteid",
                "session": "1581F5FJD2300001",
                "model": "DJI Mavic 3",
                "lat": 22.3193,
                "lon": 114.1694,
                "alt": 120.0,
                "speed": 11.5,
                "dir": 85.0,
                "freq": 2437.0,
            },
        },
        {
            "type": "detection",
            "event": {
                "source": "droneid",
                "session": "1581F5FJD2300001",
                "lat": 22.3193,
                "lon": 114.1694,
                "alt": 120.0,
                "yaw": 85.0,
                "freq": 2437.0,
                "marker": "O2PT",
            },
        },
        {
            "type": "adsb",
            "aircraft": [
                {
                    "hex": "780A1B",
                    "flight": "HKSAR01",
                    "type": "EC175",
                    "lat": 22.3240,
                    "lon": 114.1750,
                    "alt_ft": 850,
                    "gs_kt": 110,
                    "track": 240,
                    "feed": "local",
                    "source": "1090",
                }
            ],
        },
    ]
    for msg in sample_messages:
        process_message(msg, monitor, cot, zmq_pub)
    if monitor.drones:
        sample = next(iter(monitor.drones.values()))
        log.info("Sample CoT XML output:\n%s", build_cot_xml(sample).decode("utf-8"))
    if cot:
        cot.close()
    if zmq_pub:
        zmq_pub.close()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run_bench() -> None:
    """Offline multipath check of src/ ZCDetector and ChannelEstimator."""
    src_dir = repo_root() / "src"
    if not src_dir.is_dir():
        log.error("src/ directory not found at %s.", src_dir)
        sys.exit(1)
    sys.path.insert(0, str(src_dir))
    import numpy as np
    from signal_processing.channel_estimator import ChannelEstimator
    from signal_processing.zc_detector import ZCDetector

    rng = np.random.default_rng(42)
    n_subcarriers = 64
    pilot_idx = np.arange(0, n_subcarriers, 8)
    tx_bits = rng.integers(0, 2, size=(n_subcarriers, 2))
    tx_syms = (1 - 2 * tx_bits[:, 0]) / np.sqrt(2) + 1j * (1 - 2 * tx_bits[:, 1]) / np.sqrt(2)
    tx_syms[pilot_idx] = 1.0 + 0.0j
    taps = np.array([0.85 + 0.1j, 0.35 - 0.25j, 0.15 + 0.12j])
    h_freq = np.fft.fft(taps, n=n_subcarriers)
    noise = 0.08 * (rng.standard_normal(n_subcarriers) + 1j * rng.standard_normal(n_subcarriers))
    rx_syms = h_freq * tx_syms + noise
    raw_bits = np.column_stack([(rx_syms.real < 0).astype(int), (rx_syms.imag < 0).astype(int)])
    raw_ber = float(np.mean(raw_bits != tx_bits))
    pattern = np.zeros(n_subcarriers, dtype=bool)
    pattern[pilot_idx] = True
    estimator = ChannelEstimator(pilot_pattern=pattern, window_size=5)
    h_est = estimator.estimate_channel(rx_syms, tx_syms[pilot_idx])
    eq_syms = estimator.equalize(rx_syms, h_est)
    eq_bits = np.column_stack([(eq_syms.real < 0).astype(int), (eq_syms.imag < 0).astype(int)])
    data = np.ones(n_subcarriers, dtype=bool)
    data[pilot_idx] = False
    eq_ber = float(np.mean(eq_bits[data] != tx_bits[data])) if data.any() else 0.0
    raw_data = float(np.mean(raw_bits[data] != tx_bits[data])) if data.any() else raw_ber
    ber_drop = ((raw_data - eq_ber) / raw_data * 100.0) if raw_data > 0 else 0.0
    detector = ZCDetector()
    log.info(
        "Multipath Bench -> Raw BER: %.4f | Equalized BER: %.4f | Reduction: %.1f%% | ZCDetector ready (%s)",
        raw_data,
        eq_ber,
        ber_drop,
        type(detector).__name__,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Companion enhancement runner for drone-watchtower")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8080/ws", help="Watchtower WebSocket URL")
    parser.add_argument("--token", default=os.environ.get("WATCHTOWER_TOKEN"), help="Watchtower API token (header and cookie, not the URL)")
    parser.add_argument("--cot-host", default="", help="TAK CoT UDP host. Omit to send no CoT.")
    parser.add_argument("--cot-port", type=int, default=6969, help="TAK CoT UDP port")
    parser.add_argument("--zmq-bind", default="", help="ZeroMQ PUB bind, e.g. tcp://127.0.0.1:4224. Omit to publish nothing.")
    parser.add_argument("--horiz-warn-m", type=float, default=3000.0, help="Horizontal proximity threshold (meters)")
    parser.add_argument("--vert-warn-m", type=float, default=304.8, help="Vertical proximity threshold (meters)")
    parser.add_argument("--demo", action="store_true", help="Offline demo with the live message schema")
    parser.add_argument("--bench", action="store_true", help="Offline src/ ZCDetector and ChannelEstimator check")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.zmq_bind:
        zmq_endpoint(args.zmq_bind)
    if args.bench:
        run_bench()
    elif args.demo:
        run_demo(args)
    else:
        asyncio.run(run_live(args))


if __name__ == "__main__":
    main()
