"""Lecture d'en-têtes WAV multicanaux et séparation en fichiers mono.

Le module `wave` de Python 3.11 (Pi OS bookworm) ne lit pas WAVE_FORMAT_EXTENSIBLE
et refuse parfois les gros fichiers ; on parse donc les chunks soi-même.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass


@dataclass
class WavInfo:
    channels: int
    rate: int
    bits: int  # bits par échantillon (conteneur)
    data_offset: int
    data_size: int  # taille déclarée, ou réelle si l'en-tête n'a pas été finalisé

    @property
    def sample_bytes(self) -> int:
        return self.bits // 8

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.sample_bytes

    @property
    def frames(self) -> int:
        return self.data_size // self.frame_bytes

    @property
    def seconds(self) -> float:
        return self.frames / self.rate if self.rate else 0.0


class WavError(ValueError):
    pass


def read_info(path: str) -> WavInfo:
    """Lit l'en-tête. Tolère un fichier interrompu (taille du chunk data à 0 ou 0xFFFFFFFF)."""
    size_on_disk = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            raise WavError("pas un fichier WAV")
        channels = rate = bits = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                raise WavError("chunk data introuvable")
            cid, csize = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                body = f.read(csize + (csize & 1))
                tag, channels, rate, _brate, _align, bits = struct.unpack("<HHIIHH", body[:16])
                if tag not in (1, 0xFFFE):
                    raise WavError(f"format WAV non PCM (tag {tag:#x})")
            elif cid == b"data":
                offset = f.tell()
                remaining = size_on_disk - offset
                if channels is None:
                    raise WavError("chunk fmt absent avant data")
                if csize == 0 or csize == 0xFFFFFFFF or csize > remaining:
                    csize = remaining  # fichier non finalisé ou tronqué
                return WavInfo(channels, rate, bits, offset, csize)
            else:
                f.seek(csize + (csize & 1), 1)


def mono_header(rate: int, bits: int, data_size: int) -> bytes:
    block = bits // 8
    return (
        b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * block, block, bits)
        + b"data" + struct.pack("<I", data_size)
    )


def split_channels(paths: list[str], out_dir: str, prefix: str = "ch",
                   chunk_frames: int = 65536) -> list[str]:
    """Sépare un ou plusieurs WAV multicanaux (segments consécutifs) en un WAV mono par voie.

    Les segments sont concaténés dans l'ordre donné. Renvoie les chemins créés.
    Un fichier mono est limité à 4 Gio (≈ 6 h à 48 kHz / 32 bits).
    """
    if not paths:
        raise WavError("aucun fichier")
    infos = [read_info(p) for p in paths]
    ref = infos[0]
    for info in infos[1:]:
        if (info.channels, info.rate, info.bits) != (ref.channels, ref.rate, ref.bits):
            raise WavError("les segments n'ont pas le même format")
    total_frames = sum(i.frames for i in infos)
    mono_bytes = total_frames * ref.sample_bytes
    if mono_bytes >= 4 * 1024**3 - 1024:
        raise WavError("durée trop longue pour un WAV mono 32 bits de taille")

    os.makedirs(out_dir, exist_ok=True)
    out_paths = [os.path.join(out_dir, f"{prefix}{c + 1:02d}.wav") for c in range(ref.channels)]
    outs = []
    try:
        for p in out_paths:
            fo = open(p, "wb")
            outs.append(fo)
            fo.write(mono_header(ref.rate, ref.bits, mono_bytes))
        w, c = ref.sample_bytes, ref.channels
        for path, info in zip(paths, infos):
            left = info.frames
            with open(path, "rb") as f:
                f.seek(info.data_offset)
                while left > 0:
                    n = min(chunk_frames, left)
                    buf = f.read(n * info.frame_bytes)
                    n = len(buf) // info.frame_bytes
                    if n == 0:
                        break
                    buf = buf[: n * info.frame_bytes]
                    for ch in range(c):
                        out = bytearray(n * w)
                        for k in range(w):  # octet k de chaque échantillon de la voie ch
                            out[k::w] = buf[ch * w + k:: c * w]
                        outs[ch].write(out)
                    left -= n
    finally:
        for fo in outs:
            fo.close()
    return out_paths
