"""Montage automatique des disques USB (appelé par un service systemd lancé par udev, en root).

Chaque partition est montée dans `/media/xr18/<étiquette>` ; le dossier `xr18-recordings` est créé
et confié à l'utilisateur du service sur les systèmes de fichiers Linux. Sur FAT/exFAT/NTFS, les droits
sont fixés à l'option de montage (uid/gid). L'application détecte ensuite le disque via /proc/mounts.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

MOUNT_ROOT = "/media/xr18"
SERVICE_USER = "xr18rec"
RECORD_DIR = "xr18-recordings"  # même nom que storage.USB_SUBDIR
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_LINUX_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs"}
_SKIP_FS = {"swap", "LVM2_member", "crypto_LUKS", "linux_raid_member", "squashfs", "iso9660", "udf"}


@dataclass
class Plan:
    device: str
    mountpoint: str
    fstype: str  # type passé à mount -t
    options: str
    own_dir: bool  # créer et confier <montage>/xr18-recordings au service


def parse_blkid(text: str) -> dict[str, str]:
    props = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    return props


def plan(device: str, props: dict[str, str], uid: int, gid: int, root: str = MOUNT_ROOT) -> Plan | None:
    """Décide comment monter ; None si ce n'est pas un système de fichiers à monter."""
    fs = props.get("TYPE", "")
    if not fs or fs in _SKIP_FS:
        return None
    name = _SAFE.sub("_", props.get("LABEL") or props.get("UUID") or os.path.basename(device)).strip("._-") \
        or os.path.basename(device)
    mountpoint = os.path.join(root, name)
    owner = f"uid={uid},gid={gid},umask=022"
    if fs in _LINUX_FS:
        return Plan(device, mountpoint, fs, "noatime", True)
    if fs == "vfat":
        return Plan(device, mountpoint, "vfat", f"{owner},noatime,flush", False)
    if fs == "exfat":
        return Plan(device, mountpoint, "exfat", f"{owner},noatime", False)
    if fs == "ntfs":
        return Plan(device, mountpoint, "ntfs-3g", f"{owner},noatime", False)
    return None


def _mounted_at(device: str) -> str | None:
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0] == device:
                    return parts[1].replace("\\040", " ")
    except OSError:
        pass
    return None


def mount(device: str) -> int:
    props = parse_blkid(subprocess.run(["blkid", "-o", "export", device], capture_output=True, text=True).stdout)
    import pwd  # Unix seulement : importé ici pour que le module reste importable ailleurs (tests)

    user = pwd.getpwnam(SERVICE_USER)
    p = plan(device, props, user.pw_uid, user.pw_gid)
    if p is None:
        print(f"{device} : rien à monter ({props.get('TYPE', 'type inconnu')})")
        return 0
    if _mounted_at(device):
        return 0
    os.makedirs(p.mountpoint, exist_ok=True)
    subprocess.run(["mount", "-t", p.fstype, "-o", p.options, device, p.mountpoint], check=True)
    if p.own_dir:
        target = os.path.join(p.mountpoint, RECORD_DIR)
        os.makedirs(target, exist_ok=True)
        os.chown(target, user.pw_uid, user.pw_gid)
    print(f"{device} monté sur {p.mountpoint} ({p.fstype})")
    return 0


def umount(device: str) -> int:
    mp = _mounted_at(device)
    if mp:
        subprocess.run(["umount", "-l", mp], check=False)  # -l : le disque a pu être arraché
        try:
            os.rmdir(mp)
        except OSError:
            pass
    return 0
