"""Archive ZIP64 « stockée » (sans compression) écrite en flux, avec taille totale connue d'avance.

Les WAV ne se compressent pas et les sessions font plusieurs Gio : pas de fichier temporaire
(la carte SD n'a pas la place), et la taille exacte est calculée avant l'envoi, ce qui donne une
barre de progression au navigateur et permet de détecter un téléchargement tronqué.
Tout est en ZIP64 (lisible par l'Explorateur Windows, 7-Zip, unzip, Python).
"""

from __future__ import annotations

import os
import struct
import time
import zlib
from typing import BinaryIO

CHUNK = 1 << 20
_NAME_FLAGS = 0x0808  # bit 3 : CRC et tailles dans un descripteur après les données ; bit 11 : noms UTF-8
_VERSION = 45  # ZIP64


def _dos_datetime(ts: float) -> tuple[int, int]:
    t = time.localtime(ts)
    year = max(t.tm_year, 1980)
    return (t.tm_hour << 11 | t.tm_min << 5 | t.tm_sec // 2), ((year - 1980) << 9 | t.tm_mon << 5 | t.tm_mday)


class ZipStream:
    """Archive de fichiers existants : [(chemin_disque, nom_dans_l_archive)]."""

    def __init__(self, entries: list[tuple[str, str]]):
        self.entries = []
        for path, name in entries:
            st = os.stat(path)
            self.entries.append((path, name.encode("utf-8"), st.st_size, _dos_datetime(st.st_mtime)))

    @property
    def size(self) -> int:
        total = 0
        for _path, name, size, _dt in self.entries:
            total += 30 + len(name) + 20 + size + 24  # en-tête local + extra ZIP64 + données + descripteur
            total += 46 + len(name) + 28  # entrée du répertoire central + extra ZIP64
        return total + 56 + 20 + 22  # fin ZIP64, localisateur, fin classique

    def write_to(self, out: BinaryIO) -> int:
        offset, central = 0, []

        def put(data: bytes) -> None:
            nonlocal offset
            out.write(data)
            offset += len(data)

        for path, name, size, (dtime, ddate) in self.entries:
            local_offset = offset
            put(struct.pack("<IHHHHHIIIHH", 0x04034B50, _VERSION, _NAME_FLAGS, 0, dtime, ddate,
                            0, 0xFFFFFFFF, 0xFFFFFFFF, len(name), 20)
                + name + struct.pack("<HHQQ", 1, 16, 0, 0))
            crc, left = 0, size
            with open(path, "rb") as f:
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        raise OSError(f"fichier tronqué pendant la lecture : {path}")
                    crc = zlib.crc32(chunk, crc)
                    put(chunk)
                    left -= len(chunk)
            put(struct.pack("<IIQQ", 0x08074B50, crc, size, size))
            central.append((name, size, crc, dtime, ddate, local_offset))

        cd_offset = offset
        for name, size, crc, dtime, ddate, local_offset in central:
            put(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, _VERSION, _VERSION, _NAME_FLAGS, 0, dtime, ddate,
                            crc, 0xFFFFFFFF, 0xFFFFFFFF, len(name), 28, 0, 0, 0, 0o100644 << 16, 0xFFFFFFFF)
                + name + struct.pack("<HHQQQ", 1, 24, size, size, local_offset))
        cd_size = offset - cd_offset
        n = len(central)
        eocd64_offset = offset
        put(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, _VERSION, _VERSION, 0, 0, n, n, cd_size, cd_offset))
        put(struct.pack("<IIQI", 0x07064B50, 0, eocd64_offset, 1))
        put(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0))
        return offset
