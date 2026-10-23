"""Détection des emplacements d'enregistrement : carte SD (dossier par défaut) et disques USB montés.

Pas de montage automatique ici (il faudrait des droits root) : un disque USB n'est proposé que
s'il est déjà monté (fstab avec `nofail`, ou montage automatique du système). Les enregistrements
d'un disque USB vont dans `<point de montage>/xr18-recordings`.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass

MOUNTS_FILE = "/proc/mounts"
LABEL_DIR = "/dev/disk/by-label"
USB_SUBDIR = "xr18-recordings"
_USB_DEVICES = ("/dev/sd", "/dev/nvme")  # disques USB (ou SATA/NVMe) ; la carte SD du Pi est /dev/mmcblk*


@dataclass
class Volume:
    path: str  # dossier où seront écrites les sessions
    label: str
    kind: str  # "sd" ou "usb"
    device: str
    fstype: str
    total_gb: float
    free_gb: float
    writable: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _unescape(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)  # « \040 » = espace


def read_mounts() -> list[tuple[str, str, str]]:
    """[(périphérique, point de montage, système de fichiers)] ; vide si /proc/mounts est illisible."""
    out = []
    try:
        with open(MOUNTS_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    out.append((_unescape(parts[0]), _unescape(parts[1]), parts[2]))
    except OSError:
        pass
    return out


def _labels() -> dict[str, str]:
    """{périphérique réel: étiquette} d'après /dev/disk/by-label."""
    out = {}
    try:
        for name in os.listdir(LABEL_DIR):
            label = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), name)
            out[os.path.realpath(os.path.join(LABEL_DIR, name))] = label
    except OSError:
        pass
    return out


def _containing(path: str, mounts: list[tuple[str, str, str]]) -> tuple[str, str, str] | None:
    path = os.path.realpath(path)
    best = None
    for dev, mp, fs in mounts:
        if path == mp or path.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(mp) > len(best[1]):
                best = (dev, mp, fs)
    return best


def _existing_parent(path: str) -> str:
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path or "/"


def _volume(path: str, label: str, kind: str, device: str, fstype: str) -> Volume:
    probe = _existing_parent(path)
    try:
        u = shutil.disk_usage(probe)
        total, free = u.total / 1024**3, u.free / 1024**3
    except OSError:
        total = free = 0.0
    return Volume(path, label, kind, device, fstype, round(total, 1), round(free, 2),
                  os.access(probe, os.W_OK | os.X_OK))


def list_volumes(default_dir: str, usb_auto: bool = True) -> list[Volume]:
    """Dossier par défaut en premier, puis les disques USB montés."""
    mounts = read_mounts()
    labels = _labels()
    home = _containing(default_dir, mounts)
    dev, mp, fs = home if home else ("", "/", "")
    kind = "usb" if dev.startswith(_USB_DEVICES) else "sd"
    label = labels.get(os.path.realpath(dev), "") if kind == "usb" else ""
    vols = [_volume(default_dir, label or ("Disque USB" if kind == "usb" else "Carte SD"), kind, dev, fs)]
    if usb_auto:
        seen = {dev}
        for d, m, f in mounts:
            if d in seen or not d.startswith(_USB_DEVICES) or m == "/":
                continue
            seen.add(d)
            name = labels.get(os.path.realpath(d)) or os.path.basename(m.rstrip("/")) or d
            vols.append(_volume(os.path.join(m, USB_SUBDIR), name, "usb", d, f))
    return vols
