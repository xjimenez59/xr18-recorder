"""Réglages déposés sur la partition de démarrage de la carte SD (lisible et modifiable depuis Windows).

Au démarrage du service, `/boot/firmware/xr18rec.json` est validé puis copié vers `/etc/xr18rec.json`.
Si le fichier est invalide, l'ancienne configuration est conservée et la raison est écrite dans
`xr18rec-erreur.txt` sur la même partition, pour être lue en rebranchant la carte sur un PC.
"""

from __future__ import annotations

import os
import shutil
import time

from . import config as config_mod

BOOT_DIR = "/boot/firmware"
DEST = "/etc/xr18rec.json"
NAME = "xr18rec.json"
ERROR_NAME = "xr18rec-erreur.txt"


def default_boot_dir(candidates=(BOOT_DIR, "/boot")) -> str:
    """Partition de démarrage : /boot/firmware (Bookworm, Trixie) ou /boot (systèmes plus anciens)."""
    for d in candidates:
        if os.path.isfile(os.path.join(d, "config.txt")):
            return d
    return candidates[0]


def sync(boot_dir: str = BOOT_DIR, dest: str = DEST) -> str:
    """Renvoie « absent », « copié » ou « invalide »."""
    src = os.path.join(boot_dir, NAME)
    err = os.path.join(boot_dir, ERROR_NAME)
    if not os.path.isfile(src):
        return "absent"
    try:
        config_mod.load(src)
    except (ValueError, OSError) as exc:  # JSON mal formé, clé inconnue, valeur hors limites…
        try:
            with open(err, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"Le fichier {NAME} n'a pas été pris en compte :\n{exc}\n\n"
                        "La configuration précédente reste en vigueur. Corrigez le fichier puis redémarrez le Pi.\n")
        except OSError:
            pass
        return "invalide"
    tmp = dest + ".tmp"
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)  # remplacement atomique : jamais de configuration à moitié écrite
    try:
        os.remove(err)  # une erreur passée ne doit plus être affichée
    except OSError:
        pass
    return "copié"
