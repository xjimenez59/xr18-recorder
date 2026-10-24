"""Niveaux de crête par voie : pendant la prise (relecture des WAV en cours d'écriture) ou avant la prise
(flux audio direct de la carte, voir `Recorder.monitor_start`).

Dans les deux cas l'audio est analysé en entier (pas d'échantillonnage), donc une saturation courte
n'échappe pas à la mesure. Pendant la prise, la mesure ne fait que *lire* les fichiers : si elle échoue ou
prend du retard, l'enregistrement n'en souffre pas.

Résolution : l'octet de poids fort de chaque échantillon (8 bits, 0..128) ; fiable de 0 à -42 dB pleine
échelle, ce qui suffit pour voir une voie vivante et détecter une saturation, pas pour mesurer un bruit de fond.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque

from . import wavtools

LEVELS_FILE = "levels.json"
FULL_SCALE = 128  # amplitude de l'octet de poids fort à pleine échelle
CLIP_MIN = 127  # >= 127/128 de la pleine échelle (-0.07 dBFS) : saturation
MAX_EVENTS = 100  # instants de saturation conservés par voie
EVENT_GAP_S = 1.0  # deux saturations plus proches comptent pour un seul événement
MAX_LAG_S = 3.0  # retard d'analyse toléré ; au-delà, on saute l'audio ancien (le calcul ne doit jamais emballer le Pi)
WINDOW_S = 0.3  # durée d'audio sur laquelle se lit le niveau instantané affiché
# octet signé (complément à 2) -> amplitude 0..128
_ABS = bytes(min(128, abs(b - 256 if b >= 128 else b)) for b in range(256))
# position, dans l'échantillon, de l'octet de poids fort du signal (S24_LE : 24 bits dans un conteneur de 32)
_MSB = {"S16_LE": 1, "S24_3LE": 2, "S24_LE": 2, "S32_LE": 3}


def msb_offset(fmt: str, width: int) -> int:
    return _MSB.get(fmt, width - 1)


def dbfs(magnitude: int) -> float | None:
    """Amplitude 0..128 -> dB pleine échelle ; None pour le silence (moins l'infini)."""
    return None if magnitude <= 0 else round(20 * math.log10(magnitude / FULL_SCALE), 1)


def window_stats(buf: bytes, channels: int, width: int, msb: int | None = None) -> list[tuple[int, int, int]]:
    """Par voie : (crête 0..128, nombre d'échantillons saturés, indice de la première trame saturée ou -1).

    `buf` contient des trames entières de `channels` échantillons de `width` octets (little endian)."""
    frame = channels * width
    msb = width - 1 if msb is None else msb
    out = []
    for c in range(channels):
        chunk = buf[c * width + msb:: frame].translate(_ABS)  # octet de poids fort de la voie c
        peak = max(chunk) if chunk else 0
        clips, first = 0, -1
        if peak >= CLIP_MIN:
            clips = chunk.count(b"\x7f") + chunk.count(b"\x80")
            finds = [i for i in (chunk.find(b"\x7f"), chunk.find(b"\x80")) if i >= 0]
            first = min(finds) if finds else -1
        out.append((peak, clips, first))
    return out


class LevelMonitor:
    """Analyse l'audio d'une prise (`directory` : relecture des fichiers) ou d'un flux (`directory=None` : `feed`)."""

    def __init__(self, directory: str | None, rate: int, channels: int, width: int, msb: int | None = None):
        self.dir, self.rate, self.channels, self.width, self.msb = directory, rate, channels, width, msb
        self.done = 0  # trames déjà analysées (ou sautées), toutes pièces confondues
        self.skipped = 0  # trames sautées faute de puissance de calcul : l'analyse est alors partielle
        self.error: str | None = None
        self._lock = threading.Lock()
        self._recent: deque = deque()  # (trame de fin, crêtes) sur les WINDOW_S dernières secondes d'audio
        self._hold = [0] * channels  # crête affichée (remise à zéro possible)
        self._peak = [0] * channels  # crête de la prise (jamais remise à zéro)
        self._clips = [0] * channels
        self._events: list[list[float]] = [[] for _ in range(channels)]
        self._last_event = [-1e9] * channels
        self._carry = b""  # fin de flux : trame incomplète en attente de la suite
        self._saved = -1
        self._busy = 0.0  # secondes de calcul passées à analyser
        self._processed = 0  # trames réellement analysées (hors trames sautées)

    # ---- analyse ----------------------------------------------------------
    def _process(self, data: bytes) -> int:
        """Analyse des trames entières par blocs de 100 ms (instants précis, saturations distinctes non fondues)."""
        frame_bytes = self.channels * self.width
        frames = len(data) // frame_bytes
        block = max(1, self.rate // 10)
        t0 = time.perf_counter()
        with self._lock:
            for first_frame in range(0, frames, block):
                chunk = data[first_frame * frame_bytes: min(frames, first_frame + block) * frame_bytes]
                start = self.done + first_frame
                stats = window_stats(chunk, self.channels, self.width, self.msb)
                n = len(chunk) // frame_bytes
                self._recent.append((start + n, [s[0] for s in stats]))
                for c, (peak, clips, first) in enumerate(stats):
                    self._hold[c] = max(self._hold[c], peak)
                    self._peak[c] = max(self._peak[c], peak)
                    if clips:
                        self._clips[c] += clips
                        t = (start + first) / self.rate
                        if t - self._last_event[c] >= EVENT_GAP_S and len(self._events[c]) < MAX_EVENTS:
                            self._events[c].append(round(t, 2))
                            self._last_event[c] = t
            self.done += frames
            self._processed += frames
            self._busy += time.perf_counter() - t0
            horizon = self.done - int(WINDOW_S * self.rate)
            while len(self._recent) > 1 and self._recent[0][0] <= horizon:
                self._recent.popleft()
        return frames

    def feed(self, data: bytes) -> int:
        """Flux direct : ajoute des octets (une trame coupée est complétée au prochain appel)."""
        data = self._carry + data
        whole = self.channels * self.width
        usable = len(data) // whole * whole
        self._carry = data[usable:]
        return self._process(data[:usable]) if usable else 0

    # ---- lecture des fichiers d'une prise ---------------------------------
    def _segments(self):
        from .reaper import find_segments  # import tardif : reaper dépend de ce module

        out = []
        for name in find_segments(self.dir):
            path = os.path.join(self.dir, name)
            try:
                out.append((path, wavtools.read_info(path)))
            except (OSError, ValueError):
                continue
        return out

    def step(self, max_frames: int | None = None) -> int:
        """Analyse l'audio écrit depuis le dernier appel ; renvoie le nombre de trames traitées."""
        segs = self._segments()
        total = sum(i.frames for _p, i in segs)
        lag = total - self.done - int(MAX_LAG_S * self.rate)
        if lag > 0:  # trop de retard : on repart du présent proche, en le signalant
            with self._lock:
                self.done += lag
                self.skipped += lag
        want = min(total - self.done, max_frames or self.rate * 2)
        if want <= 0:
            return 0
        buf, pos, left, acc = bytearray(), self.done, want, 0
        for path, info in segs:
            if pos >= acc + info.frames:
                acc += info.frames
                continue
            n = min(left, acc + info.frames - pos)
            with open(path, "rb") as f:
                f.seek(info.data_offset + (pos - acc) * info.frame_bytes)
                data = f.read(n * info.frame_bytes)
            got = len(data) // info.frame_bytes
            buf += data[: got * info.frame_bytes]
            pos, left, acc = pos + got, left - got, acc + info.frames
            if got < n or left <= 0:
                break
        return self._process(bytes(buf)) if len(buf) >= self.channels * self.width else 0

    def run(self, alive, interval: float = 0.1, save_every: float = 30.0) -> None:
        """Boucle d'analyse tant que `alive()` ; vide le reste et enregistre à la fin."""
        last_save = time.monotonic()
        while alive():
            try:
                self.step()
            except Exception as exc:  # jamais d'incidence sur l'enregistrement
                self.error = f"{type(exc).__name__}: {exc}"
            time.sleep(interval)
            if time.monotonic() - last_save >= save_every:
                self.save()
                last_save = time.monotonic()
        try:
            for _ in range(200):  # fichiers finalisés : on analyse ce qui restait
                if not self.step():
                    break
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        self.save()

    # ---- résultats --------------------------------------------------------
    def _window(self) -> list[int]:
        return [max((peaks[c] for _end, peaks in self._recent), default=0) for c in range(self.channels)]

    def skipped_seconds(self) -> float:
        return self.skipped / self.rate

    def reset_display(self) -> None:
        """Remet à zéro les crêtes affichées (le bilan de la prise, lui, ne change pas)."""
        with self._lock:
            self._hold = self._window()

    def snapshot(self) -> dict:
        with self._lock:
            window = self._window()
            chans = [{"window_db": dbfs(window[c]), "peak_db": dbfs(self._hold[c]),
                      "take_peak_db": dbfs(self._peak[c]), "clipped": self._clips[c] > 0,
                      "clip_samples": self._clips[c], "events": list(self._events[c])}
                     for c in range(self.channels)]
            load = 100 * self._busy / (self._processed / self.rate) if self._processed else 0.0
            return {"has_data": self.done > 0, "elapsed_s": round(self.done / self.rate, 1),
                    "skipped_s": round(self.skipped / self.rate, 1), "load_pct": round(load, 1),
                    "channels": chans, "error": self.error}

    def report(self) -> dict:
        snap = self.snapshot()
        return {"version": 1, "rate": self.rate, "duration_s": snap["elapsed_s"], "skipped_s": snap["skipped_s"],
                "load_pct": snap["load_pct"],
                "channels": [{"channel": i + 1, "peak_dbfs": c["take_peak_db"], "clip_samples": c["clip_samples"],
                              "events": c["events"]} for i, c in enumerate(snap["channels"])]}

    def save(self) -> None:
        """Bilan de la prise dans la session (levels.json), réécrit seulement s'il a changé."""
        if self.dir is None or self.done == self._saved:
            return
        tmp = os.path.join(self.dir, LEVELS_FILE + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.report(), f, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(self.dir, LEVELS_FILE))
            self._saved = self.done
        except OSError as exc:
            self.error = f"levels.json non écrit : {exc}"
