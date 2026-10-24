#!/usr/bin/env python3
"""Faux `arecord` pour les tests : mêmes options utiles, écrit des WAV de silence.

Reproduit : -D -c -r -f -t --max-file-time, nom de fichier en dernier argument,
nommage name.wav puis name-01.wav, name-02.wav… à chaque rotation, finalisation
de l'en-tête sur SIGINT. Débit simulé : variable FAKE_ARECORD_BPS (octets/s, défaut 20000).
Si FAKE_ARECORD_OVERRUN=1, écrit « overrun!!! » sur stderr toutes les 0,2 s.
"""

import atexit
import math
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
    if "-l" in argv:  # liste des cartes (détection depuis la page de paramètres)
        print("**** List of CAPTURE Hardware Devices ****")
        print("card 1: XR18 [XR18], device 0: USB Audio [USB Audio]")
        return 0
    if "--dump-hw-params" in argv:  # ce que la carte accepte
        lines = ['HW Params of device "hw:CARD=XR18,DEV=0":', "--------------------",
                 "ACCESS:  MMAP_INTERLEAVED RW_INTERLEAVED", "FORMAT:  S24_3LE S32_LE",
                 "CHANNELS: 18", "RATE: [44100 48000]", "--------------------"]
        print("\n".join(lines), file=sys.stderr)
        return 0
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
    signal_on = os.environ.get("FAKE_ARECORD_SIGNAL") == "1"
    maxv = (1 << (8 * width - 1)) - 1
    t0, counter = time.monotonic(), [0]

    def frames(count):
        """Un sinus par voie (amplitude croissante) ; la voie 3 sature entre 1,0 s et 1,5 s après le début."""
        clip = 1.0 <= time.monotonic() - t0 < 1.5
        out = bytearray()
        for _ in range(count):
            k = counter[0]
            counter[0] += 1
            for c in range(ch):
                a = 0.04 + 0.05 * c
                v = int(a * math.sin(2 * math.pi * k / 32) * maxv)
                if clip and c == 2:
                    v = maxv if (k // 4) % 2 == 0 else -maxv - 1
                out += v.to_bytes(width, "little", signed=True)
        return bytes(out)
    signal.signal(signal.SIGINT, lambda *_: stop.update(v=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))
    lock = os.environ.get("FAKE_ARECORD_LOCK")  # simule une carte exclusive : un seul programme à la fois
    if lock:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            print("arecord: main:831: audio open error: Device or resource busy", file=sys.stderr)
            return 1
        atexit.register(lambda: os.path.exists(lock) and os.remove(lock))
    if name == "-":  # flux brut sur la sortie standard (contrôle des niveaux)
        out = sys.stdout.buffer
        while not stop["v"]:
            time.sleep(0.05)
            n = int(bps * 0.05) // (ch * width)
            try:
                out.write(frames(n) if signal_on else bytes(n * ch * width))
                out.flush()
            except (BrokenPipeError, OSError):
                break
        return 0
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
        f.write(frames(n // (ch * width)) if signal_on else bytes(n))
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
