"""Serveur HTTP minimal : interface web + API JSON (bibliothèque standard uniquement)."""

from __future__ import annotations

import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from . import audio, qr, system, xr18osc
from .app import App
from .updater import UpdateError
from .ziputil import ZipStream
from .mixer import MixError, MixerClient
from .session import Recorder, RecorderError
from .xr18osc import OscError

_HOST_OK = re.compile(r"[A-Za-z0-9.\-]{1,100}(:\d{1,5})?")
_STATIC_FILE = re.compile(r"/static/([A-Za-z0-9_-]+\.(?:css|js))")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def make_handler(rec: Recorder, app: App):
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

        def _zip(self, session: str):
            """Session complète en ZIP (WAV + projet Reaper) pour la déposer telle quelle sur le PC."""
            if not session or session in (".", "..") or "/" in session or "\\" in session:
                return self._json({"error": "chemin invalide"}, 400)
            if rec.is_recording():
                return self._json({"error": "téléchargement indisponible pendant un enregistrement "
                                            "(il ralentirait le Pi et risquerait de faire perdre de l'audio)"}, 409)
            directory = rec.resolve_session(session)
            if directory is None:
                return self._json({"error": "introuvable"}, 404)
            try:
                names = sorted(n for n in os.listdir(directory) if os.path.isfile(os.path.join(directory, n)))
                archive = ZipStream([(os.path.join(directory, n), f"{session}/{n}") for n in names])
            except OSError as exc:
                return self._json({"error": f"lecture impossible : {exc}"}, 500)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{session}.zip"')
            self.send_header("Content-Length", str(archive.size))
            self.end_headers()
            try:
                archive.write_to(self.wfile)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True  # téléchargement interrompu par le client ou fichier disparu

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
            if path.startswith("/zip/"):
                return self._zip(path[len("/zip/"):])
            if path == "/favicon.ico":  # pas d'icône : évite un 404 à chaque ouverture
                self.send_response(204)
                self.end_headers()
                return
            if path == "/rec":
                return self._file(os.path.join(STATIC_DIR, "rec.html"))
            m = _STATIC_FILE.fullmatch(path)
            if m:  # feuilles de style et scripts partagés (liste blanche : pas de remontée de dossier)
                return self._file(os.path.join(STATIC_DIR, m.group(1)))
            if path == "/mix":
                return self._file(os.path.join(STATIC_DIR, "mix.html"))
            if path.startswith("/api/mix/"):
                return self._mix_get(path[len("/api/mix/"):])
            if path == "/settings":
                return self._file(os.path.join(STATIC_DIR, "settings.html"))
            if path == "/api/settings":
                return self._json(app.settings())
            if path == "/api/update":
                return self._json(app.updates.status())
            if path == "/api/audio":
                return self._json(audio.probe(rec.cfg, rec.is_recording()))
            if path == "/api/system":
                return self._json(system.info())
            if path == "/api/status":
                return self._json(rec.status())
            if path == "/api/levels":
                return self._json(rec.levels())
            if path == "/api/mute":
                return self._mix_call(app.mixer.mute_state if app.mixer else None)
            if path == "/api/storage":
                return self._json(rec.storage_info())
            if path == "/api/sessions":
                return self._json(rec.sessions())
            if path.startswith("/files/"):
                parts = path[len("/files/"):].split("/")
                if len(parts) == 2 and all(p and p not in (".", "..") and "\\" not in p for p in parts):
                    found = rec.resolve_file(*parts)
                    return self._file(found) if found else self._json({"error": "introuvable"}, 404)
                return self._json({"error": "chemin invalide"}, 400)
            return self._json({"error": "introuvable"}, 404)

        def _mix_call(self, fn):
            if app.mixer is None:
                return self._json({"error": "mix non configuré (xr18_host absent)"}, 503)
            try:
                return self._json(fn())
            except MixError as exc:
                return self._json({"error": str(exc)}, 400)
            except OscError as exc:
                return self._json({"error": str(exc)}, 503)

        def _app_call(self, fn):
            """Réponse JSON pour les actions de la page de paramètres, avec le code HTTP adapté à l'erreur."""
            try:
                return self._json(fn())
            except RecorderError as exc:
                return self._json({"error": str(exc)}, 409)
            except UpdateError as exc:
                return self._json({"error": str(exc)}, 400)
            except system.SystemActionError as exc:
                return self._json({"error": str(exc)}, 502)
            except OscError as exc:
                return self._json({"error": str(exc)}, 503)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)

        def _mix_get(self, rest: str):
            if rest == "buses":
                return self._mix_call(app.mixer.bus_list if app.mixer else None)
            if rest.isdigit():
                return self._mix_call(lambda: app.mixer.get_bus(int(rest)))
            return self._json({"error": "introuvable"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/mute":
                body = self._body()
                return self._mix_call(lambda: app.mixer.set_mute(str(body.get("target")), bool(body.get("muted"))))
            if path == "/api/settings":
                return self._app_call(lambda: app.apply(self._body()))
            if path == "/api/levels/monitor":
                try:
                    on = bool(self._body().get("on"))
                    return self._json(rec.monitor_start() if on else rec.monitor_stop())
                except RecorderError as exc:
                    return self._json({"error": str(exc)}, 409)
            if path == "/api/levels/reset":
                return self._json(rec.reset_levels())
            if path == "/api/update/scan":
                return self._app_call(lambda: {"usb": app.updates.usb_candidates()})
            if path == "/api/update/upload":  # corps brut = le fichier de mise à jour
                length = int(self.headers.get("Content-Length") or 0)
                return self._app_call(lambda: app.updates.stage_stream(self.rfile, length))
            if path == "/api/update/stage":
                return self._app_call(lambda: app.updates.stage_usb(str(self._body().get("path", ""))))
            if path == "/api/update/check":
                return self._app_call(app.updates.check_online)
            if path == "/api/update/download":
                return self._app_call(app.updates.stage_online)
            if path == "/api/update/install":
                return self._app_call(lambda: app.updates.install(bool(self._body().get("allow_downgrade"))))
            if path == "/api/settings/reset":
                return self._app_call(app.reset)
            if path == "/api/xr18/discover":
                return self._app_call(lambda: {"found": xr18osc.discover()})
            if path == "/api/system/time":
                return self._app_call(lambda: {"result": system.set_time(self._body().get("epoch"))})
            if path == "/api/system/power":
                def power():
                    if rec.is_recording():
                        raise RecorderError("impossible pendant un enregistrement")
                    system.power(str(self._body().get("action")))
                    return {"ok": True}
                return self._app_call(power)
            if path == "/api/system/restart-app":
                return self._app_call(lambda: (app.restart_app(), {"ok": True})[1])
            m = re.fullmatch(r"/api/mix/(\d+)/send", path)
            if m:
                body = self._body()
                return self._mix_call(lambda: {"db": app.mixer.set_send(int(m.group(1)), str(body.get("source")),
                                                                    body.get("db"))})
            try:
                if path == "/api/start":
                    name = self._body().get("name")
                    return self._json({"session": rec.start(name), "status": rec.status()})
                if path == "/api/storage":
                    return self._json(rec.set_storage(str(self._body().get("path", ""))))
                if path == "/api/stop":
                    return self._json(rec.stop())
            except RecorderError as exc:
                return self._json({"error": str(exc)}, 409)
            return self._json({"error": "introuvable"}, 404)

    return Handler


def make_server(rec: Recorder, host: str | None = None, port: int | None = None,
                mixer: MixerClient | None = None, app: App | None = None) -> ThreadingHTTPServer:
    app = app or App(rec.cfg, rec, mixer)
    srv = ThreadingHTTPServer((host or rec.cfg.host, rec.cfg.port if port is None else port),
                              make_handler(rec, app))
    srv.app = app
    srv.daemon_threads = True
    return srv
