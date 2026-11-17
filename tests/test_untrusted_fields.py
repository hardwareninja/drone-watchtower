"""Radio strings stay data in the dashboard, the history export, and CoT."""
from __future__ import annotations

import asyncio
import csv
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

import app  # noqa: E402


HOSTILE = "<script>alert(1)</script>"


def test_presented_token_prefers_headers_over_the_query_string():
    headers = {"x-watchtower-token": "header-token", "authorization": "Bearer other"}
    assert app.presented_token(headers, {"watchtower_token": "cookie"}, {"token": "query"}) == "header-token"
    assert app.presented_token(
        {"authorization": "Bearer bearer-token"}, {}, {"token": "query"},
    ) == "bearer-token"
    assert app.presented_token({}, {"watchtower_token": "cookie-token"}, {"token": "query"}) == "cookie-token"


def test_publish_constrains_radio_strings(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB", tmp_path / "history.db")
    app.init_db()
    sent = []
    monkeypatch.setattr(app, "broadcast", lambda msg: sent.append(msg))
    app.aircraft_registry.registry.reset()
    app.CONFIG["aircraft"] = {"SN100": "Known craft"}
    try:
        app.publish_event({
            "source": "remoteid",
            "time": "2026-11-17T00:00:01.000+00:00",
            "session": "SN100",
            "marker": "RemoteID",
            "selfid": HOSTILE + "hello",
            "operator": "Pat & <Lee>",
            "status": "airborne",
            "lat": 22.3,
            "lon": 114.1,
            "crc": True,
            "freq": 2437.0,
        })
        evt = sent[0]["event"]
        assert "<" not in evt["selfid"]
        assert "<" not in evt["operator"]
        assert evt["model"] == "Known craft"
        lookups = app.aircraft_registry.registry.lookups
        app.publish_event({
            "source": "remoteid",
            "time": "2026-11-17T00:00:05.000+00:00",
            "session": "SN100",
            "marker": "RemoteID",
            "lat": 22.31,
            "lon": 114.11,
            "crc": True,
        })
        assert app.aircraft_registry.registry.lookups == lookups
    finally:
        app.CONFIG["aircraft"] = {}
        app.aircraft_registry.registry.reset()


def _body(resp) -> str:
    body = getattr(resp, "body", None)
    if body:
        return body.decode() if isinstance(body, bytes) else body

    async def collect():
        parts = []
        async for chunk in resp.body_iterator:
            parts.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(parts)

    return asyncio.run(collect())


def test_history_export_encodes_html_csv_and_xml(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB", tmp_path / "history.db")
    app.init_db()
    row = {key: None for key in app.EVENT_COLS}
    row.update({
        "time": "2026-11-17T00:00:00+00:00",
        "session": "+cmd|' /C calc'!A0",
        "source": "remoteid",
        "marker": HOSTILE,
        "pclass": 'a&b"c',
        "freq": 5745.0,
        "rssi": -40,
        "lat": 22.3,
        "lon": 114.1,
        "alt": 12,
        "crc": 1,
    })
    with app.db_conn() as conn:
        conn.execute(
            f"INSERT INTO events ({','.join(app.EVENT_COLS)}) VALUES ({','.join('?' * len(app.EVENT_COLS))})",
            tuple(row[key] for key in app.EVENT_COLS),
        )
    csv_text = _body(app.history_export())
    assert "<script" not in csv_text
    session = list(csv.reader(io.StringIO(csv_text)))[1][1]
    assert session.startswith("'")
    assert "<" not in session
    kml = _body(app.history_export(fmt="kml"))
    gpx = _body(app.history_export(fmt="gpx"))
    for doc in (kml, gpx):
        assert "<script" not in doc
        assert "&amp;" in doc or "a&b" not in doc
    assert "<kml" in kml and "<gpx" in gpx


def test_dashboard_escape_and_sinks():
    node = subprocess.run(
        ["node", "-e", r"""
const {esc, kvHtml} = require(process.env.ESCAPE);
const payload = `<img src=x onerror="alert(1)"><script>alert(1)</script>&"'`;
const html = kvHtml([["Serial", payload, "mono"], ["Callsign", payload], ["Operator", payload]]);
const leftover = html.replace(/<\/?(div|span|code)(?:\s[^>]*)?>/g, "");
if (leftover.includes("<") || leftover.includes(">")) {
  console.error(html); process.exit(2);
}
if (!html.includes("&lt;script&gt;") || !html.includes("&amp;") || !html.includes("&quot;")) {
  console.error(html); process.exit(3);
}
const attr = `<span title="${esc(payload)}">x</span>`;
if ((attr.match(/"/g) || []).length !== 2) {
  console.error(attr); process.exit(4);
}
"""],
        capture_output=True, text=True,
        env={**os.environ, "ESCAPE": str(ROOT / "webapp" / "frontend" / "escape.js")},
    )
    assert node.returncode == 0, node.stderr
    js = (ROOT / "webapp" / "frontend" / "app.js").read_text()
    assert '`<div class="kv"><span>${k}</span><span>${v}</span></div>`' not in js
    assert "kvHtml(kv)" in js
    assert "${esc(p.marker)}" in js
    assert "${esc(e.marker" in js
