#!/usr/bin/env python3
"""Faux `arecord` pour les tests : mêmes options utiles, écrit des WAV de silence.

Reproduit : -D -c -r -f -t --max-file-time, nom de fichier en dernier argument,
nommage name.wav puis name-01.wav, name-02.wav… à chaque rotation, finalisation
de l'en-tête sur SIGINT. Débit simulé : variable FAKE_ARECORD_BPS (octets/s, défaut 20000).
Si FAKE_ARECORD_OVERRUN=1, écrit « overrun!!! » sur stderr toutes les 0,2 s.
"""

import os
import signal
import struct
import sys
import time

FMT = {"S16_LE": 2, "S24_3LE": 3, "S24_LE": 4, "S32_LE": 4}


def header(ch, rate, bits, size):
    block = ch * bits // 8
    return (b"RIFF" + struct.pack("<I", 36 + size) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, ch, rate, rate * block, block, bits)
            + b"data" + struct.pack("<I", size))


def main(argv):
    args = {"-c": "2", "-r": "48000", "-f": "S16_LE", "--max-file-time": "0"}
    i = 0
    name = None
    while i < len(argv):
        a = argv[i]
        if a in ("-D", "-t"):
            i += 2
        elif a in args:
            args[a] = argv[i + 1]
            i += 2
        elif a in ("-q", "-v"):
            i += 1
        else:
            name = a
            i += 1
    if name is None:
        print("arecord: pas de nom de fichier", file=sys.stderr)
        return 1
    ch, rate = int(args["-c"]), int(args["-r"])
    width = FMT[args["-f"]]
    seg = int(args["--max-file-time"])
    bps = int(os.environ.get("FAKE_ARECORD_BPS", "20000"))
    bps -= bps % (ch * width)
    stop = {"v": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(v=True))
    print(f"Recording WAVE '{name}' : {args['-f']}, Rate {rate} Hz, Channels {ch}", file=sys.stderr, flush=True)

    base, ext = os.path.splitext(name)
    index = 0
    cur = name
    f = open(cur, "wb")
    f.write(header(ch, rate, width * 8, 0))
    size = 0
    seg_start = time.monotonic()
    last_over = time.monotonic()
    tick = 0.05
    while not stop["v"]:
        time.sleep(tick)
        n = int(bps * tick) // (ch * width) * (ch * width)
        f.write(bytes(n))
        size += n
        if os.environ.get("FAKE_ARECORD_OVERRUN") == "1" and time.monotonic() - last_over > 0.2:
            print("overrun!!! (at least 1.000 ms long)", file=sys.stderr, flush=True)
            last_over = time.monotonic()
        if seg and time.monotonic() - seg_start >= seg:
            f.seek(0)
            f.write(header(ch, rate, width * 8, size))
            f.close()
            if index == 0:
                os.rename(cur, f"{base}-01{ext}")
            index += 1
            cur = f"{base}-{index + 1:02d}{ext}"
            f = open(cur, "wb")
            f.write(header(ch, rate, width * 8, 0))
            size = 0
            seg_start = time.monotonic()
    f.seek(0)
    f.write(header(ch, rate, width * 8, size))
    f.close()
    print("Aborted by signal Interrupt...", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
