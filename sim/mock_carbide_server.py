"""In-memory mock of the Light Conversion CARBIDE / PHAROS REST API, for testing
iocs/carbide_ioc.py and iocs/pharos_ioc.py without the real laser. The laser model is
ioc_host.drivers.carbide.SimLaser (the documented endpoints and their 403/404 answers).
Logs EVERY non-GET request to stdout (prefixed "WRITE") so a test can assert that
startup/polling never commands the laser.

    python sim/mock_carbide_server.py --port 20010
    python ioc_host/iocs/carbide_ioc.py --host 127.0.0.1 --port 20010 --prefix TEST:LASER1:
    python sim/mock_carbide_server.py --port 20022 --flavour pharos --version v0
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ioc_host.drivers.carbide import LaserHTTPError, SimLaser  # noqa: E402

LASER = SimLaser()
VERSION = "v1"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _endpoint(self):
        version, _, endpoint = self.path.strip("/").partition("/")
        return endpoint if version == VERSION else None

    def _call(self, func, *args):
        try:
            return 200, func(*args)
        except LaserHTTPError as exc:
            return exc.status, str(exc)
        except (ValueError, TypeError) as exc:
            return 400, str(exc)

    def do_GET(self):
        endpoint = self._endpoint()
        if endpoint is None:
            return self._send(404, "not found")
        self._send(*self._call(LASER.get, "/" + endpoint))

    def _write(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        print(f"WRITE {method} {self.path} {body}", flush=True)
        endpoint = self._endpoint()
        if endpoint is None:
            return self._send(404, "not found")
        if method == "PUT":
            try:
                value = json.loads(body)
            except ValueError:
                return self._send(400, "body is not JSON")
            code, msg = self._call(LASER.put, "/" + endpoint, value)
        else:
            code, msg = self._call(LASER.post, "/" + endpoint)
        self._send(code, None if code == 200 else msg.split(": ", 1)[-1])

    def do_PUT(self):
        self._write("PUT")

    def do_POST(self):
        self._write("POST")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=20010)
    ap.add_argument("--flavour", choices=("carbide", "pharos"), default="carbide")
    ap.add_argument("--version", default=None, help="API version in the URL (default v1, pharos v0)")
    a = ap.parse_args()
    LASER = SimLaser(a.flavour)
    VERSION = a.version or ("v0" if a.flavour == "pharos" else "v1")
    print(f"mock {a.flavour} on 127.0.0.1:{a.port}/{VERSION}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
