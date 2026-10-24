"""Écoute d'une session : le mix de la console (WAV stéréo à la demande) et les voies une à une (écoute multipiste).

Aucun calcul de mixage sur le Pi : on extrait des voies du WAV multicanal en découpant des octets (rapide, même
sur un Pi 2). Les segments de la prise (take.wav, take-01.wav…) sont vus comme un seul fichier continu.
- `MixSource` : deux voies (17/18 par défaut) présentées comme un fichier WAV stéréo 16 bits « virtuel » de taille
  connue ; le navigateur le lit avec sa balise audio et se positionne grâce aux requêtes HTTP `Range`.
- `SessionAudio.channels16` : plusieurs voies mono 16 bits pour une plage de temps, que le navigateur mélange
  lui-même (muets, solos) avec Web Audio.
"""

from __future__ import annotations

import json
import os
import struct
from typing import Iterator

from . import meters, wavtools
from .xr18osc import NUM_INPUTS, color_rgb

HEADER_BYTES = 44
FRAME_BYTES = 4  # stéréo 16 bits
CHUNK_FRAMES = 8192  # ~170 ms d'audio par lecture de fichier
MAX_PCM_SECONDS = 10.0  # durée maximale demandée en une fois pour les voies une à une
_BITS_TO_FORMAT = {16: "S16_LE", 24: "S24_3LE", 32: "S32_LE"}


class PlaybackError(RuntimeError):
    pass


def _recorded_format(directory: str) -> str | None:
    try:
        with open(os.path.join(directory, "recording.json"), encoding="utf-8") as f:
            return json.load(f).get("format")
    except (OSError, ValueError):
        return None


class SessionAudio:
    """Les fichiers audio d'une session, vus comme un seul flux de trames multicanal."""

    def __init__(self, directory: str):
        from .reaper import find_segments  # import tardif : reaper dépend de meters

        self.directory = directory
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
        self.total_frames = sum(i.frames for _p, i in self.segments)
        self._starts = []
        acc = 0
        for _p, info in self.segments:
            self._starts.append(acc)
            acc += info.frames

    @property
    def duration_s(self) -> float:
        return self.total_frames / self.rate

    def _pieces(self, start: int, count: int) -> Iterator[tuple[bytes | None, int]]:
        """Morceaux (octets bruts, nombre de trames) couvrant [start, start+count) ; (None, n) = n trames de silence."""
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
            if got:
                yield raw[: got * info.frame_bytes], got
            if got < n:  # fichier plus court qu'annoncé (arrêt brutal) : silence
                yield None, n - got
            start, want = start + n, want - n

    def _mono16(self, raw: bytes, frames: int, ch: int) -> bytes:
        """Voie `ch` en 16 bits little endian : les deux octets de poids fort de chaque échantillon."""
        frame, w, hi = self.channels * self.width, self.width, self.msb
        out = bytearray(frames * 2)
        out[0::2] = raw[ch * w + hi - 1:: frame]
        out[1::2] = raw[ch * w + hi:: frame]
        return bytes(out)

    def channels16(self, chans: list[int], start: int, count: int) -> list[bytes]:
        """Voies `chans` (numéros à partir de 0), mono 16 bits, pour [start, start+count) ; tronqué à la fin de la session.

        Le fichier n'est lu qu'une fois, quel que soit le nombre de voies demandées."""
        planes: list[list[bytes]] = [[] for _ in chans]
        for raw, frames in self._pieces(start, count):
            for i, ch in enumerate(chans):
                planes[i].append(bytes(frames * 2) if raw is None else self._mono16(raw, frames, ch))
        return [b"".join(p) for p in planes]


class MixSource(SessionAudio):
    """Mix stéréo 16 bits d'une session, lisible par plages d'octets."""

    def __init__(self, directory: str, mix: tuple[int, int] = (17, 18)):
        super().__init__(directory)
        left, right = mix[0] - 1, mix[1] - 1
        self.fallback = max(left, right) >= self.channels or min(left, right) < 0
        if self.fallback:  # moins de voies que prévu : on prend les deux premières (ou la seule)
            left, right = 0, min(1, self.channels - 1)
        self.left, self.right = left, right
        self.data_bytes = self.total_frames * FRAME_BYTES
        self.size = HEADER_BYTES + self.data_bytes

    def header(self) -> bytes:
        data = min(self.data_bytes, 0xFFFFFFFF)  # au-delà de 4 Gio (≈ 6 h) le champ est saturé ; Content-Length fait foi
        return (b"RIFF" + struct.pack("<I", min(36 + self.data_bytes, 0xFFFFFFFF)) + b"WAVE" + b"fmt "
                + struct.pack("<IHHIIHH", 16, 1, 2, self.rate, self.rate * FRAME_BYTES, FRAME_BYTES, 16)
                + b"data" + struct.pack("<I", data))

    def read_frames(self, start: int, count: int) -> bytes:
        """`count` trames stéréo 16 bits à partir de la trame `start` (complétées de silence au-delà de la fin)."""
        left, right = self.channels16([self.left, self.right], start, count)
        out = bytearray(len(left) // 2 * FRAME_BYTES)
        out[0::4], out[1::4] = left[0::2], left[1::2]
        out[2::4], out[3::4] = right[0::2], right[1::2]
        return bytes(out) + bytes(max(0, count - len(out) // FRAME_BYTES) * FRAME_BYTES) if start >= self.total_frames else bytes(out)

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


def track_infos(directory: str, audio: SessionAudio, names: list[str], layout=None) -> list[dict]:
    """Les voies de la session pour la page d'écoute : nom, couleur, crête de la prise, voie silencieuse ?

    Une voie est dite silencieuse si le bilan de niveaux de la prise (levels.json) n'a mesuré aucun signal dessus
    (crête nulle : voie mutée sur la console mais envoyée quand même en USB) ET que l'analyse était complète ;
    sans bilan fiable on ne cache rien."""
    from .reaper import load_levels

    levels = load_levels(directory) or {}
    reliable = bool(levels) and not levels.get("skipped_s", 0)
    by_channel = {c.get("channel"): c for c in levels.get("channels", [])}
    out = []
    for i in range(audio.channels):
        entry = by_channel.get(i + 1)
        rgb = None
        if layout is not None:
            rgb = color_rgb((layout.channels[i] if i < NUM_INPUTS else layout.main).color) if i < NUM_INPUTS + 2 else None
        peak = entry.get("peak_dbfs") if entry else None
        out.append({"index": i + 1, "name": names[i], "color": "#%02x%02x%02x" % rgb if rgb else None,
                    "peak_db": peak, "clipped": bool(entry and entry.get("clip_samples")),
                    "silent": bool(reliable and entry is not None and peak is None)})
    return out
