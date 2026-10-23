"""Actions système : heure, extinction, informations. Passent par systemd (autorisé par polkit dans l'image)."""

from __future__ import annotations

import os
import socket
import subprocess
import time

from . import xr18osc

RUN = subprocess.run  # remplaçable dans les tests
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_EPOCH, MAX_EPOCH = 1_577_836_800, 4_102_444_800  # 2020-01-01 .. 2100-01-01


class SystemActionError(RuntimeError):
    pass


def _run(cmd: list[str]) -> str:
    try:
        proc = RUN(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemActionError(f"{cmd[0]} : {exc}") from exc
    if proc.returncode != 0:
        raise SystemActionError((proc.stderr or proc.stdout or f"code {proc.returncode}").strip())
    return proc.stdout


def version() -> str:
    try:
        with open(os.path.join(APP_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


def ntp_synchronized() -> bool | None:
    """True/False d'après systemd, None si inconnu (hors systemd)."""
    try:
        out = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    except SystemActionError:
        return None
    return out.strip() == "yes"


def info() -> dict:
    return {
        "hostname": socket.gethostname(),
        "version": version(),
        "ips": [str(ip) for ip, _net in xr18osc.local_addresses()],
        "time": int(time.time()),
        "timezone": time.tzname[0],
        "ntp_synchronized": ntp_synchronized(),
    }


def set_time(epoch: int) -> str:
    """Cale l'horloge du Pi (sans RTC ni Internet, elle repart d'une date fausse)."""
    if not isinstance(epoch, int) or not MIN_EPOCH <= epoch <= MAX_EPOCH:
        raise SystemActionError("date invalide")
    if ntp_synchronized():
        return "inutile : l'horloge est déjà synchronisée par Internet"
    _run(["timedatectl", "set-ntp", "false"])  # sinon systemd refuse de régler l'heure à la main
    _run(["timedatectl", "set-time", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))])
    return "horloge réglée"


def power(action: str) -> None:
    if action not in ("poweroff", "reboot"):
        raise SystemActionError("action inconnue")
    _run(["systemctl", action])
