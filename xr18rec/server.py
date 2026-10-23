"""Serveur HTTP minimal : interface web + API JSON (bibliothèque standard uniquement)."""

from __future__ import annotations

import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from . import qr
from .mixer import MixError, MixerClient
from .session import Recorder, RecorderError
from .xr18osc import OscError

_HOST_OK = re.compile(r"[A-Za-z0-9.\-]{1,100}(:\d{1,5})?")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def make_handler(rec: Recorder, mixer: MixerClient | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "xr18rec/0.1"

        def log_message(self, fmt, *args):
            pass

        # -- helpers
        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            except ValueError:
                return {}

        def _file(self, path: str):
            try:
                size = os.path.getsize(path)
                f = open(path, "rb")
            except OSError:
                return self._json({"error": "introuvable"}, 404)
            with f:
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.end_headers()
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break

        # -- routes
        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path in ("/", "/index.html"):
                return self._file(os.path.join(STATIC_DIR, "index.html"))
            if path == "/qr/mix.svg":  # QR de l'adresse que ce client a utilisée pour joindre le Pi
                host = self.headers.get("Host", "")
                if not _HOST_OK.fullmatch(host):
                    return self._json({"error": "en-tête Host invalide"}, 400)
                body = qr.to_svg(f"http://{host}/mix").encode()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/mix":
                return self._file(os.path.join(STATIC_DIR, "mix.html"))
            if path.startswith("/api/mix/"):
                return self._mix_get(path[len("/api/mix/"):])
            if path == "/api/status":
                return self._json(rec.status())
            if path == "/api/sessions":
                return self._json(rec.sessions())
            if path.startswith("/files/"):
                parts = path[len("/files/"):].split("/")
                if len(parts) == 2 and all(p and p not in (".", "..") and "\\" not in p for p in parts):
                    return self._file(os.path.join(rec.cfg.output_dir, *parts))
                return self._json({"error": "chemin invalide"}, 400)
            return self._json({"error": "introuvable"}, 404)

        def _mix_call(self, fn):
            if mixer is None:
                return self._json({"error": "mix non configuré (xr18_host absent)"}, 503)
            try:
                return self._json(fn())
            except MixError as exc:
                return self._json({"error": str(exc)}, 400)
            except OscError as exc:
                return self._json({"error": str(exc)}, 503)

        def _mix_get(self, rest: str):
            if rest == "buses":
                return self._mix_call(mixer.bus_list if mixer else None)
            if rest.isdigit():
                return self._mix_call(lambda: mixer.get_bus(int(rest)))
            return self._json({"error": "introuvable"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            m = re.fullmatch(r"/api/mix/(\d+)/send", path)
            if m:
                body = self._body()
                return self._mix_call(lambda: {"db": mixer.set_send(int(m.group(1)), str(body.get("source")),
                                                                    body.get("db"))})
            try:
                if path == "/api/start":
                    name = self._body().get("name")
                    return self._json({"session": rec.start(name), "status": rec.status()})
                if path == "/api/stop":
                    return self._json(rec.stop())
            except RecorderError as exc:
                return self._json({"error": str(exc)}, 409)
            return self._json({"error": "introuvable"}, 404)

    return Handler


def make_server(rec: Recorder, host: str | None = None, port: int | None = None,
                mixer: MixerClient | None = None) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host or rec.cfg.host, rec.cfg.port if port is None else port),
                              make_handler(rec, mixer))
    srv.daemon_threads = True
    return srv
