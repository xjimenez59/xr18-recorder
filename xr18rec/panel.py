"""Face avant du boîtier : écran OLED, bouton REC/STOP et LED (facultatifs, détectés au démarrage).

Processus séparé de l'enregistrement : il ne fait qu'interroger l'API locale (`/api/status` chaque seconde) et appeler
`/api/start`, `/api/stop` et `/api/system/power`. S'il plante ou s'il n'y a aucun matériel, l'application se comporte
comme sans face avant. Lancé et surveillé par `xr18rec serve` (`supervise`).

Bouton : appui court au repos = démarrer ; maintenu 2 s pendant la prise = arrêter ; maintenu 5 s au repos = éteindre
le Pi proprement. **Non vérifié sur le matériel.**
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from . import gpio, oled

STATUS_EVERY_S = 1.0
DRAW_EVERY_S = 0.25
POLL_S = 0.02  # lecture du bouton : 20 ms (l'anti-rebond demande deux lectures identiques)
NOTICE_S = 2.5

SHORT_ALERTS = {
    "take_undervoltage": "SOUS-TENSION", "undervoltage": "SOUS-TENSION", "take_throttled": "RALENTISSEMENT",
    "throttled": "RALENTISSEMENT", "take_temp": "TEMPÉRATURE", "temp": "TEMPÉRATURE", "temperature": "TEMPÉRATURE",
    "levels_partial": "ANALYSE PARTIELLE", "recorder": "ERREUR ENREGISTR.", "space": "DISQUE PRESQUE PLEIN",
    "load": "PI SURCHARGE", "memory": "MÉMOIRE",
}


def fmt_clock(seconds: float) -> str:
    s = max(0, int(seconds))
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m:02d}:{x:02d}"


def fmt_remaining(seconds) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    return f"{s // 3600}h{s % 3600 // 60:02d}" if s >= 3600 else f"{s // 60} min"


def short_alert(alert: dict, status: dict) -> str:
    code = str(alert.get("code", ""))
    if code == "overruns":
        return f"AUDIO PERDU x{status.get('overruns', '?')}"
    return SHORT_ALERTS.get(code) or str(alert.get("message", "ALERTE")).upper()


def net_info() -> tuple[str, str]:
    """(adresse IP, nom d'hôte.local) ; l'IP est celle de la route par défaut (aucun paquet n'est envoyé)."""
    ip = "?"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
    except OSError:
        pass
    return ip, socket.gethostname() + ".local"


class ButtonLogic:
    """Interprète les appuis (voir l'en-tête du module) ; testable sans matériel."""
    TAP_MAX, SHOW_AFTER, STOP_HOLD, POWER_SHOW, POWER_HOLD = 0.8, 0.3, 2.0, 1.0, 5.0

    def __init__(self):
        self.t0: float | None = None
        self.fired = False

    def update(self, now: float, pressed: bool, recording: bool):
        """Renvoie (action, surimpression) ; action : start | stop | poweroff | hint | None ; surimpression : (texte, fraction)."""
        action = overlay = None
        if pressed:
            if self.t0 is None:
                self.t0, self.fired = now, False
            held = now - self.t0
            if not self.fired:
                if recording:
                    if held >= self.STOP_HOLD:
                        action, self.fired = "stop", True
                    elif held >= self.SHOW_AFTER:
                        overlay = ("Maintenir : ARRÊT", held / self.STOP_HOLD)
                elif held >= self.POWER_HOLD:
                    action, self.fired = "poweroff", True
                elif held >= self.POWER_SHOW:
                    overlay = ("Maintenir : EXTINCTION", (held - self.POWER_SHOW) / (self.POWER_HOLD - self.POWER_SHOW))
        elif self.t0 is not None:
            if not self.fired and now - self.t0 < self.TAP_MAX:
                action = "hint" if recording else "start"
            self.t0 = None
        return action, overlay


_SEGMENTS = {"0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd", "6": "afgecd",
             "7": "abc", "8": "abcdefg", "9": "abcdfg"}


def _digit(c: oled.Canvas, x: int, y: int, w: int, h: int, t: int, ch: str) -> None:
    """Un chiffre « sept segments » de w×h pixels, traits de t pixels."""
    ym = (h - t) // 2
    segs = {"a": (x + t, y, w - 2 * t, t), "g": (x + t, y + ym, w - 2 * t, t), "d": (x + t, y + h - t, w - 2 * t, t),
            "f": (x, y, t, ym + t), "b": (x + w - t, y, t, ym + t),
            "e": (x, y + ym, t, h - ym), "c": (x + w - t, y + ym, t, h - ym)}
    for name in _SEGMENTS[ch]:
        c.rect(*segs[name])


def big_clock(c: oled.Canvas, y: int, text: str, h: int = 36) -> None:
    """Chronomètre en chiffres sept segments, le plus grand possible : la largeur de chaque chiffre s'adapte au texte
    (08:05, 1:23:45 ou 10:23:45 occupent toute la largeur de l'écran)."""
    digits = sum(ch != ":" for ch in text)
    colons = len(text) - digits
    gap = 3
    for w in range(30, 11, -1):
        t = 6 if w >= 24 else 5 if w >= 17 else 4
        colon_w = t + 2
        total = digits * w + colons * colon_w + (len(text) - 1) * gap
        if total <= oled.WIDTH - 2:
            break
    x = (oled.WIDTH - total) // 2
    for ch in text:
        if ch == ":":
            c.rect(x + 1, y + h // 4 - t // 2, t, t)
            c.rect(x + 1, y + 3 * h // 4 - t // 2, t, t)
            x += colon_w + gap
        else:
            _digit(c, x, y, w, h, t, ch)
            x += w + gap


def frame_key(st: dict | None, now: float, net: tuple[str, str], overlay=None) -> tuple:
    """Tout ce qui change l'image : deux états de même clé donnent le même dessin (on évite de redessiner pour rien)."""
    if st is None:
        return (None,)
    alerts = st.get("alerts") or []
    shown = short_alert(alerts[int(now / 3) % len(alerts)], st) if alerts else None
    fraction = None if not overlay or overlay[1] is None else round(overlay[1], 2)
    return (st.get("state"), st.get("session"), fmt_clock(st.get("elapsed_s", 0)), fmt_remaining(st.get("remaining_s")),
            (st.get("storage") or {}).get("kind"), st.get("version"), shown,
            int(now * 2) % 2, int(now / 3) % 3, overlay[0] if overlay else None, fraction, net)


def render(c: oled.Canvas, st: dict | None, now: float, net: tuple[str, str], overlay=None) -> None:
    """Dessine l'écran correspondant à l'état (`st` = réponse de /api/status, None si l'application ne répond pas).

    Disposition (police 6×12 = 21 colonnes, 5 lignes) : en-tête y 0-11, grand affichage y 13-48, ligne du bas y 52-63."""
    c.clear()
    if st is None:
        c.centered(8, "XR18 REC", "12x24")
        c.centered(44, "Démarrage...")
        return
    recording = st.get("state") == "recording"
    alerts = st.get("alerts") or []
    storage = st.get("storage") or {}
    where = "USB" if storage.get("kind") == "usb" else "SD"
    alert_text = ("! " + short_alert(alerts[int(now / 3) % len(alerts)], st)) if alerts else None
    reste = "Reste " + fmt_remaining(st.get("remaining_s"))
    if recording:
        if not alert_text:  # une alerte prend la place de la ligne d'en-tête (le chronomètre reste visible)
            if int(now * 2) % 2 == 0:
                c.disc(5, 5, 4)
            c.text(14, 0, "REC " + str(st.get("session") or "")[:17])
        big_clock(c, 13, fmt_clock(st.get("elapsed_s", 0)))
        c.text(0, 52, reste)
        c.text(oled.WIDTH - c.text_width(where), 52, where)
    else:
        if not alert_text:
            c.text(0, 0, "XR18 REC")
            version = "v" + str(st.get("version", ""))
            c.text(oled.WIDTH - c.text_width(version), 0, version)
        c.centered(15, "ERREUR" if st.get("state") == "error" else "PRÊT", "16x32")
        # ligne du bas : temps restant et stockage, puis l'adresse du Pi, puis son nom, à tour de rôle (3 s chacun)
        phase = int(now / 3) % 3
        if phase == 0:
            c.text(0, 52, reste)
            c.text(oled.WIDTH - c.text_width(where), 52, where)
        else:
            c.centered(52, net[phase - 1])
    if alert_text:
        c.rect(0, 0, oled.WIDTH, 12)
        c.text(2, 0, alert_text[:20], invert=True)
    if overlay:
        message, fraction = overlay
        c.rect(0, 34, oled.WIDTH, 30, False)
        c.outline(0, 34, oled.WIDTH, 30)
        c.centered(38, message)
        if fraction is not None:
            c.outline(8, 54, oled.WIDTH - 16, 7)
            c.rect(10, 56, int((oled.WIDTH - 20) * max(0.0, min(1.0, fraction))), 3)


class Panel:
    """Boucle de la face avant. Tout le matériel est injecté (afficheur, lecture du bouton, LED, appels à l'API)."""

    def __init__(self, cfg, display=None, read_button=None, set_leds=None, fetch=None, post=None,
                 clock=time.monotonic, net=None):
        self.cfg, self.display = cfg, display
        self.read_button, self.set_leds = read_button, set_leds
        self.fetch, self.post, self.clock = fetch, post, clock
        self.net = net or net_info()
        self.canvas = oled.Canvas()
        self.logic = ButtonLogic()
        self.status: dict | None = None
        self.notice: tuple | None = None  # (texte, fraction, jusqu'à)
        self._raw = None
        self._stable = False
        self._awake_until = clock() + float(cfg.panel_sleep_s)
        self._drawn = float("-inf")
        self._last_key = None
        self._was_active = True
        self.stop_event = threading.Event()

    # -- entrées
    def _debounced_pressed(self) -> bool:
        raw = bool(self.read_button()) if self.read_button else False
        if raw == self._raw:
            self._stable = raw
        self._raw = raw
        return self._stable

    def _say(self, now: float, text: str, seconds: float = NOTICE_S) -> None:
        self.notice = (text, None, now + seconds)

    def _call(self, now: float, path: str, body: dict, failure: str) -> None:
        def go():
            try:
                self.post(path, body)
            except Exception as exc:  # l'écran montre l'échec, il ne doit jamais interrompre la boucle
                self._say(self.clock(), failure + " : " + str(exc)[:18], 4)
        threading.Thread(target=go, daemon=True).start()

    def _act(self, now: float, action: str) -> None:
        if action == "start":
            self._call(now, "/api/start", {}, "Échec REC")
        elif action == "stop":
            self._call(now, "/api/stop", {}, "Échec STOP")
        elif action == "poweroff":
            self._say(now, "Extinction...", 30)
            self._call(now, "/api/system/power", {"action": "poweroff"}, "Échec")
        elif action == "hint":
            self._say(now, "Maintenir 2 s : ARRÊT")

    # -- un tour de boucle
    def step(self) -> None:
        now = self.clock()
        st = self.status
        recording = bool(st and st.get("state") == "recording")
        pressed = self._debounced_pressed()
        action, hold = self.logic.update(now, pressed, recording)
        if action:
            self._act(now, action)
        if pressed or action:
            self._awake_until = now + float(self.cfg.panel_sleep_s)
        overlay = hold
        if overlay is None and self.notice and now < self.notice[2]:
            overlay = self.notice[:2]
        alerts = bool(st and st.get("alerts"))
        active = recording or alerts or pressed or bool(overlay) or st is None
        if active:
            self._awake_until = now + float(self.cfg.panel_sleep_s)
        awake = not self.cfg.panel_sleep_s or now < self._awake_until
        if self.display:
            awake_before = self.display._on if hasattr(self.display, "_on") else getattr(self.display, "on", True)
            self.display.power(awake)
            if awake and (now - self._drawn >= DRAW_EVERY_S or action):  # le dessin coûte cher sur un Pi 2 : 4 fois par seconde au plus
                self._drawn = now
                wall = time.time()
                key = frame_key(st, wall, self.net, overlay)
                if key != self._last_key or not awake_before:
                    self._last_key = key
                    render(self.canvas, st, wall, self.net, overlay)
                    self.display.show(self.canvas)
        if self.set_leds:
            blink1, blink2 = int(now) % 2 == 0, int(now * 4) % 2 == 0
            if recording:
                rec = blink2 if alerts else True
                ok = False
            else:
                rec = False
                ok = blink1 if alerts else bool(st)
            self.set_leds(rec, ok)

    def poll_status(self) -> None:
        try:
            self.status = self.fetch()
        except Exception:
            self.status = None

    def run(self) -> None:
        threading.Thread(target=self._poller, daemon=True).start()
        while not self.stop_event.is_set():
            try:
                self.step()
            except OSError as exc:  # bus I²C ou GPIO momentanément en défaut : on réessaie au tour suivant
                print(f"face avant : {exc}", file=sys.stderr, flush=True)
            self.stop_event.wait(POLL_S)

    def _poller(self) -> None:
        while not self.stop_event.is_set():
            self.poll_status()
            self.stop_event.wait(STATUS_EVERY_S)


# ---- matériel réel ------------------------------------------------------------------------------
def api_client(port: int):
    base = f"http://127.0.0.1:{port}"

    def fetch():
        with urllib.request.urlopen(base + "/api/status", timeout=2) as r:
            return json.loads(r.read())

    def post(path: str, body: dict):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", exc.reason)
            except ValueError:
                detail = exc.reason
            raise RuntimeError(detail) from exc
    return fetch, post


def build(cfg) -> Panel | None:
    """Panel avec le matériel présent, ou None s'il n'y a pas de face avant (aucun écran détecté)."""
    if cfg.panel == "off":
        return None
    link = None
    if os.path.exists(f"/dev/i2c-{cfg.panel_i2c_bus}"):
        addresses = (cfg.panel_i2c_address,) if cfg.panel_i2c_address else (0x3C, 0x3D)
        link = oled.detect(cfg.panel_i2c_bus, addresses)
    if link is None and cfg.panel != "on":
        return None
    display = None
    if link is not None:
        display = oled.Oled(link, cfg.panel_driver, cfg.panel_rotate)
        display.init()
    read_button = set_leds = None
    try:
        if cfg.panel_button_gpio:
            line = gpio.Lines([cfg.panel_button_gpio], gpio.FLAG_INPUT | gpio.FLAG_BIAS_PULL_UP)
            read_button = lambda: line.get()[0] == 0  # noqa: E731  (contact vers la masse = appuyé)
    except gpio.GpioError as exc:
        print(f"face avant : bouton indisponible ({exc})", flush=True)
    try:
        pins = [p for p in (cfg.panel_led_rec_gpio, cfg.panel_led_ok_gpio) if p]
        if pins:
            leds = gpio.Lines(pins, gpio.FLAG_OUTPUT)
            order = [bool(cfg.panel_led_rec_gpio), bool(cfg.panel_led_ok_gpio)]

            def set_leds(rec, ok, leds=leds, order=order):  # noqa: E306
                wanted = [rec, ok]
                leds.set([int(v) for v, used in zip(wanted, order) if used])
    except gpio.GpioError as exc:
        print(f"face avant : LED indisponibles ({exc})", flush=True)
    fetch, post = api_client(cfg.port)
    return Panel(cfg, display, read_button, set_leds, fetch, post)


def run(cfg) -> int:
    panel = build(cfg)
    if panel is None:
        print("face avant : aucun écran détecté, rien à faire", flush=True)
        return 0
    try:
        os.nice(10)  # jamais au détriment de l'enregistrement
    except OSError:
        pass
    print("face avant : écran détecté, démarrage", flush=True)
    try:
        panel.run()
    except KeyboardInterrupt:
        pass
    finally:
        panel.stop_event.set()
        if panel.display:
            panel.display.power(False)
        if panel.set_leds:
            panel.set_leds(False, False)
    return 0


def supervise(config_path: str | None, stop: threading.Event, retry_s: float = 5.0):
    """Lance `xr18rec panel` en processus séparé et le relance s'il plante ; code 0 = pas de matériel, on s'arrête."""
    cmd = [sys.executable, "-m", "xr18rec", "panel"] + (["--config", config_path] if config_path else [])

    def loop():
        while not stop.is_set():
            try:
                proc = subprocess.Popen(cmd)
            except OSError:
                return
            while proc.poll() is None:
                if stop.wait(0.5):
                    proc.terminate()
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return
            if proc.returncode == 0:
                return
            stop.wait(retry_s)
    t = threading.Thread(target=loop, name="face-avant", daemon=True)
    t.start()
    return t
