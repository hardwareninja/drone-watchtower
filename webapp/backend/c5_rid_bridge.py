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
import time

import zmq

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
        print(f"stats ble={obj.get('ble')} wifi={obj.get('wifi')}", flush=True)
        return
    serial = obj.get("serial") or ""
    if not serial:
        return
    via = obj.get("via") or "wifi"
    ch = int(obj.get("ch") or 0)
    if via == "bt4":
        freq = 2402.0
        status = "bt4 legacy"
    elif 1 <= ch <= 14:
        freq = 2407 + ch * 5
        status = "c5 beacon"
    else:
        freq = 5000 + ch * 5 if ch else 2437.0
        status = "c5 beacon"
    evt = {
        "source": "remoteid",
        "time": stamp(),
        "session": serial,
        "marker": "RemoteID",
        "mac": obj.get("mac") or "",
        "crc": True,
        "freq": float(freq),
        "rssi": obj.get("rssi") or 0,
        "lat": float(obj.get("lat") or 0),
        "lon": float(obj.get("lon") or 0),
        "alt": 0.0,
        "speed": 0,
        "status": status,
    }
    pub.send_string(json.dumps({"watchtower_event": evt}))
    print(f"[{evt['time']}] {serial} ch{ch} rssi={evt['rssi']} ({evt['lat']:.6f},{evt['lon']:.6f})", flush=True)


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
            buf = b""
            while True:
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
