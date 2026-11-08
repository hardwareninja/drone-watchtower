#!/usr/bin/env python3
"""Forward ADSB1090 and swap its blocked basemap URLs for Esri dark-gray tiles.

OpenStreetMap returns an "Access blocked" tile to this page, and CARTO's
dark/light styles now render an "API key required" placeholder. Esri's
public dark-gray canvas serves real tiles without a key. Zoom past 16
reuses the last native level.
"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = ("127.0.0.1", 8091)
ESRI = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"
)
REPLACEMENTS = (
    ("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", ESRI),
    ("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", ESRI),
    ("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", ESRI),
    ("https://{s}.basemaps.cartocdn.com/voyager/{z}/{x}/{y}{r}.png", ESRI),
    (
        "{maxZoom:18, attribution:'&copy; OpenStreetMap'}",
        "{maxZoom:19, maxNativeZoom:16, attribution:'Tiles &copy; Esri'}",
    ),
    ("/static/js/app.js?v=1.70-strict2", "/static/js/app.js?v=1.70-esri1"),
)
HOP = {"content-length", "transfer-encoding", "content-encoding", "connection"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else None
        headers = {}
        for key, value in self.headers.items():
            if key.lower() in ("host", "content-length", "accept-encoding", "connection"):
                continue
            headers[key] = value
        headers["Host"] = "127.0.0.1:8091"
        headers["Accept-Encoding"] = "identity"
        if length:
            headers["Content-Length"] = str(length)
        conn = http.client.HTTPConnection(*UPSTREAM, timeout=120)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:
            msg = f"upstream unavailable: {exc}\n".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        ctype = (resp.getheader("Content-Type") or "").lower()
        rewrite = (
            "javascript" in ctype
            or "text/html" in ctype
            or self.path.startswith("/static/js/app.js")
        )
        if "text/event-stream" in ctype and self.command != "HEAD":
            self.send_response(resp.status)
            for key, value in resp.headers.items():
                if key.lower() in HOP:
                    continue
                self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                try:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            try:
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            conn.close()
            return
        raw = b"" if self.command == "HEAD" else resp.read()
        if rewrite:
            text = raw.decode("utf-8", "ignore")
            for old, new in REPLACEMENTS:
                text = text.replace(old, new)
            raw = text.encode("utf-8")
        self.send_response(resp.status)
        for key, value in resp.headers.items():
            if key.lower() in HOP:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        if rewrite:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)
        conn.close()

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_HEAD(self):
        self._proxy()

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
