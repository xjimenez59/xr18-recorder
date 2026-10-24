"""Complète l'agrandissement de la carte SD si le premier démarrage a été interrompu.

Raspberry Pi OS agrandit la partition puis le système de fichiers au *premier* démarrage seulement. Si le
Pi est débranché pendant l'opération (plusieurs minutes sur une grosse carte), le système de fichiers reste à
mi-chemin et rien ne la relance. Ce programme, lancé à chaque démarrage par un service, compare les tailles
(carte, partition, système de fichiers) et ne fait quelque chose que s'il manque de la place.
"""

from __future__ import annotations

import os
import re
import subprocess

SYS = "/sys"
RUN = subprocess.run  # remplaçable dans les tests
MIN_FREE_AFTER = 128 * 1024**2  # moins d'espace libre derrière la partition : on n'y touche pas
FS_SLACK = 16 * 1024**2  # écart toléré entre partition et système de fichiers (arrondis)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return RUN(cmd, capture_output=True, text=True)


def _read_int(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        return int(f.read().strip())


def needs(disk_sectors: int, start: int, size: int, fs_bytes: int, other_after: bool) -> tuple[bool, bool]:
    """(agrandir la partition ?, agrandir le système de fichiers ?) ; tailles en secteurs de 512 octets sauf fs."""
    grow_part = (not other_after) and (disk_sectors - (start + size)) * 512 >= MIN_FREE_AFTER
    grow_fs = grow_part or fs_bytes < size * 512 - FS_SLACK
    return grow_part, grow_fs


def _layout(part: str) -> tuple[str, int, int, int, int, bool]:
    """(disque, numéro, début, taille, taille du disque, autre partition derrière) d'après /sys."""
    real = os.path.realpath(os.path.join(SYS, "class", "block", part))
    disk = os.path.basename(os.path.dirname(real))
    number = _read_int(os.path.join(real, "partition"))
    start = _read_int(os.path.join(real, "start"))
    size = _read_int(os.path.join(real, "size"))
    disk_sectors = _read_int(os.path.join(os.path.dirname(real), "size"))
    other_after = False
    for name in os.listdir(os.path.dirname(real)):
        sib = os.path.join(os.path.dirname(real), name)
        if name != part and os.path.exists(os.path.join(sib, "partition")) and _read_int(os.path.join(sib, "start")) > start:
            other_after = True
    return disk, number, start, size, disk_sectors, other_after


def _fs_bytes(device: str) -> int | None:
    out = _run(["dumpe2fs", "-h", device]).stdout
    count, bsize = re.search(r"Block count:\s+(\d+)", out), re.search(r"Block size:\s+(\d+)", out)
    return int(count.group(1)) * int(bsize.group(1)) if count and bsize else None


def run() -> int:
    src = _run(["findmnt", "-no", "SOURCE,FSTYPE", "/"]).stdout.split()
    if len(src) < 2 or src[1] != "ext4" or not src[0].startswith("/dev/"):
        print("racine non ext4 sur un périphérique : rien à faire")
        return 0
    device = src[0]
    try:
        disk, number, start, size, disk_sectors, other_after = _layout(os.path.basename(device))
    except (OSError, ValueError) as exc:
        print(f"tailles illisibles ({exc}) : rien à faire")
        return 0
    fs_bytes = _fs_bytes(device)
    if fs_bytes is None:
        print("taille du système de fichiers illisible : rien à faire")
        return 0
    grow_part, grow_fs = needs(disk_sectors, start, size, fs_bytes, other_after)
    print(f"carte {disk_sectors * 512 / 1024**3:.1f} Gio, partition {size * 512 / 1024**3:.1f} Gio, "
          f"système de fichiers {fs_bytes / 1024**3:.1f} Gio")
    if not (grow_part or grow_fs):
        print("tout est à la bonne taille")
        return 0
    if grow_part:
        r = _run(["growpart", f"/dev/{disk}", str(number)])  # code 1 = rien à changer
        print(f"growpart : code {r.returncode} {(r.stdout or r.stderr).strip()}")
        if r.returncode not in (0, 1):
            return 1
    r = _run(["resize2fs", device])  # en ligne : le système reste utilisable pendant l'opération
    print(f"resize2fs : code {r.returncode} {(r.stdout or r.stderr).strip()}")
    return 0 if r.returncode == 0 else 1
