"""ESP32-C5 Remote ID channel plan and health, with no radio I/O.

The firmware in firmware/esp32c5-rid is the receiver. This module is the
same plan on the host side: which Wi-Fi channels are 2.4 GHz, which are
5 GHz, which of those are DFS, and how a firmware stats line becomes the
green/red band dots. HackRF 5.8 GHz DroneID and analog FPV are not Remote
ID and are not classified here.
"""
from __future__ import annotations

import time

# 2.4 GHz beacons. Channel 6 is where many DJI aircraft park.
WIFI24_CHANNELS = (1, 6, 11)

# 5 GHz non-DFS. Channel 149 is the common UNII-3 Remote ID channel.
WIFI5_NON_DFS = (36, 40, 44, 48, 149, 153, 157, 161, 165)

# DFS (UNII-2A and UNII-2C). The C5 may refuse a channel; the firmware
# skips a refused set and does not transmit to satisfy it.
WIFI5_DFS = (
    52, 56, 60, 64,
    100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
)

FRESH_S = 8.0


def channel_mhz(channel: int) -> float | None:
    """Center frequency for a Wi-Fi channel number, or None if it is not one."""
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        return None
    if 1 <= ch <= 13:
        return 2407.0 + ch * 5
    if ch == 14:
        return 2484.0
    if 32 <= ch <= 177:
        return 5000.0 + ch * 5
    return None


def is_dfs(channel: int) -> bool:
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        return False
    return 52 <= ch <= 144


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _opt_bool(obj: dict, key: str):
    if key not in obj or obj.get(key) is None:
        return None
    return bool(obj.get(key))


def wifi5_from_config(raw) -> bool:
    """5 GHz coverage from config.json. Missing or false stays off."""
    if not isinstance(raw, dict):
        return False
    return bool(raw.get("c5_wifi5"))


def wifi5_command(enabled: bool) -> bytes:
    """One line the bridge writes to the C5. This does not key the radio."""
    return b"wifi5=1\n" if enabled else b"wifi5=0\n"


def health_from_stats(obj: dict, now: float | None = None) -> dict:
    """Normalize one firmware stats object.

    ``band`` is the single place the radio is tuned: ``24``, ``5``, or
    ``ble``. A heartbeat without ``band`` is legacy and must not be read
    as two Wi-Fi bands up at once.
    """
    if not isinstance(obj, dict):
        obj = {}
    legacy = "band" not in obj
    band = str(obj.get("band") or "").strip()
    if band not in ("24", "5", "ble"):
        band = ""
    wifi24 = obj.get("wifi24")
    if wifi24 is None and legacy:
        wifi24 = obj.get("wifi") or 0
    when = now
    if when is None:
        try:
            when = float(obj["t"]) if obj.get("t") is not None else time.time()
        except (TypeError, ValueError):
            when = time.time()
    dfs = obj.get("dfs")
    if dfs is None:
        dfs = obj.get("dfs_ok")
    return {
        "t": when,
        "legacy": legacy,
        "band": band,
        "ble": _int(obj.get("ble")),
        "wifi24": _int(wifi24),
        "wifi5": _int(obj.get("wifi5")),
        "nan": _int(obj.get("nan")),
        "wifi5_on": _opt_bool(obj, "wifi5_on"),
        "dfs": None if dfs is None else bool(dfs),
    }


_NOTE = "One C5 radio is on one band at a time. The others are not receiving."


def band_status(attached: bool, health: dict | None, now: float,
                wifi5_configured: bool = False) -> dict:
    """Dots for the C5. At most one of 2.4, 5 GHz, and BLE is ``ok``.

    ``ok`` means the radio is on that band now. ``away`` means coverage
    exists but the radio is elsewhere. ``off`` means 5 GHz coverage is
    not enabled. ``down`` means the dongle is attached with no fresh
    heartbeat. ``offline`` means it is not attached.
    """
    health = health or {}
    age = None
    if health.get("t") is not None:
        try:
            age = now - float(health["t"])
        except (TypeError, ValueError):
            age = None
    fresh = age is not None and 0 <= age <= FRESH_S
    if health.get("wifi5_on") is None:
        enabled = bool(wifi5_configured)
    else:
        enabled = bool(health.get("wifi5_on"))
    band = health.get("band") or ""
    if not attached:
        wifi24, wifi5, ble = "offline", "offline", "offline"
    elif not fresh:
        wifi24, ble = "down", "down"
        wifi5 = "off" if not enabled else "down"
    elif not band:
        wifi24, wifi5, ble = "away", "off", "away"
    else:
        wifi24, ble = "away", "away"
        wifi5 = "away" if enabled else "off"
        if band == "24":
            wifi24 = "ok"
        elif band == "5":
            wifi5 = "ok"
        elif band == "ble":
            ble = "ok"

    def detail(state: str, listening: str, idle: str) -> str:
        if state == "offline":
            return "C5 not attached. " + _NOTE
        if state == "off":
            return (
                "5 GHz Wi-Fi Remote ID is off (c5_wifi5). "
                "The radio stays on 2.4 GHz and BLE, one at a time. "
                "HackRF 5.8 GHz DroneID and video are not Remote ID."
            )
        if state == "down":
            return "C5 is attached but there is no fresh band heartbeat. " + _NOTE
        if state == "ok":
            return listening + " " + _NOTE
        return idle + " " + _NOTE

    dfs_note = ""
    if wifi5 == "ok" and health.get("dfs"):
        dfs_note = " This dwell is a DFS channel."
    elif wifi5 == "ok":
        dfs_note = " Channel 149 and the other 5 GHz channels are visited one at a time."
    return {
        "wifi24": wifi24,
        "wifi5": wifi5,
        "ble": ble,
        "band": band,
        "wifi5_on": enabled,
        "age_s": None if age is None else round(age, 1),
        "legacy": bool(health.get("legacy")),
        "nan": int(health.get("nan") or 0),
        "dfs": health.get("dfs"),
        "detail": {
            "wifi24": detail(
                wifi24,
                "C5 is receiving 2.4 GHz Wi-Fi Remote ID now.",
                "C5 is on another band, so 2.4 GHz Wi-Fi is not receiving.",
            ),
            "wifi5": detail(
                wifi5,
                "C5 is receiving 5 GHz Wi-Fi beacon/NAN Remote ID now. "
                "2.4 GHz packets are missed until it hops back." + dfs_note,
                "C5 is on another band, so 5 GHz Wi-Fi is not receiving.",
            ),
            "ble": detail(
                ble,
                "C5 is receiving BLE Remote ID now. Wi-Fi is paused.",
                "C5 is on Wi-Fi, so BLE is not receiving.",
            ),
        },
    }


def event_from_firmware(obj: dict, when: str) -> dict | None:
    """Map one firmware detection line to a watchtower Remote ID event.

    The status string names the Wi-Fi band or BLE. A 5 GHz Wi-Fi hit,
    including channel 165 at 5825 MHz, stays Remote ID. It is not DroneID
    and it is not analog FPV.
    """
    if not isinstance(obj, dict):
        return None
    serial = str(obj.get("serial") or "").strip()
    if not serial:
        return None
    via = str(obj.get("via") or "wifi").strip().lower()
    try:
        ch = int(obj.get("ch") or 0)
    except (TypeError, ValueError):
        ch = 0
    freq, status = _freq_status(via, ch)

    def fnum(key: str) -> float:
        try:
            return float(obj.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    try:
        rssi = int(obj.get("rssi") or 0)
    except (TypeError, ValueError):
        rssi = 0
    evt = {
        "source": "remoteid",
        "time": when,
        "session": serial,
        "marker": "RemoteID",
        "mac": str(obj.get("mac") or ""),
        "crc": True,
        "freq": freq,
        "rssi": rssi,
        "lat": fnum("lat"),
        "lon": fnum("lon"),
        "alt": 0.0,
        "speed": 0,
        "status": status,
    }
    op_lat, op_lon = fnum("op_lat"), fnum("op_lon")
    if op_lat or op_lon:
        evt["op_lat"] = op_lat
        evt["op_lon"] = op_lon
    return evt


def _freq_status(via: str, ch: int) -> tuple[float, str]:
    mhz = channel_mhz(ch)
    if via == "bt4":
        return 2402.0, "bt4 legacy"
    band = "5" if ch >= 32 else "2.4"
    if via == "nan":
        fallback = 5745.0 if ch >= 32 else 2437.0
        return (mhz if mhz is not None else fallback), f"c5 nan {band}"
    if mhz is None:
        return 2437.0, "c5 beacon"
    if ch >= 32:
        return mhz, "c5 beacon 5"
    return mhz, "c5 beacon 2.4"
