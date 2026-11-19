#!/usr/bin/env python3
"""Faux `aplay` pour les tests du métronome : lit le PCM brut sur l'entrée standard et l'écrit dans un fichier.

FAKE_APLAY_OUT : fichier de sortie (obligatoire). FAKE_APLAY_SPEED : vitesse par rapport au temps réel
(défaut 20 : le pipe est ainsi vidé à cadence limitée, comme un vrai périphérique). FAKE_APLAY_FAIL=1 : échoue aussitôt.
FAKE_APLAY_UNDERRUN=1 : écrit « underrun!!! » sur stderr.
"""
import os
import signal
import sys
import time

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


def main(argv):
    if os.environ.get("FAKE_APLAY_FAIL"):
        print("aplay: main:830: audio open error: Device or resource busy", file=sys.stderr)
        return 1
    args = {"-c": "2", "-r": "48000", "-f": "S16_LE"}
    for i, a in enumerate(argv):
        if a in args:
            args[a] = argv[i + 1]
    width = {"S16_LE": 2, "S24_3LE": 3, "S24_LE": 4, "S32_LE": 4}[args["-f"]]
    frame = int(args["-c"]) * width
    speed = float(os.environ.get("FAKE_APLAY_SPEED", "20"))
    print("Playing raw data 'stdin' : fake", file=sys.stderr, flush=True)
    if os.environ.get("FAKE_APLAY_UNDERRUN"):
        print("underrun!!!", file=sys.stderr, flush=True)
    src = sys.stdin.buffer
    with open(os.environ["FAKE_APLAY_OUT"], "ab", buffering=0) as out:
        while True:
            data = src.read(frame * 4800)
            if not data:
                return 0
            out.write(data)
            time.sleep(len(data) / frame / int(args["-r"]) / speed)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
