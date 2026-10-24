"""Écoute d'une session : le mix de la console (par défaut les voies USB 17/18) en WAV stéréo 16 bits à la demande.

Aucun calcul de mixage : on extrait deux voies du WAV multicanal en découpant des octets (rapide, même sur un Pi 2).
Le résultat est présenté comme un fichier WAV « virtuel » de taille connue : le navigateur le lit avec sa balise audio
et se positionne n'importe où grâce aux requêtes HTTP `Range` ; les segments de la prise (take.wav, take-01.wav…) sont
vus comme un seul fichier continu.
"""

from __future__ import annotations

import json
import os
import struct
from typing import Iterator

from . import meters, wavtools

HEADER_BYTES = 44
FRAME_BYTES = 4  # stéréo 16 bits
CHUNK_FRAMES = 8192  # ~170 ms d'audio par lecture de fichier (le disque n'est jamais lu en gros blocs d'un coup)
_BITS_TO_FORMAT = {16: "S16_LE", 24: "S24_3LE", 32: "S32_LE"}


class PlaybackError(RuntimeError):
    pass


def _recorded_format(directory: str) -> str | None:
    try:
        with open(os.path.join(directory, "recording.json"), encoding="utf-8") as f:
            return json.load(f).get("format")
    except (OSError, ValueError):
        return None


class MixSource:
    """Mix stéréo 16 bits d'une session, lisible par plages d'octets."""

    def __init__(self, directory: str, mix: tuple[int, int] = (17, 18)):
        from .reaper import find_segments  # import tardif : reaper dépend de meters

        self.segments = []
        for name in find_segments(directory):
            path = os.path.join(directory, name)
            try:
                self.segments.append((path, wavtools.read_info(path)))
            except (OSError, ValueError):
                continue
        if not self.segments:
            raise PlaybackError("cette session ne contient aucun fichier audio lisible")
        ref = self.segments[0][1]
        if any((i.channels, i.rate, i.bits) != (ref.channels, ref.rate, ref.bits) for _p, i in self.segments):
            raise PlaybackError("les segments de la session n'ont pas tous le même format")
        self.rate, self.channels, self.bits, self.width = ref.rate, ref.channels, ref.bits, ref.sample_bytes
        fmt = _recorded_format(directory) or _BITS_TO_FORMAT.get(self.bits, "S32_LE")
        self.msb = meters.msb_offset(fmt, self.width)
        left, right = mix[0] - 1, mix[1] - 1
        self.fallback = max(left, right) >= self.channels or min(left, right) < 0
        if self.fallback:  # moins de voies que prévu : on prend les deux premières (ou la seule)
            left, right = 0, min(1, self.channels - 1)
        self.left, self.right = left, right
        self.total_frames = sum(i.frames for _p, i in self.segments)
        self.data_bytes = self.total_frames * FRAME_BYTES
        self.size = HEADER_BYTES + self.data_bytes
        self._starts = []
        acc = 0
        for _p, info in self.segments:
            self._starts.append(acc)
            acc += info.frames

    @property
    def duration_s(self) -> float:
        return self.total_frames / self.rate

    def header(self) -> bytes:
        data = min(self.data_bytes, 0xFFFFFFFF)  # au-delà de 4 Gio (≈ 6 h) le champ est saturé ; Content-Length fait foi
        return (b"RIFF" + struct.pack("<I", min(36 + self.data_bytes, 0xFFFFFFFF)) + b"WAVE" + b"fmt "
                + struct.pack("<IHHIIHH", 16, 1, 2, self.rate, self.rate * FRAME_BYTES, FRAME_BYTES, 16)
                + b"data" + struct.pack("<I", data))

    def read_frames(self, start: int, count: int) -> bytes:
        """`count` trames stéréo 16 bits à partir de la trame `start` (complétées de silence au-delà de la fin)."""
        out = bytearray()
        want = max(0, min(count, self.total_frames - start))
        for (path, info), seg_start in zip(self.segments, self._starts):
            if want <= 0:
                break
            if start >= seg_start + info.frames:
                continue
            n = min(want, seg_start + info.frames - start)
            with open(path, "rb") as f:
                f.seek(info.data_offset + (start - seg_start) * info.frame_bytes)
                raw = f.read(n * info.frame_bytes)
            got = len(raw) // info.frame_bytes
            out += self._stereo16(raw[: got * info.frame_bytes], got)
            if got < n:  # fichier plus court qu'annoncé (arrêt brutal) : silence
                out += bytes((n - got) * FRAME_BYTES)
            start, want = start + n, want - n
        out += bytes(max(0, count - len(out) // FRAME_BYTES) * FRAME_BYTES) if start >= self.total_frames else b""
        return bytes(out)

    def _stereo16(self, raw: bytes, frames: int) -> bytes:
        frame, w, hi = self.channels * self.width, self.width, self.msb
        out = bytearray(frames * FRAME_BYTES)
        out[0::4] = raw[self.left * w + hi - 1:: frame]
        out[1::4] = raw[self.left * w + hi:: frame]
        out[2::4] = raw[self.right * w + hi - 1:: frame]
        out[3::4] = raw[self.right * w + hi:: frame]
        return bytes(out)

    def iter_range(self, first: int, last: int) -> Iterator[bytes]:
        """Octets `first`..`last` (inclus) du fichier virtuel, sans jamais charger la session en mémoire."""
        pos = first
        if pos < HEADER_BYTES:
            yield self.header()[pos: min(last, HEADER_BYTES - 1) + 1]
            pos = HEADER_BYTES
        while pos <= last:
            frame, skip = divmod(pos - HEADER_BYTES, FRAME_BYTES)
            need = last - pos + 1
            data = self.read_frames(frame, min(CHUNK_FRAMES, -(-(need + skip) // FRAME_BYTES)))
            piece = data[skip: skip + need]
            if not piece:
                return
            yield piece
            pos += len(piece)
