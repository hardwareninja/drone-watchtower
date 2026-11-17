"""CSV, KML, and GPX renderers for stored detection rows.

Text columns are constrained and then encoded for the format. A spreadsheet
formula and an XML tag both have to survive a row that was stored before
that constraint existed.
"""
from __future__ import annotations

import csv
import io

import textsan

TEXT_FIELDS = ("session", "source", "marker", "pclass")


def public_row(row: dict) -> dict:
    out = dict(row)
    for key in TEXT_FIELDS:
        if key in out and out[key] is not None and not isinstance(out[key], (int, float, bool)):
            out[key] = textsan.safe_label(out[key])
    return out


def csv_document(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow([textsan.csv_cell(value) for value in row])
    return buf.getvalue()


def _point(row: dict):
    try:
        lat = float(row.get("lat"))
        lon = float(row.get("lon"))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    try:
        alt = float(row["alt"]) if row.get("alt") is not None else 0.0
    except (TypeError, ValueError):
        alt = 0.0
    return lat, lon, alt


def _label(row: dict) -> str:
    return textsan.xml_escape(row.get("session") or row.get("marker") or "track")


def _description(row: dict) -> str:
    parts = [str(row.get(key) or "") for key in ("source", "marker", "pclass")]
    return textsan.xml_escape(" ".join(p for p in parts if p))


def to_kml(rows: list[dict]) -> str:
    marks = []
    for raw in rows:
        row = public_row(raw)
        point = _point(row)
        if point is None:
            continue
        lat, lon, alt = point
        marks.append(
            "<Placemark>"
            f"<name>{_label(row)}</name>"
            f"<description>{_description(row)}</description>"
            f"<Point><coordinates>{lon:.6f},{lat:.6f},{alt:.1f}</coordinates></Point>"
            "</Placemark>"
        )
    body = "".join(marks)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<name>Watchtower events</name>"
        f"{body}</Document></kml>"
    )


def to_gpx(rows: list[dict]) -> str:
    points = []
    for raw in rows:
        row = public_row(raw)
        point = _point(row)
        if point is None:
            continue
        lat, lon, _alt = point
        points.append(
            f'<wpt lat="{lat:.6f}" lon="{lon:.6f}">'
            f"<name>{_label(row)}</name>"
            f"<desc>{_description(row)}</desc>"
            f"<type>{textsan.xml_escape(row.get('source') or '')}</type>"
            "</wpt>"
        )
    body = "".join(points)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="drone-watchtower" '
        'xmlns="http://www.topografix.com/GPX/1/1">'
        f"{body}</gpx>"
    )
