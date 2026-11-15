"""Charset allowlists for text that arrives over the air.

Every string a receiver decodes is chosen by whoever is on the air: an
ADS-B callsign, a Remote ID serial, a MAVLink STATUSTEXT. Those strings
reach the dashboard, so they must be constrained at the trust boundary
rather than escaped at each render site.

Rule: keep the charset narrow enough that the value cannot carry markup,
but wide enough that the real value survives. Legitimate ADS-B callsigns
are letters, digits, spaces and a few punctuation marks; the ICAO hex is
exactly six hex digits.
"""

from __future__ import annotations

import re

# Callsigns and free-text labels seen in ADS-B feeds.
_LABEL_ALLOWED = re.compile(r"[^A-Za-z0-9 .\-_/&+()'\"~]")
# ICAO 24-bit address: exactly six hex digits.
_HEX_ALLOWED = re.compile(r"[^0-9A-F]")

MAX_LABEL_LEN = 64
MAX_HEX_LEN = 6


def safe_label(value, max_len: int = MAX_LABEL_LEN) -> str:
    """Constrain a free-text field to a markup-free charset.

    Control characters are dropped, the rest is filtered to the allowed
    set, and the result is length-capped. Returns "" when nothing
    printable survives.

    ``~`` is allowed because the local receiver marks a non-ICAO
    transmitter as ``~7b1004`` and downstream code tests for that prefix.
    """
    if value is None:
        return ""
    text = str(value)
    # Drop control characters outright (a callsign may legitimately
    # contain none, and they are the easiest way to break a terminal).
    text = "".join(ch for ch in text if ch.isprintable())
    text = _LABEL_ALLOWED.sub("", text).strip()
    return text[:max_len]


def safe_hex(value) -> str:
    """Constrain an ICAO address to uppercase hex digits.

    Anything that is not a hex digit is dropped, so a spoofed "hex" field
    cannot carry a payload into the dashboard.

    A leading ``~`` is preserved: the local receiver uses it to mark an
    address reconstructed from a non-ICAO source, and ``adsb_classify``
    depends on seeing it.
    """
    if value is None:
        return ""
    text = str(value).strip().upper()
    non_icao = text.startswith("~")
    if non_icao:
        text = text[1:]
    text = _HEX_ALLOWED.sub("", text)
    return ("~" + text[:MAX_HEX_LEN]) if non_icao else text[:MAX_HEX_LEN]
