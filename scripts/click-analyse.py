#!/usr/bin/env python3
"""Mesure la régularité d'un clic enregistré : détecte les débuts de clic sur une voie d'un WAV
multivoie (plusieurs segments possibles, dans l'ordre) et compare à une grille idéale.
Usage : click-analyse.py --bpm 120 --channel 16 take-01.wav take-02.wav ...
Sortie : nombre de clics, intervalle moyen, dérive (ppm) par rapport au tempo théorique,
écart max à la droite ajustée (gigue, ms), clics manquants ou en trop.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from xr18rec.wavtools import read_info  # noqa: E402


_LOUD = re.compile(bytes([91,92,120,49,48,45,92,120,102,48,93]))  # [-ð] : octet de poids fort >= 16 en valeur absolue (~ -18 dBFS)


def onsets(paths, channel, dead_s=0.1):
    """Débuts de clic (en trames depuis le début du 1er fichier) sur la voie 1-based `channel`."""
    found, base = [], 0
    last = -10**12
    for path in paths:
        info = read_info(path)
        sb, fb = info.sample_bytes, info.frame_bytes
        off = (channel - 1) * sb
        dead = int(dead_s * info.rate)
        with open(path, "rb") as f:
            f.seek(info.data_offset)
            remaining = info.frames
            idx = 0
            while remaining:
                n = min(remaining, 1 << 16)
                buf = f.read(n * fb)
                n = len(buf) // fb
                if not n:
                    break
                hs = buf[off + sb - 1::fb]  # octet de poids fort de la voie, une valeur par trame
                for m in _LOUD.finditer(hs):
                    pos = base + idx + m.start()
                    if pos - last > dead:
                        found.append(pos)
                        last = pos
                idx += n
                remaining -= n
        base += info.frames
        rate = info.rate
    return found, rate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bpm", type=float, required=True)
    ap.add_argument("--channel", type=int, default=16)
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    pos, rate = onsets(a.files, a.channel)
    if len(pos) < 3:
        sys.exit(f"Seulement {len(pos)} clic(s) détecté(s) : voie ou seuil à revoir.")
    period = 60 * rate / a.bpm
    k = [round((p - pos[0]) / period) for p in pos]
    n = len(k)
    mk = sum(k) / n
    mp = sum(pos) / n
    slope = sum((x - mk) * (y - mp) for x, y in zip(k, pos)) / sum((x - mk) ** 2 for x in k)
    icpt = mp - slope * mk
    resid = [y - (icpt + slope * x) for x, y in zip(k, pos)]
    missing = (k[-1] - k[0] + 1) - len(set(k))
    gaps = [b - a_ for a_, b in zip(k, k[1:]) if b - a_ != 1]
    print(f"clics détectés : {n}   manquants : {missing}   sauts de grille : {len(gaps)}")
    print(f"période mesurée : {slope / rate * 1000:.4f} ms (théorique {period / rate * 1000:.4f})")
    print(f"dérive vs tempo théorique : {(slope / period - 1) * 1e6:+.1f} ppm")
    print(f"gigue max autour de la droite : {max(abs(r) for r in resid) / rate * 1000:.3f} ms")


if __name__ == "__main__":
    main()
