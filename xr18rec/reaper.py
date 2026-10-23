"""Génération d'un projet Reaper (.rpp) à partir des segments WAV d'une session.

Un seul jeu de fichiers WAV multicanaux est utilisé (pas de copie) : chaque piste Reaper
référence les mêmes fichiers et choisit sa voie via le « mode de canal » de la prise.
Voies USB de l'XR18 supposées : 1..16 = entrées 1..16, 17/18 = mix général L/R (routage USB par défaut).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid

from . import wavtools
from .xr18osc import NUM_INPUTS, Layout, color_rgb

_SEGMENT = re.compile(r"take(?:-(\d+))?\.wav", re.IGNORECASE)


LAYOUT_FILE = "channels.json"


def load_layout(directory: str) -> Layout | None:
    """Noms/couleurs mémorisés au démarrage de la session, ou None."""
    try:
        with open(os.path.join(directory, LAYOUT_FILE), encoding="utf-8") as f:
            return Layout.from_dict(json.load(f))
    except (OSError, ValueError):
        return None


def mono_chanmode(channel: int) -> int:
    """Mode de canal Reaper « mono, voie N » (N à partir de 1). 3/4 = gauche/droite, 5 = voie 3, etc. (à vérifier)."""
    return channel + 2


def stereo_chanmode(first_channel: int) -> int:
    """Mode de canal Reaper « stéréo, voies N/N+1 » : 67 = 1/2, 68 = 2/3, etc. (à vérifier)."""
    return 66 + first_channel


def find_segments(directory: str) -> list[str]:
    """Segments `take.wav`, `take-01.wav`… dans l'ordre chronologique."""
    found = []
    for n in os.listdir(directory):
        m = _SEGMENT.fullmatch(n)
        if m:
            found.append((int(m.group(1) or 0), n))
    return [n for _, n in sorted(found)]


def _guid() -> str:
    return "{" + str(uuid.uuid4()).upper() + "}"


def _q(text: str) -> str:
    return '"' + text.replace('"', "'") + '"'


def _peakcol(rgb: tuple[int, int, int] | None) -> str:
    if rgb is None:
        return ""
    r, g, b = rgb
    return f"    PEAKCOL {0x1000000 | r | (g << 8) | (b << 16)}\n"  # COLORREF (BGR) + drapeau « couleur définie »


def build_project(directory: str, layout: Layout | None = None) -> str:
    segments = find_segments(directory)
    if not segments:
        raise wavtools.WavError("aucun segment take*.wav dans " + directory)
    infos = [wavtools.read_info(os.path.join(directory, n)) for n in segments]
    ref = infos[0]
    for i in infos[1:]:
        if (i.channels, i.rate) != (ref.channels, ref.rate):
            raise wavtools.WavError("les segments n'ont pas le même format")

    positions, t = [], 0.0
    for i in infos:
        positions.append(t)
        t += i.seconds

    layout = layout or Layout()
    tracks: list[tuple[str, tuple[int, int, int] | None, int]] = []
    for n in range(1, min(NUM_INPUTS, ref.channels) + 1):
        s = layout.channels[n - 1]
        tracks.append((s.name or f"Ch {n:02d}", color_rgb(s.color), mono_chanmode(n)))
    if ref.channels >= NUM_INPUTS + 2:
        tracks.append((layout.main.name or "Main L/R", color_rgb(layout.main.color),
                       stereo_chanmode(NUM_INPUTS + 1)))

    out = [f'<REAPER_PROJECT 0.1 "7.0/xr18rec" {int(time.time())}\n',
           "  RIPPLE 0\n  AUTOXFADE 1\n",
           f"  SAMPLERATE {ref.rate} 0 0\n  TEMPO 120 4 4\n"]
    for name, rgb, mode in tracks:
        out.append(f"  <TRACK {_guid()}\n    NAME {_q(name)}\n{_peakcol(rgb)}    NCHAN 2\n")
        for seg, pos, info in zip(segments, positions, infos):
            out.append(
                "    <ITEM\n"
                f"      POSITION {pos:.6f}\n      LENGTH {info.seconds:.6f}\n      LOOP 0\n"
                f"      IGUID {_guid()}\n      NAME {_q(seg)}\n      CHANMODE {mode}\n      GUID {_guid()}\n"
                f"      <SOURCE WAVE\n        FILE {_q(seg)}\n      >\n    >\n")
        out.append("  >\n")
    out.append(">\n")
    return "".join(out)


def write_project(directory: str, layout: Layout | None = None, name: str | None = None) -> str:
    path = os.path.join(directory, (name or os.path.basename(os.path.abspath(directory))) + ".rpp")
    text = build_project(directory, layout)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path
