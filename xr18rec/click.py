"""Métronome : un clic envoyé à l'XR18 par une voie de retour USB (via `aplay`).

Le Pi ne fait que fournir la référence de tempo. Sur la console, la voie dont la source est « USB n »
reçoit le clic (envoi vers les bus des retours in-ear, désassignée du LR) ; comme toute voie, elle est
aussi enregistrée sur la piste USB correspondante (point de prélèvement « Pre EQ » ou après, pas « Analog »).

Précision : chaque battement est placé à l'échantillon près (cumul en virgule flottante, un seul arrondi
par battement) ; l'horloge de lecture est celle de l'XR18. Mesure sur Pi 2 B (5 min, 120 bpm, enregistrement
en parallèle) : gigue ~0,7 ms (écart-type), tempo constant à ~0,04 % près de la consigne.
"""

from __future__ import annotations

import math
import subprocess
import threading
from collections import deque

from .config import FORMAT_BYTES, Config

SOUNDS = {
    # nom : (libellé, partiels [(rapport de fréquence, amplitude)], fréquence de base, constante de temps (s),
    #        durée (s), rapport de hauteur du temps 1)
    "bois": ("Bloc de bois", [(1.0, 1.0), (2.4, 0.3)], 900.0, 0.018, 0.12, 1.5),
    "cloche": ("Cloche (cowbell)", [(1.0, 1.0), (1.51, 0.8)], 560.0, 0.055, 0.2, 1.33),
    "bip": ("Bip doux", [(1.0, 1.0)], 1000.0, None, 0.045, 1.5),
    "tic": ("Tic sec", [(1.0, 1.0)], 2500.0, 0.003, 0.03, 1.5),
}
ATTACK_S = 0.002  # montée de 2 ms : pas de claquement (un début brutal est ce qui rend un clic « acide »)
FADE_S = 0.01  # descente finale : le son se termine à zéro, même s'il est coupé par un tempo rapide
MAX_CLIP_S = 0.2  # un battement ne dépasse jamais 200 ms (tempo maxi 240 bpm = 250 ms)
ACCENT_GAIN, NORMAL_GAIN, FLAT_GAIN = 1.0, 0.7, 0.85  # temps 1, autres temps, sans accent


class ClickError(RuntimeError):
    pass


def synth(sound: str, accent: bool, rate: int) -> list[float]:
    """Un battement, échantillons entre -1 et 1 (crête 1 avant gain)."""
    _label, partials, base, tau, length, ratio = SOUNDS[sound]
    freq = base * (ratio if accent else 1.0)
    n = int(min(length, MAX_CLIP_S) * rate)
    att, fade = max(1, int(ATTACK_S * rate)), max(1, int(FADE_S * rate))
    norm = sum(a for _r, a in partials)
    out = []
    for i in range(n):
        t = i / rate
        if tau is None:  # bip : enveloppe en cosinus surélevé (montée 5 ms, descente 15 ms)
            up, down = int(0.005 * rate), int(0.015 * rate)
            env = 0.5 - 0.5 * math.cos(math.pi * i / up) if i < up else 1.0
            if i >= n - down:
                env *= 0.5 + 0.5 * math.cos(math.pi * (i - (n - down)) / down)
        else:
            env = math.exp(-t / tau) * (min(1.0, i / att)) * (min(1.0, (n - 1 - i) / fade))
        s = sum(a * math.sin(2 * math.pi * freq * r * t) for r, a in partials) / norm
        out.append(s * env)
    return out


def encode(value: int, fmt: str) -> bytes:
    """Un échantillon (entier signé sur 24 bits) dans le format ALSA du périphérique."""
    if fmt == "S24_3LE":
        return (value & 0xFFFFFF).to_bytes(3, "little")
    if fmt == "S32_LE":
        return ((value << 8) & 0xFFFFFFFF).to_bytes(4, "little")
    if fmt == "S24_LE":
        return (value & 0xFFFFFFFF).to_bytes(4, "little")
    if fmt == "S16_LE":
        return ((value >> 8) & 0xFFFF).to_bytes(2, "little")
    raise ClickError(f"format non géré : {fmt}")


def render(sound: str, accent: bool, gain: float, level_db: float, channel: int, channels: int,
           fmt: str, rate: int, width: int) -> bytes:
    """Les trames (toutes les voies) d'un battement : le clic sur `channel` (1-based), silence ailleurs."""
    peak = (2**23 - 1) * 10 ** (level_db / 20) * gain
    quiet = bytes(width)
    before, after = quiet * (channel - 1), quiet * (channels - channel)
    out = bytearray()
    for s in synth(sound, accent, rate):
        out += before + encode(round(s * peak), fmt) + after
    return bytes(out)


class Metronome:
    """Pilote `aplay` : un fil écrit les battements à cadence exacte dans son entrée standard."""

    def __init__(self, cfg: Config, rec=None):
        self.cfg = cfg
        self.rec = rec  # Recorder : sert à arrêter le clic avec la prise (démarré par REC)
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._by_rec = False
        self._error: str | None = None
        self._underruns = 0
        self._tail: deque[str] = deque(maxlen=10)
        self._beat = 0  # numéro du battement dans la mesure (1..n), pour l'affichage

    # ---- état ------------------------------------------------------------
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None and not self._stop.is_set()

    def status(self) -> dict:
        c = self.cfg
        with self._lock:
            running = self.running()
            if self._proc is not None and not running and not self._stop.is_set() and self._error is None:
                self._error = f"aplay s'est arrêté (code {self._proc.returncode}) : " + " | ".join(self._tail)
            return {
                "running": running, "started_by_rec": running and self._by_rec, "error": self._error,
                "underruns": self._underruns, "beat": self._beat if running else 0,
                "bpm": c.click_bpm, "beats": c.click_beats, "accent": c.click_accent,
                "sound": c.click_sound, "level_db": c.click_level_db, "channel": c.click_channel,
                "with_rec": c.click_with_rec,
                "sounds": [{"id": k, "label": v[0]} for k, v in SOUNDS.items()],
            }

    # ---- commande ---------------------------------------------------------
    def start(self, by_rec: bool = False) -> dict:
        with self._lock:
            if self.running():
                return self.status()
            c = self.cfg
            if c.click_channel > c.channels:
                raise ClickError(f"voie du clic ({c.click_channel}) au-delà des {c.channels} voies de la carte")
            self._reap()
            cmd = [c.aplay_bin, "-D", c.device, "-c", str(c.channels), "-r", str(c.rate), "-f", c.format, "-t", "raw"]
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, text=False, bufsize=0)
            except OSError as exc:
                raise ClickError(f"lancement d'aplay impossible : {exc}") from exc
            self._proc, self._by_rec, self._error, self._underruns, self._beat = proc, by_rec, None, 0, 0
            self._tail.clear()
            self._stop = stop = threading.Event()
            threading.Thread(target=self._read_stderr, args=(proc,), daemon=True, name="click-stderr").start()
            self._thread = threading.Thread(target=self._loop, args=(proc, stop), daemon=True, name="click")
            self._thread.start()
            if by_rec and self.rec is not None:
                threading.Thread(target=self._follow_rec, args=(stop,), daemon=True, name="click-follow").start()
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            self._stop.set()
            proc, thread = self._proc, self._thread
            if proc is not None and proc.poll() is None:
                proc.terminate()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        if proc is not None:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        with self._lock:
            self._by_rec = False
            return self.status()

    def _reap(self) -> None:
        if self._proc is not None:
            for pipe in (self._proc.stdin, self._proc.stderr):
                try:
                    pipe and pipe.close()
                except OSError:
                    pass

    def on_rec_started(self) -> None:
        """Appelé au début d'une prise : démarre le clic si l'option « avec REC » est active."""
        if self.cfg.click_with_rec and not self.running():
            try:
                self.start(by_rec=True)
            except ClickError as exc:  # ne doit jamais gêner l'enregistrement
                with self._lock:
                    self._error = str(exc)

    def _follow_rec(self, stop: threading.Event) -> None:
        """Clic démarré par REC : il s'arrête avec la prise (arrêt, ou prise interrompue)."""
        while not stop.wait(0.3):
            if not self.rec.is_recording():
                self.stop()
                return

    # ---- fils ---------------------------------------------------------------
    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._tail.append(line)
                if "underrun" in line.lower():
                    self._underruns += 1

    def _loop(self, proc: subprocess.Popen, stop: threading.Event) -> None:
        c = self.cfg
        rate, channels, fmt = c.rate, c.channels, c.format
        width = FORMAT_BYTES[fmt]
        frame = channels * width
        silence = bytes(frame * (rate // 4))  # le silence entre deux clics s'écrit par morceaux de 250 ms
        cache: dict[tuple, bytes] = {}
        acc, pos, beat = 0.0, 0, 0
        try:
            while not stop.is_set():
                bpm, beats = c.click_bpm, c.click_beats  # relus à chaque battement : réglables en marche
                first = beat == 0
                if c.click_accent:
                    accent, gain = first and beats > 1, (ACCENT_GAIN if first and beats > 1 else NORMAL_GAIN)
                else:
                    accent, gain = False, FLAT_GAIN
                key = (c.click_sound, accent, gain, c.click_level_db, c.click_channel)
                if key not in cache:
                    if len(cache) > 16:
                        cache.clear()
                    cache[key] = render(c.click_sound, accent, gain, c.click_level_db, c.click_channel, channels,
                                        fmt, rate, width)
                data = cache[key]
                acc += 60.0 * rate / bpm
                nxt = round(acc)
                n = nxt - pos
                pos = nxt
                self._beat = beat + 1
                data = data[:n * frame]
                proc.stdin.write(data)
                left = n - len(data) // frame
                while left > 0 and not stop.is_set():
                    k = min(left, rate // 4)
                    proc.stdin.write(silence[:k * frame])
                    left -= k
                beat = (beat + 1) % beats
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
