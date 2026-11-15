#!/usr/bin/env python3
"""Flag ADS-B tracks that look like small unmanned aircraft.

Most civil traffic is easy to rule out: it has a callsign, a registration,
a known airframe type, and it flies at airliner speeds and altitudes. A
small UAS tends to break several of those expectations at once, and when
the aircraft also does not broadcast an ICAO identity properly the picture
gets stranger still.

This is a scoring heuristic, not a classifier with a training set. It
produces a score and the list of signals that produced it, so the
dashboard can show *why* a track was flagged and a human can judge. The
bar for a flag is deliberately set at the level where several independent
signals agree — one odd attribute on its own is normal traffic.
"""
from __future__ import annotations

# Weights sum to 100 in the "everything wrong" case. A track needs at
# least UAS_SCORE_THRESHOLD to be flagged.
WEIGHTS = {
    "no_callsign": 22,
    "no_registration": 16,
    "no_icao_identity": 20,
    "unknown_type": 14,
    "low_altitude": 12,
    "low_speed": 10,
    "light_category": 8,
    "hovering": 12,
}

# A track needs this many points before it is called a candidate.
UAS_SCORE_THRESHOLD = 45

# Altitude and speed ceilings. Small UAS fly low and slow; airliners and
# helicopters do not. These are generous on purpose.
LOW_ALTITUDE_FT = 1500
LOW_SPEED_KT = 120
HOVER_SPEED_KT = 25

# ADS-B emitter categories that describe light or small aircraft.
# The ICAO "A" set is graduated by weight: A1 is 15,000 kg or less, A2 is
# up to 75,000 kg, A3 is 75,000 kg or more. Only A1 is a genuine "light"
# signal. A4 through A7 are the "no category information" values, and
# airliners routinely carry them, so they must not be read as small.
LIGHT_CATEGORIES = {"A1"}

# The "B" set is ground vehicles and the "C" set is surface vehicles. These
# are not aircraft, so they earn no light-category bonus. They are not
# treated as a rule-out either: a track in these categories that carries no
# ICAO identity, no callsign and no registration is still worth a look,
# because that combination is exactly what a non-ICAO transmitter looks
# like.
SURFACE_CATEGORIES = {
    "B0", "B1", "B2", "B3", "B4", "B5", "B6", "B7",
    "C0", "C1", "C2", "C3", "C4", "C5", "C6", "C7",
}

# Aircraft types that are never a small UAS.
KNOWN_TYPES = {
    "A20N", "A21N", "A319", "A320", "A321", "A332", "A333", "A339", "A359",
    "A388", "B38M", "B733", "B734", "B735", "B736", "B737", "B738", "B739",
    "B744", "B748", "B752", "B753", "B762", "B763", "B764", "B772", "B77L",
    "B77W", "B788", "B789", "B78X", "E135", "E145", "E170", "E175", "E190",
    "E195", "GLF4", "GLF5", "GLF6", "H135", "H145", "PC12", "SR20", "C172",
    "C182", "C206", "C25A", "C25B", "C25C", "C56X", "DH8D", "DH8E",
    "MD11", "MD80", "MD82", "MD83", "MD87", "MD88", "MD90", "PA31", "PA32",
    "SB20", "SF34", "SJY", "JS31", "JS41", "AT43", "AT72", "ATP", "SH6",
    "A306", "A310", "B462", "B463", "B712", "B762",
}

# DO-260B emitter category A7 = Rotorcraft. FR24 also uses category code H.
ROTORCRAFT_CATEGORIES = {"A7", "H"}

# Common ICAO type designators for helicopters / tiltrotors.
HELICOPTER_TYPES = {
    "A109", "A139", "A169", "A189", "AS50", "AS55", "AS65", "AW09", "AW39",
    "B06", "B06T", "B407", "B412", "B427", "B429", "B430", "B505",
    "CH47", "EC20", "EC25", "EC30", "EC35", "EC45", "EC55", "EC75",
    "H120", "H125", "H130", "H135", "H145", "H155", "H160", "H175", "H225",
    "KA32", "MI8", "MI17", "MI24", "R22", "R44", "R66", "S76", "S92",
    "UH1", "UH60", "MH60", "V22", "EC135", "EC145", "BK117", "BO105",
}

HELI_LABEL_KEYWORDS = ("HELI", "ROTOR", "GYROCOP", "GYROPLANE", "TILTROTOR")


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(value) -> str:
    return str(value or "").strip()


def has_icao_identity(hex_code: str) -> bool:
    """True when the hex is a proper 24-bit ICAO address.

    Non-ICAO transmitters emit values like "~7b1004" or "0x7b1004" — the
    prefix means the address was reconstructed from a non-ICAO source and
    is not a real allocated ICAO hex.
    """
    code = _text(hex_code)
    if not code or code.startswith(("~", "0x")) or not code.isalnum():
        return False
    if len(code) != 6:
        return False
    try:
        int(code, 16)
    except ValueError:
        return False
    return True


def classify_helicopter(track: dict) -> dict:
    """True when the ADS-B track is a helicopter / rotorcraft.

    Uses emitter category A7 (rotorcraft), known heli type designators, and
    label keywords such as HELI / ROTOR.
    """
    reasons: list[str] = []
    category = _text(track.get("category")).upper()
    aircraft_type = _text(track.get("type")).upper().replace("-", "").replace(" ", "")
    # Keep a display-friendly type for messages.
    type_raw = _text(track.get("type")).upper()
    label = " ".join([
        type_raw,
        _text(track.get("category_label")).upper(),
        _text(track.get("desc")).upper(),
    ])

    if category in ROTORCRAFT_CATEGORIES:
        reasons.append(f"emitter category {category} (rotorcraft)")

    if aircraft_type in HELICOPTER_TYPES or type_raw in HELICOPTER_TYPES:
        reasons.append(f"type {type_raw or aircraft_type}")

    if any(k in label for k in HELI_LABEL_KEYWORDS):
        hit = next(k for k in HELI_LABEL_KEYWORDS if k in label)
        reasons.append(f"label contains {hit}")

    return {
        "helicopter": bool(reasons),
        "helicopter_flags": reasons,
    }


def classify(track: dict) -> dict:
    """Score one ADS-B track for small-UAS characteristics and helicopters.

    Returns UAS scoring fields plus ``helicopter`` / ``helicopter_flags``.
    A UAS score below the threshold carries an empty UAS flag list.
    """
    score = 0
    flags: list[str] = []

    callsign = _text(track.get("flight"))
    registration = _text(track.get("registration"))
    aircraft_type = _text(track.get("type")).upper()
    category = _text(track.get("category")).upper()
    alt_ft = _number(track.get("alt_ft"))
    gs_kt = _number(track.get("gs_kt"))
    heli = classify_helicopter(track)

    if not callsign:
        score += WEIGHTS["no_callsign"]
        flags.append("no callsign")

    if not registration:
        score += WEIGHTS["no_registration"]
        flags.append("no registration")

    if not has_icao_identity(_text(track.get("hex"))):
        score += WEIGHTS["no_icao_identity"]
        flags.append("no ICAO identity")

    if not aircraft_type or aircraft_type not in KNOWN_TYPES:
        score += WEIGHTS["unknown_type"]
        flags.append("unknown type")

    if alt_ft is not None and alt_ft < LOW_ALTITUDE_FT:
        score += WEIGHTS["low_altitude"]
        flags.append(f"low altitude {int(alt_ft)} ft")

    if gs_kt is not None and gs_kt < LOW_SPEED_KT:
        score += WEIGHTS["low_speed"]
        flags.append(f"low speed {int(gs_kt)} kt")

    if category in LIGHT_CATEGORIES:
        score += WEIGHTS["light_category"]
        flags.append(f"light category {category}")

    if gs_kt is not None and gs_kt < HOVER_SPEED_KT:
        score += WEIGHTS["hovering"]
        flags.append(f"near-hover {int(gs_kt)} kt")

    # Helicopters are crewed rotorcraft — never treat them as small-UAS.
    flagged = (score >= UAS_SCORE_THRESHOLD) and not heli["helicopter"]
    return {
        "uas_score": score,
        "uas_flags": flags if flagged else [],
        "uas_candidate": flagged,
        "helicopter": heli["helicopter"],
        "helicopter_flags": heli["helicopter_flags"],
    }
