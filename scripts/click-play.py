#!/usr/bin/env python3
"""Prototype de métronome : envoie un clic sur UNE voie de retour USB de l'XR18 via aplay.

Les 18 voies de lecture sont envoyées (l'XR18 les attend toutes) ; seule la voie choisie porte le clic.
Chaque battement est placé à l'échantillon exact (round(n * 60 * rate / bpm)) : aucune dérive
cumulée côté Pi, l'horloge est celle de l'XR18 (aplay est cadencé par le tube).
Usage : click-play.py [--bpm 120] [--beats 4] [--channel 16] [--seconds 0] [--device hw:CARD=XR18,DEV=0]
"""
import argparse
import math
import struct
import subprocess
import sys

CH, RATE = 18, 48000
FMT = {"S32_LE": 4, "S24_3LE": 3}


def burst(rate, freq, ms, amp):
    n = int(rate * ms / 1000)
    return [int(amp * math.sin(2 * math.pi * freq * i / rate) * (1 - i / n) ** 2) for i in range(n)]


def sample_bytes(v, width):  # v : entier signé 24 bits
    if width == 4:
        return struct.pack("<i", v << 8)
    return struct.pack("<i", v)[:3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bpm", type=float, default=120)
    ap.add_argument("--beats", type=int, default=4, help="temps par mesure (accent sur le 1er)")
    ap.add_argument("--channel", type=int, default=16, help="voie de retour USB (1-18)")
    ap.add_argument("--seconds", type=float, default=0, help="0 = jusqu'à Ctrl+C")
    ap.add_argument("--device", default="hw:CARD=XR18,DEV=0")
    ap.add_argument("--format", default="S32_LE", choices=FMT)
    ap.add_argument("--rate", type=int, default=RATE)
    a = ap.parse_args()
    w = FMT[a.format]
    frame = CH * w
    silence = b"\0" * w
    hi = [sample_bytes(v, w) for v in burst(a.rate, 1500, 25, 6_000_000)]
    lo = [sample_bytes(v, w) for v in burst(a.rate, 1000, 25, 3_500_000)]

    def clip(samples):
        out = bytearray()
        for s in samples:
            out += silence * (a.channel - 1) + s + silence * (CH - a.channel)
        return bytes(out)

    accent, normal = clip(hi), clip(lo)
    cmd = ["aplay", "-q", "-D", a.device, "-c", str(CH), "-r", str(a.rate), "-f", a.format, "-t", "raw"]
    # --device - : écrit le PCM brut sur la sortie standard (essai hors Pi)
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE) if a.device != "-" else None
    out = p.stdin if p else sys.stdout.buffer
    beat = 0
    pos = 0  # trames déjà écrites
    total = int(a.seconds * a.rate) if a.seconds else None
    try:
        while total is None or pos < total:
            start = round(beat * 60 * a.rate / a.bpm)
            nxt = round((beat + 1) * 60 * a.rate / a.bpm)
            data = accent if beat % a.beats == 0 else normal
            gap = nxt - start - len(data) // frame
            out.write(data + b"\0" * (gap * frame))
            pos = nxt
            beat += 1
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        if p:
            try:
                p.stdin.close()
            except OSError:
                pass
            p.wait()
        else:
            out.flush()
    sys.exit(p.returncode if p and p.returncode else 0)


if __name__ == "__main__":
    main()
