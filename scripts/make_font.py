#!/usr/bin/env python3
"""Génère xr18rec/fontdata.py à partir des polices bitmap Spleen (BDF), pour l'écran OLED.

Spleen : https://github.com/fcambus/spleen (licence BSD 2 clauses, © Frederic Cambus). Usage :
    python3 scripts/make_font.py DOSSIER_DES_BDF > xr18rec/fontdata.py
où DOSSIER_DES_BDF contient spleen-6x12.bdf, spleen-12x24.bdf, spleen-16x32.bdf et LICENSE.
La petite police garde l'ASCII imprimable et les accents du français ; les grandes (titres) seulement majuscules (accentuées), chiffres et quelques signes.
"""

import os
import sys

ASCII = "".join(chr(c) for c in range(32, 127))
ACCENTS = "ÀÂÄÇÈÉÊËÎÏÔÖÙÛÜàâäçèéêëîïôöùûüÿ°«»"  # français
CAPS = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.:!-ÀÂÄÇÈÉÊËÎÏÔÖÙÛÜŒ"
FONTS = [("6x12", ASCII + ACCENTS), ("12x24", CAPS), ("16x32", CAPS)]


def parse(path: str):
    """Renvoie (largeur, hauteur, {code: colonnes}) ; une colonne = entier dont le bit r est la ligne r depuis le haut de la cellule."""
    width = height = ascent = None
    glyphs, cur = {}, None
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            key = parts[0]
            if key == "FONTBOUNDINGBOX":
                width, height = int(parts[1]), int(parts[2])
            elif key == "FONT_ASCENT":
                ascent = int(parts[1])
            elif key == "STARTCHAR":
                cur = {"rows": None}
            elif key == "ENCODING" and cur is not None:
                cur["code"] = int(parts[1])
            elif key == "BBX" and cur is not None:
                cur["bbx"] = tuple(int(v) for v in parts[1:5])
            elif key == "BITMAP" and cur is not None:
                cur["rows"] = []
            elif key == "ENDCHAR" and cur is not None:
                glyphs[cur["code"]] = cur
                cur = None
            elif cur is not None and cur.get("rows") is not None:
                cur["rows"].append(parts[0])
    out = {}
    for code, g in glyphs.items():
        w, h, xoff, yoff = g["bbx"]
        top = ascent - (yoff + h)  # ligne de la cellule où commence le glyphe
        cols = [0] * width
        for r, hexrow in enumerate(g["rows"]):
            bits = int(hexrow, 16) << (4 * len(hexrow) - 4 * len(hexrow))  # valeur brute de la ligne
            nbits = 4 * len(hexrow)
            for x in range(w):
                if bits >> (nbits - 1 - x) & 1 and 0 <= xoff + x < width and 0 <= top + r < height:
                    cols[xoff + x] |= 1 << (top + r)
        out[code] = tuple(cols)
    return width, height, out


def main(folder: str) -> None:
    with open(os.path.join(folder, "LICENSE"), encoding="utf-8") as f:
        licence = f.read().strip()
    print('"""Polices bitmap Spleen pour l\'écran OLED (généré par scripts/make_font.py : ne pas modifier à la main).\n')
    print("Spleen, https://github.com/fcambus/spleen\n")
    print(licence.replace('"', "'"))
    print('"""\n')
    print("# Chaque police : (largeur, hauteur, {caractère: colonnes}) ; colonne = entier, le bit r est la ligne r depuis le haut.")
    print("FONTS = {")
    for name, chars in FONTS:
        width, height, glyphs = parse(os.path.join(folder, f"spleen-{name}.bdf"))
        print(f'    "{name}": ({width}, {height}, {{')
        for ch in chars:
            if ord(ch) not in glyphs:
                print(f"police {name} : caractère absent {ch!r}", file=sys.stderr)
                continue
            print(f"        {ch!r}: {glyphs[ord(ch)]!r},")
        print("    }),")
    print("}")


if __name__ == "__main__":
    main(sys.argv[1])
