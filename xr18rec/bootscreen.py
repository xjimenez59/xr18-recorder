"""Écran d'attente de l'allumage : « Démarrage... » et anneau qui clignote, avant que l'application soit prête.

Module **volontairement léger** (n'importe que `oled`, `gpio`, `config`) : lancé par `xr18rec-boot.service` dès le
début du démarrage, il doit s'afficher le plus tôt possible sur un Pi 2 B (charger tout `xr18rec` coûte ~1 s,
davantage à froid). Lancement : `python3 -m xr18rec.bootscreen --config /etc/xr18rec.json`.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import threading
import time

from . import config as config_mod
from . import gpio, oled


def boot_frame(c: oled.Canvas, now: float) -> None:
    """Écran « Démarrage... » (points animés : montre que le Pi n'est pas figé) ; sert aussi quand l'application ne répond pas."""
    c.clear()
    c.centered(8, "XR18 REC", "12x24")
    x = (oled.WIDTH - c.text_width("Démarrage...")) // 2
    c.text(x, 44, "Démarrage" + "." * (int(now * 2) % 4))


def shutdown_frame(c: oled.Canvas) -> None:
    """Pendant l'extinction (le Pi écrit encore sur la carte SD) : surtout ne pas couper l'alimentation."""
    c.clear()
    c.centered(10, "EXTINCTION", "12x24")
    c.centered(44, "Ne pas débrancher")


def bye_frame(c: oled.Canvas) -> None:
    """Dernier écran, quand le système est arrêté : l'alimentation peut être coupée."""
    c.clear()
    c.centered(8, "Vous pouvez")
    c.centered(26, "ÉTEINDRE", "12x24")
    c.centered(52, "Pi arrêté")


BOOT_BUS_WAIT_S = 20.0
BOOT_BLINK_HZ = 1.0


class _LazyLed:
    """LED du démarrage : la ligne GPIO est demandée au premier appel qui réussit (une tentative par seconde).

    Très tôt dans le démarrage, /dev/gpiochip0 n'a pas encore reçu son groupe (règle udev) : « permission denied »."""

    def __init__(self, pin: int, clock=time.monotonic):
        self.pin, self.clock, self.lines, self._next = pin, clock, None, 0.0

    def __call__(self, on) -> None:
        if self.lines is None:
            if self.clock() < self._next:
                return
            self._next = self.clock() + 1.0
            try:
                self.lines = gpio.Lines([self.pin], gpio.FLAG_OUTPUT)
            except gpio.GpioError:
                return
        self.lines.set([int(bool(on))])


def open_display(cfg, stop: threading.Event, wait_s: float, clock=time.monotonic):
    """Écran OLED initialisé, ou None s'il n'y en a pas (on attend le bus I²C jusqu'à `wait_s` secondes)."""
    bus = f"/dev/i2c-{cfg.panel_i2c_bus}"
    deadline = clock() + wait_s
    while not os.path.exists(bus) and clock() < deadline and not stop.is_set():
        stop.wait(0.5)  # le module i2c-dev et les règles udev arrivent un peu après le démarrage du service
    link = None
    while link is None and clock() < deadline and not stop.is_set():
        addresses = (cfg.panel_i2c_address,) if cfg.panel_i2c_address else (0x3C, 0x3D)
        try:
            link = oled.detect(cfg.panel_i2c_bus, addresses) if os.path.exists(bus) else None
        except OSError:
            link = None
        if link is None:
            stop.wait(0.5)
    if link is None:
        return None
    display = oled.Oled(link, cfg.panel_driver, cfg.panel_rotate)
    display.init()
    return display


def _poweroff_requested() -> bool:
    """True si le système s'arrête (et non redémarre) : lit les tâches systemd en cours (poweroff.target / halt.target)."""
    try:
        out = subprocess.run(["systemctl", "list-jobs", "--no-legend"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "poweroff.target" in out or "halt.target" in out


def run_bye(cfg, display=None, poweroff=None, force: bool = False) -> int:
    """Dernier écran de l'extinction : « Vous pouvez éteindre ». Lancé par `xr18rec-bye.service` à son arrêt, quand
    tous les autres services sont arrêtés. Rien n'est affiché pour un simple redémarrage (`poweroff` = False)."""
    if cfg.panel == "off":
        return 0
    if not force and not (poweroff or _poweroff_requested)():
        return 0
    display = display or open_display(cfg, threading.Event(), 2.0)
    if display is None:
        return 0
    canvas = oled.Canvas()
    bye_frame(canvas)
    display.power(True)
    display.show(canvas)
    print("arrêt : écran « Vous pouvez éteindre »", flush=True)
    return 0


def run_boot(cfg, stop: threading.Event | None = None, display=None, set_led=None, clock=time.monotonic) -> int:
    """Écran « Démarrage... » et anneau qui clignote pendant l'allumage, avant que l'application soit prête.

    Lancé très tôt par `xr18rec-boot.service` ; systemd l'arrête quand `xr18rec.service` démarre (`Conflicts=`),
    la face avant normale reprend alors l'écran. Sans écran ni LED, sort aussitôt (code 0).
    Matériel injectable pour les tests (`display`, `set_led`)."""
    stop = stop or threading.Event()
    if cfg.panel == "off":
        return 0
    if display is None and set_led is None:
        display = open_display(cfg, stop, BOOT_BUS_WAIT_S, clock)
        if cfg.panel_led_rec_gpio:
            set_led = _LazyLed(cfg.panel_led_rec_gpio, clock)
    if display is None and set_led is None:
        print("démarrage : ni écran ni LED, rien à faire", flush=True)
        return 0
    print("démarrage : écran/LED d'attente", flush=True)
    canvas, last = oled.Canvas(), None
    try:
        while not stop.is_set():
            now = clock()
            if set_led:
                set_led(int(now * BOOT_BLINK_HZ * 2) % 2 == 0)
            if display:
                key = int(time.time() * 2) % 4
                if key != last:
                    last = key
                    boot_frame(canvas, time.time())
                    display.show(canvas)
            stop.wait(0.1)
    finally:
        if set_led:
            try:
                set_led(False)
            except OSError:
                pass
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xr18rec.bootscreen", description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--bye", action="store_true", help="affiche « Vous pouvez éteindre » (fin d'une extinction) et sort")
    ap.add_argument("--force", action="store_true", help="avec --bye : affiche même sans extinction en cours (essai)")
    args = ap.parse_args(argv)
    cfg = config_mod.load(args.config)
    config_mod.apply_overlay(cfg)
    if args.bye:
        return run_bye(cfg, force=args.force)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        return run_boot(cfg, stop)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
