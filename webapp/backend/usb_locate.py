#!/usr/bin/env python3
"""Find Watchtower radios by USB identity, not by which port they enumerated on."""
from __future__ import annotations

from pathlib import Path

WIFI_DRIVERS = {
    "ath9k_htc", "carl9170", "rt2800usb", "rtl8187", "rtl8xxxu",
    "mt7601u", "mt76x0u", "mt76x2u", "mt7921u",
}
# Sonoff / CC2652 Sniffle sticks. The u-blox and ESP32-C5 are ACM, not these.
BLE_IDS = (("10c4", "ea60"), ("1a86", "7523"))


def usb_ids(start: Path) -> tuple[str, str] | None:
    path = start.resolve()
    for _ in range(10):
        vendor = path / "idVendor"
        product = path / "idProduct"
        if vendor.exists() and product.exists():
            try:
                return vendor.read_text().strip().lower(), product.read_text().strip().lower()
            except OSError:
                return None
        if path.parent == path:
            break
        path = path.parent
    return None


def tty_for(vendor: str, product: str) -> str | None:
    """CDC ACM or USB-serial node for this vendor:product, on any bus address."""
    root = Path("/sys/class/tty")
    if not root.exists():
        return None
    want = (vendor.lower(), product.lower())
    nodes = sorted(root.glob("ttyACM*")) + sorted(root.glob("ttyUSB*"))
    for node in nodes:
        device = node / "device"
        if device.exists() and usb_ids(device) == want:
            return "/dev/" + node.name
    return None


def wifi_iface() -> str | None:
    """Monitor-capable USB Wi-Fi adapter, whatever name udev gave it."""
    root = Path("/sys/class/net")
    if not root.exists():
        return None
    for node in sorted(root.iterdir()):
        driver = node / "device" / "driver"
        if not driver.exists():
            continue
        try:
            if driver.resolve().name in WIFI_DRIVERS:
                return node.name
        except OSError:
            continue
    return None


def ble_port() -> str | None:
    for vendor, product in BLE_IDS:
        path = tty_for(vendor, product)
        if path:
            return path
    return None


def uvc_index() -> int | None:
    """USB UVC capture node. Metadata nodes that share the camera are skipped."""
    root = Path("/sys/class/video4linux")
    if not root.exists():
        return None
    best = None
    for node in root.glob("video*"):
        driver = node / "device" / "driver"
        if not driver.exists():
            continue
        try:
            if driver.resolve().name != "uvcvideo":
                continue
            label = (node / "name").read_text().strip().lower() if (node / "name").exists() else ""
        except OSError:
            continue
        if "metadata" in label:
            continue
        try:
            index = int(node.name.removeprefix("video"))
        except ValueError:
            continue
        if best is None or index < best:
            best = index
    return best
