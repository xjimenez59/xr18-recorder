#!/usr/bin/env python3
"""Génère les fichiers de découpe laser (SVG) du boîtier de l'enregistreur XR18.

Contreplaqué 3 mm, assemblage à queues droites, couvercle démontable par 4 vis M3
dans des écrous emprisonnés (plots d'angle de 3 couches). Stdlib uniquement.

    py boitier/boitier.py                 # -> boitier/boitier.svg et boitier/essai.svg
    py boitier/boitier.py --kerf 0.18 --epaisseur 3.2

Repère : toutes les cotes en mm. Les « cotes intérieures » L x W x H sont celles de la
cavité entre les parois ; le fond et le couvercle sont posés *sur* les parois.
Rouge = découpe ; bleu = gravure (repère de la petite carte sur le fond, étiquettes sur le couvercle). Voir docs/boitier.md.
"""
import argparse
import math
import os

# ---------------------------------------------------------------- paramètres
T = 3.0            # épaisseur réelle du contreplaqué (à mesurer au pied à coulisse)
KERF = 0.15        # largeur de coupe du laser (à mesurer avec essai.svg)
N_DOIGTS = 5       # nombre de segments sur la hauteur (impair : les coins finissent en doigt)

L, W, H = 128.0, 74.0, 38.0   # cavité intérieure : longueur (x), largeur (y), hauteur (z)

# Raspberry Pi 2 B : USB/Ethernet à droite (mur droit), prise d'alimentation sur le mur avant
PI_L, PI_W = 85.0, 56.0
PI_JOUR_DROITE = 1.0           # jeu entre la carte et le mur droit
PI_ENTRETOISE = 6.0            # hauteur des entretoises sous la carte
PI_TROUS = [(3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5)]   # M2,5 (58 x 49)
PI_TROU_D = 2.7
PI_PCB = 1.4

# Plots d'angle (3 couches) : vis M3 du couvercle
PLOT = 9.0
VIS_D = 3.4
ECROU_M3_PLATS = 5.7           # écrou M3 : 5,5 mm sur plats, un peu de jeu

# Face avant : bouton Ø16 à gauche, écran 2,42" (70,1 x 48,1) à droite
BOUTON_D = 16.2
BOUTON_X = 14.0                # centre, depuis la paroi gauche intérieure
ECRAN_L, ECRAN_H = 70.1, 48.1
ECRAN_X = 28.0                 # bord gauche du module, depuis la paroi gauche intérieure
ECRAN_FENETRE = (60.0, 32.0)   # fenêtre vue de face (à confirmer avec essai.svg)
ECRAN_FENETRE_DECALAGE = (0.0, 0.0)   # décalage de la fenêtre par rapport au centre du module
ECRAN_TROUS = (64.0, 42.0)     # entraxe des trous du module (À MESURER)
ECRAN_TROU_D = 2.3             # vis M2

# Petite carte (transistor + résistances + broches), posée au fond à l'avant gauche : repère gravé
CARTE_L, CARTE_W = 30.0, 20.0
CARTE_X, CARTE_Y = 1.0, 5.0    # coin avant gauche, repère intérieur (hors de l'emprise du bouton : y < 26)
CARTE_TROUS = None             # ex. ((3, 3), (27, 17)) : trous Ø2,2 si l'on veut visser la carte (entraxe à mesurer)

FEUILLE_L = 400.0              # largeur utile (xTool D1 Pro : 430 x 390)
ESPACE = 4.0

ROUGE = "#ff0000"
BLEU = "#0000ff"      # gravure (repères, sans découpe)


# ------------------------------------------------------------------ géométrie
def _clean(pts):
    """Supprime doublons et points alignés d'un polygone rectiligne."""
    out = []
    for p in pts:
        if not out or (abs(p[0] - out[-1][0]) > 1e-9 or abs(p[1] - out[-1][1]) > 1e-9):
            out.append(p)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) < 1e-9 and abs(out[0][1] - out[-1][1]) < 1e-9:
        out.pop()
    changed = True
    while changed and len(out) > 3:
        changed = False
        for i in range(len(out)):
            a, b, c = out[i - 1], out[i], out[(i + 1) % len(out)]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if abs(cross) < 1e-9:
                del out[i]
                changed = True
                break
    return out


def _area(pts):
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                     for i in range(len(pts)))


def offset(pts, d):
    """Décale un polygone (d > 0 : vers l'extérieur), coins en onglet."""
    pts = _clean(pts)
    if _area(pts) < 0:
        pts = pts[::-1]
    n = len(pts)
    out = []
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        d1 = (b[0] - a[0], b[1] - a[1])
        d2 = (c[0] - b[0], c[1] - b[1])
        l1, l2 = math.hypot(*d1), math.hypot(*d2)
        n1 = (d1[1] / l1, -d1[0] / l1)      # normale extérieure d'un polygone anti-horaire
        n2 = (d2[1] / l2, -d2[0] / l2)
        k = d / (1 + n1[0] * n2[0] + n1[1] * n2[1])
        out.append((b[0] + (n1[0] + n2[0]) * k, b[1] + (n1[1] + n2[1]) * k))
    return out


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def slot(cx, cy, w, h):
    return rect(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def hexagone(cx, cy, plats):
    """Hexagone régulier (sommets en haut/bas) de largeur `plats` entre deux côtés."""
    r = plats / math.sqrt(3)
    return [(cx + r * math.cos(math.radians(90 + 60 * i)), cy + r * math.sin(math.radians(90 + 60 * i)))
            for i in range(6)]


def texte(txt, cx, cy, hauteur):
    """Contours d'un texte (DejaVu Sans Bold, via matplotlib) centré en (cx, cy), `hauteur` = hauteur des capitales (mm).
    Liste de polygones (les trous des lettres sont des polygones de sens inverse : remplissage evenodd).
    Sans matplotlib, retourne [] (les étiquettes sont alors omises)."""
    try:
        from matplotlib.textpath import TextPath
        from matplotlib.font_manager import FontProperties
    except ImportError:
        return []
    tp = TextPath((0, 0), txt, size=10, prop=FontProperties(family="DejaVu Sans", weight="bold"))
    ref = TextPath((0, 0), "H", size=10, prop=FontProperties(family="DejaVu Sans", weight="bold")).get_extents()
    bb = tp.get_extents()
    k = hauteur / ref.height
    mx, my = (bb.x0 + bb.x1) / 2, (ref.y0 + ref.y1) / 2
    return [[(cx + (x - mx) * k, cy + (y - my) * k) for x, y in poly] for poly in tp.to_polygons(closed_only=True)]


def paroi_doigts(largeur, hauteur, doigts_pairs, n=None):
    """Paroi à queues droites sur ses deux bords verticaux.
    doigts_pairs=True : les segments pairs (0, 2, ...) avancent jusqu'au bord ;
    False : ce sont les segments impairs (la paroi complémentaire)."""
    n = n or N_DOIGTS
    s = hauteur / n

    def avance(i):
        return (i % 2 == 0) == doigts_pairs

    droite, gauche = [], []
    for i in range(n):
        x = largeur if avance(i) else largeur - T
        droite += [(x, i * s), (x, (i + 1) * s)]
        xg = 0 if avance(i) else T
        gauche += [(xg, i * s), (xg, (i + 1) * s)]
    return _clean(gauche[0:1] + droite + gauche[::-1])


# ------------------------------------------------------------------ pièces
Ex, Ey = L + 2 * T, W + 2 * T
PI_X0 = L - PI_JOUR_DROITE - PI_L        # coin du Pi, repère intérieur
PI_Y0 = (W - PI_W) / 2


def pieces():
    P = {}

    # Fond : trous des entretoises du Pi + fentes d'aération
    trous = [("c", T + PI_X0 + x, T + PI_Y0 + y, PI_TROU_D / 2) for x, y in PI_TROUS]
    fentes = [slot(T + 62 + j * 6, T + cy, 2.5, 15) for j in range(6) for cy in (W / 2 - 10, W / 2 + 10)]
    # pas d'accès à la carte micro-SD : mises à jour en OTA ; ouvrir le couvercle si besoin
    grav = [rect(T + CARTE_X, T + CARTE_Y, T + CARTE_X + CARTE_L, T + CARTE_Y + CARTE_W)]   # repère de la carte
    if CARTE_TROUS:
        trous += [("c", T + CARTE_X + x, T + CARTE_Y + y, 1.1) for x, y in CARTE_TROUS]
    P["fond"] = dict(contour=rect(0, 0, Ex, Ey), trous=trous + fentes, gravure=grav, n=1)

    # Couvercle : 4 vis M3, bouton, fenêtre et 4 vis M2 de l'écran
    cl = []
    for cx in (T + PLOT / 2, Ex - T - PLOT / 2):
        for cy in (T + PLOT / 2, Ey - T - PLOT / 2):
            cl.append(("c", cx, cy, VIS_D / 2))
    cy_mid = Ey / 2
    cl.append(("c", T + BOUTON_X, cy_mid, BOUTON_D / 2))
    ex_c = T + ECRAN_X + ECRAN_L / 2           # centre du module
    fx, fy = ECRAN_FENETRE_DECALAGE
    cl.append(slot(ex_c + fx, cy_mid + fy, *ECRAN_FENETRE))
    for sx in (-1, 1):
        for sy in (-1, 1):
            cl.append(("c", ex_c + sx * ECRAN_TROUS[0] / 2, cy_mid + sy * ECRAN_TROUS[1] / 2, ECRAN_TROU_D / 2))
    # Étiquettes gravées : « REC » sous le bouton, nom du projet sous l'écran (lisibles depuis l'avant du boîtier)
    etiq = texte("REC", T + BOUTON_X, T + W / 2 - 16.0, 5.0) + texte("XR18 REC", ex_c, T + 6.5, 5.0)
    P["couvercle"] = dict(contour=rect(0, 0, Ex, Ey), trous=cl, texte=etiq, n=1)

    # Murs avant/arrière : largeur Ex, doigts sur les segments pairs
    pwr_u = T + PI_X0 + 10.6
    P["mur_avant"] = dict(contour=paroi_doigts(Ex, H, True),
                          trous=[slot(pwr_u, 9.0, 14.0, 9.0)], n=1)   # micro-USB d'alimentation
    vent = [slot(T + 50 + j * 8, 18.0, 2.5, 20.0) for j in range(8)]
    P["mur_arriere"] = dict(contour=paroi_doigts(Ex, H, True), trous=vent, n=1)

    # Murs gauche/droit : largeur Ey, doigts complémentaires (segments impairs)
    vent_g = [slot(T + 14 + j * 8, 18.0, 2.5, 20.0) for j in range(6)]
    P["mur_gauche"] = dict(contour=paroi_doigts(Ey, H, False), trous=vent_g, n=1)
    zb = PI_ENTRETOISE + 0.5                  # bas des ouvertures
    ouv = [
        rect(T + PI_Y0 + 10.25 - 8.5, zb, T + PI_Y0 + 10.25 + 8.5, zb + 15.0),   # Ethernet
        rect(T + PI_Y0 + 29 - 7.5, zb, T + PI_Y0 + 29 + 7.5, zb + 17.0),         # USB (XR18)
        rect(T + PI_Y0 + 47 - 7.5, zb, T + PI_Y0 + 47 + 7.5, zb + 17.0),         # USB (disque)
    ]
    P["mur_droit"] = dict(contour=paroi_doigts(Ey, H, False), trous=ouv, n=1)

    # Plots d'angle : 3 couches (vis / écrou / vis), 4 jeux
    P["plot_haut"] = dict(contour=rect(0, 0, PLOT, PLOT), trous=[("c", PLOT / 2, PLOT / 2, VIS_D / 2)], n=4)
    P["plot_milieu"] = dict(contour=rect(0, 0, PLOT, PLOT), trous=[hexagone(PLOT / 2, PLOT / 2, ECROU_M3_PLATS)], n=4)
    P["plot_bas"] = dict(contour=rect(0, 0, PLOT, PLOT), trous=[("c", PLOT / 2, PLOT / 2, VIS_D / 2)], n=4)
    return P


def essai():
    """Planche d'essai : réglage du kerf (doigts), fenêtre/trous de l'écran, bouton, écrou."""
    P = {}
    P["essai_doigts_A"] = dict(contour=paroi_doigts(30, 3 * 6 + 3, True, n=5), trous=[], n=1)
    P["essai_doigts_B"] = dict(contour=paroi_doigts(30, 3 * 6 + 3, False, n=5), trous=[], n=1)
    cx, cy = 45.0, 30.0
    tr = [slot(cx, cy, *ECRAN_FENETRE)]
    for sx in (-1, 1):
        for sy in (-1, 1):
            tr.append(("c", cx + sx * ECRAN_TROUS[0] / 2, cy + sy * ECRAN_TROUS[1] / 2, ECRAN_TROU_D / 2))
    P["essai_ecran"] = dict(contour=rect(0, 0, 90, 60), trous=tr, n=1)
    P["essai_bouton"] = dict(contour=rect(0, 0, 30, 30), trous=[("c", 15, 15, BOUTON_D / 2)], n=1)
    P["essai_ecrou"] = dict(contour=rect(0, 0, PLOT, PLOT),
                            trous=[hexagone(PLOT / 2, PLOT / 2, ECROU_M3_PLATS)], n=1)
    return P


# ------------------------------------------------------------------ SVG
def _bbox(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _compense(piece, kerf):
    contour = offset(piece["contour"], kerf / 2)
    trous = []
    for t in piece["trous"]:
        if isinstance(t, tuple) and t[0] == "c":
            trous.append(("c", t[1], t[2], max(t[3] - kerf / 2, 0.05)))
        else:
            trous.append(offset(t, -kerf / 2))
    return contour, trous


def ecrire_svg(P, chemin, kerf):
    """Range les pièces en rayonnages, applique la compensation du kerf, écrit le SVG."""
    items = []
    for nom, p in P.items():
        contour, trous = _compense(p, kerf)
        trous = trous + [("g", q) for q in p.get("gravure", [])]
        if p.get("texte"):
            trous.append(("t", p["texte"]))
        x0, y0, x1, y1 = _bbox(contour)
        for k in range(p["n"]):
            items.append((nom if p["n"] == 1 else "%s_%d" % (nom, k + 1), contour, trous, x0, y0, x1 - x0, y1 - y0))
    items.sort(key=lambda it: -it[6])
    x = y = rangee_h = 0.0
    place = []
    for nom, contour, trous, x0, y0, w, h in items:
        if x + w > FEUILLE_L and x > 0:
            x = 0.0
            y += rangee_h + ESPACE
            rangee_h = 0.0
        place.append((nom, contour, trous, x - x0, y - y0, h))
        x += w + ESPACE
        rangee_h = max(rangee_h, h)
    total_w = min(FEUILLE_L, max(x, FEUILLE_L if len(place) > 1 else x))
    total_w = max(max(_bbox(c)[2] + dx for _, c, _, dx, _, _ in place), 1)
    total_h = y + rangee_h
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%.2fmm" height="%.2fmm" viewBox="0 0 %.2f %.2f">'
           % (total_w, total_h, total_w, total_h)]
    # le repère des pièces a y vers le haut : on retourne chaque pièce dans sa boîte
    for nom, contour, trous, dx, dy, h in place:
        out.append('<g id="%s" fill="none" stroke="%s" stroke-width="0.1">' % (nom, ROUGE))
        bx0, by0, bx1, by1 = _bbox(contour)

        def pt(px, py):
            return "%.3f %.3f" % (px + dx, dy + (by1 + by0) - py)   # miroir vertical dans la boîte

        out.append('<path d="M %s Z"/>' % " L ".join(pt(*q) for q in contour))
        for t in trous:
            if isinstance(t, tuple) and t[0] == "t":      # texte gravé : surface pleine (evenodd pour les trous des lettres)
                d = " ".join("M %s Z" % " L ".join(pt(*q) for q in poly) for poly in t[1])
                out.append('<path fill="%s" stroke="none" fill-rule="evenodd" d="%s"/>' % (BLEU, d))
            elif isinstance(t, tuple) and t[0] == "g":      # gravure : couleur distincte, pas de compensation
                out.append('<path stroke="%s" d="M %s Z"/>' % (BLEU, " L ".join(pt(*q) for q in t[1])))
            elif isinstance(t, tuple) and t[0] == "c":
                out.append('<circle cx="%.3f" cy="%.3f" r="%.3f"/>' % (t[1] + dx, dy + (by1 + by0) - t[2], t[3]))
            else:
                out.append('<path d="M %s Z"/>' % " L ".join(pt(*q) for q in t))
        out.append("</g>")
    out.append("</svg>")
    with open(chemin, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return total_w, total_h


def main():
    global T, KERF, Ex, Ey
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kerf", type=float, default=KERF)
    ap.add_argument("--epaisseur", type=float, default=T)
    ap.add_argument("--sortie", default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args()
    T, KERF = a.epaisseur, a.kerf
    Ex, Ey = L + 2 * T, W + 2 * T
    w, h = ecrire_svg(pieces(), os.path.join(a.sortie, "boitier.svg"), KERF)
    print("boitier.svg : %.0f x %.0f mm (extérieur du boîtier : %.0f x %.0f x %.0f mm)"
          % (w, h, Ex, Ey, H + 2 * T))
    w, h = ecrire_svg(essai(), os.path.join(a.sortie, "essai.svg"), KERF)
    print("essai.svg : %.0f x %.0f mm" % (w, h))


if __name__ == "__main__":
    main()
