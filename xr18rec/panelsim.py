"""Simulateur de la face avant pour le poste de développement : l'écran OLED, le bouton et les LED dans une page web.

Utilise le vrai code de `panel.py` (mêmes écrans, mêmes règles d'appui) contre l'API d'un serveur en marche
(`scripts/dev-ui.sh`), sans I²C ni GPIO. Lancement : `python3 -m xr18rec panel --simulate --config <fichier>`.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import panel

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "panelsim.html")


class Board:
    """Ce que la face avant « voit » : l'image de l'écran, l'état des LED, le bouton."""

    def __init__(self):
        self.frame = bytes(1024)
        self.on = True
        self.leds = (False, False)
        self.pressed = False
        self._down_at = 0.0

    def press(self, pressed: bool) -> None:
        """Appui de la souris ; un clic très bref est prolongé à 120 ms, comme un vrai contact (et pour l'anti-rebond)."""
        now = time.monotonic()
        if pressed:
            self._down_at, self.pressed = now, True
        elif now - self._down_at < 0.12:
            threading.Timer(0.12 - (now - self._down_at), lambda: setattr(self, "pressed", False)).start()
        else:
            self.pressed = False

    # affichage (même interface que oled.Oled)
    def power(self, on: bool) -> None:
        self.on = on

    def show(self, canvas) -> int:
        self.frame = bytes(canvas.buf)
        return 8


def make_server(cfg, sim_port: int, fetch=None, post=None):
    board = Board()
    if fetch is None:
        fetch, post = panel.api_client(cfg.port)
    real_post = post

    def safe_post(path: str, body: dict):
        if path == "/api/system/power":  # jamais d'extinction réelle de la machine de développement
            return {"ok": True, "simulated": True}
        return real_post(path, body)
    p = panel.Panel(cfg, board, lambda: board.pressed, lambda rec, ok: setattr(board, "leds", (rec, ok)), fetch, safe_post)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/frame":
                notice = p.notice[0] if p.notice and p.clock() < p.notice[2] else ""
                return self._send(json.dumps({"on": board.on, "frame": base64.b64encode(board.frame).decode(),
                                              "rec": board.leds[0], "ok": board.leds[1], "notice": notice,
                                              "api": p.status is not None}).encode(), "application/json")
            with open(PAGE, "rb") as f:
                self._send(f.read(), "text/html; charset=utf-8")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                board.press(bool(json.loads(self.rfile.read(n) or b"{}").get("pressed")))
            except ValueError:
                pass
            self._send(b"{}", "application/json")

    srv = ThreadingHTTPServer(("0.0.0.0", sim_port), Handler)
    return srv, p


def run(cfg, sim_port: int) -> int:
    srv, p = make_server(cfg, sim_port)
    threading.Thread(target=p.run, daemon=True).start()
    print(f"simulateur de la face avant : http://localhost:{sim_port}  (serveur de l'enregistreur : port {cfg.port})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        p.stop_event.set()
        srv.server_close()
    return 0
