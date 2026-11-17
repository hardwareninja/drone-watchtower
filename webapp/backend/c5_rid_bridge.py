#!/usr/bin/env python3
"""Read Remote ID JSON lines from the ESP32-C5 and publish them to Watchtower.

The firmware prints one object per detection on the USB serial port:
  {"src":"c5","serial":"...","lat":..,"lon":..,"rssi":..,"ch":..,"mac":".."}
This process stamps the time and publishes {"watchtower_event": ...} on
tcp://127.0.0.1:4226, which the webapp already subscribes to.
"""
import fcntl
import json
import os
import select
import time
from pathlib import Path

import zmq

import c5_rid
import usb_locate

C5_VENDOR = "303a"
C5_PRODUCT = "1001"
ZMQ_OUT = "tcp://127.0.0.1:4226"

TIOCMGET = 0x5415
TIOCMSET = 0x5418
TIOCM_DTR = 0x002
TIOCM_RTS = 0x004

pub = zmq.Context.instance().socket(zmq.PUB)
pub.bind(ZMQ_OUT)


def stamp() -> str:
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now * 1000) % 1000:03d}+00:00"


def publish(obj: dict) -> None:
    if obj.get("raw"):
        print(f"raw rssi={obj.get('rssi')} {obj.get('raw')}", flush=True)
        return
    if obj.get("stats"):
        health = c5_rid.health_from_stats(obj)
        pub.send_string(json.dumps({"c5_health": health}))
        print(
            f"stats band={health['band'] or '-'} wifi5_on={health['wifi5_on']} "
            f"ble={health['ble']} wifi24={health['wifi24']} "
            f"wifi5={health['wifi5']} nan={health['nan']}",
            flush=True,
        )
        return
    evt = c5_rid.event_from_firmware(obj, stamp())
    if not evt:
        return
    pub.send_string(json.dumps({"watchtower_event": evt}))
    ch = obj.get("ch") or 0
    print(
        f"[{evt['time']}] {evt['session']} {evt['status']} ch{ch} "
        f"rssi={evt['rssi']} ({evt['lat']:.6f},{evt['lon']:.6f})",
        flush=True,
    )


def load_wifi5() -> bool:
    """config.json c5_wifi5. Absent, unreadable, or false means stay on 2.4."""
    path = Path(__file__).with_name("config.json")
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return c5_rid.wifi5_from_config(raw)


def find_port() -> str | None:
    """ESP32-C5 USB-Serial-JTAG, on any bus and any tty number."""
    return usb_locate.tty_for(C5_VENDOR, C5_PRODUCT)


def open_port(path: str):
    import termios
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    attr = termios.tcgetattr(fd)
    attr[0] = 0
    attr[1] = 0
    attr[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attr[3] = 0
    attr[4] = attr[5] = termios.B115200
    attr[6][termios.VMIN] = 1
    attr[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attr)

    def set_modem(dtr: bool, rts: bool) -> None:
        bits = array_int(0)
        fcntl.ioctl(fd, TIOCMGET, bits, True)
        flags = bits[0] & ~(TIOCM_DTR | TIOCM_RTS)
        if dtr:
            flags |= TIOCM_DTR
        if rts:
            flags |= TIOCM_RTS
        bits[0] = flags
        fcntl.ioctl(fd, TIOCMSET, bits, True)

    try:
        set_modem(False, True)    # EN low — reset
        time.sleep(0.05)
        set_modem(False, False)   # run firmware, not download mode
    except OSError:
        pass
    return os.fdopen(fd, "rb", buffering=0)


def array_int(value: int):
    import array
    return array.array("I", [value])


def main() -> None:
    print(f"C5 bridge: auto ttyACM -> {ZMQ_OUT}", flush=True)
    while True:
        path = find_port()
        if not path:
            print("serial closed: C5 ACM not found", flush=True)
            time.sleep(2)
            continue
        try:
            print(f"C5 bridge: opening {path}", flush=True)
            fp = open_port(path)
            fd = fp.fileno()
            buf = b""
            last_cmd = 0.0
            while True:
                now = time.time()
                if now - last_cmd >= 5:
                    os.write(fd, c5_rid.wifi5_command(load_wifi5()))
                    last_cmd = now
                ready, _, _ = select.select([fp], [], [], 1.0)
                if not ready:
                    continue
                chunk = fp.read(256)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"{"):
                        continue
                    try:
                        publish(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            fp.close()
        except OSError as exc:
            print(f"serial closed: {exc}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
