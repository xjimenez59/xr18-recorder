#!/usr/bin/env python3
"""Génère les fichiers de découpe laser (SVG) du boîtier de l'enregistreur XR18.

Contreplaqué 5 mm, assemblage à queues droites, couvercle démontable par 4 vis M2,5
dans des écrous emprisonnés (plots d'angle de 3 couches). Stdlib uniquement.

    py boitier/boitier.py                 # -> boitier.svg, couvercle.svg (couvercle seul), essai.svg
    py boitier/boitier.py --kerf 0.18 --epaisseur 3.2

Repère : toutes les cotes en mm. Les « cotes intérieures » L x W x H sont celles de la
cavité entre les parois ; le fond et le couvercle sont posés *sur* les parois.
Rouge = découpe ; bleu = gravure (repère de la petite carte sur le fond, étiquettes sur le couvercle). Voir docs/boitier.md.
"""
import argparse
import math
import os

# ---------------------------------------------------------------- paramètres
T = 5.0            # épaisseur réelle du contreplaqué (5 mm mesuré le 22/11/2026)
KERF = 0.15        # largeur de coupe du laser (à mesurer avec essai.svg)
TENON_L = 20.0       # longueur des tenons du bas des parois (dans des encoches du fond)
N_TENONS_LONG, N_TENONS_COURT = 3, 2   # tenons par paroi avant/arrière (longues) et gauche/droite (courtes)
N_DOIGTS = 5       # nombre de segments sur la hauteur (impair : les coins finissent en doigt)

L, W, H = 128.0, 74.0, 38.0   # cavité intérieure : longueur (x), largeur (y), hauteur (z)

# Raspberry Pi 2 B : USB/Ethernet à droite (mur droit), prise d'alimentation sur le mur avant
PI_L, PI_W = 85.0, 56.0
PI_JOUR_DROITE = 0.5           # jeu entre la carte et le mur droit (les prises USB/Ethernet doivent rester accessibles)
PI_JOUR_AVANT = 1.5            # jeu entre la carte et le mur avant (prise micro-USB d'alimentation : plus près = fiche accessible)
# La carte repose sur 4 piliers en contreplaqué collés au fond (hauteur = épaisseur T), percés du trou de la vis ;
# l'écrou est emprisonné dans un trou hexagonal du fond (il ne tourne pas), inséré par le dessous.
PI_TROUS = [(3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5)]   # M2,5 (58 x 49)
PI_TROU_D = 2.7                # trou de la vis M2,5 dans le pilier (le trou du Pi fait 2,75 : M2,5 y passe ajusté)
PI_VIS_ECROU_PLATS = 5.2       # écrou M2,5 : 5,0 mm sur plats + jeu
PI_PILIER = 9.0                # côté des piliers carrés
PI_PCB = 1.4

# Plots d'angle : vis du couvercle dans un écrou emprisonné (alvéole hexagonale) ;
# n couches percées d'un hexagone + 1 couche pleine percée dessous
# Tout en M2,5 (même boulon pour le couvercle, le Pi et l'écran) : écrou M2,5 = 5 mm sur plats, 2 mm d'épaisseur ; rondelle 5,5 mm x 0,5 mm
PLOT = 12.0
VIS_D = 2.7                    # trou de passage M2,5
ECROU_PLATS = 5.2              # écrou M2,5 : 5,0 mm sur plats + jeu
ECROU_H = 2.0                  # épaisseur de l'écrou M2,5 standard
VIS_L = 14.0                   # vis du couvercle : traverse le couvercle (T), l'écrou posé au fond de l'alvéole (T) et finit dans la couche du dessous

# Face avant : bouton (filetage 11,62 mm) à gauche, écran 2,42" (70,1 x 48,1) à droite
BOUTON_D = 12.0                # filetage mesuré 11,62 mm (22/11/2026) + jeu
BOUTON_X = 14.0                # centre, depuis la paroi gauche intérieure
ECRAN_L, ECRAN_H = 70.1, 48.1
ECRAN_X = 28.0                 # bord gauche du module, depuis la paroi gauche intérieure
ECRAN_FENETRE = (62.0, 39.0)   # fenêtre vue de face : le verre doit y entrer (mesuré 22/11/2026)
ECRAN_FENETRE_DECALAGE = (0.0, 0.0)   # décalage de la fenêtre par rapport au centre du module
ECRAN_TROUS = (64.0, 42.0)     # entraxe des trous du module (À MESURER)
ECRAN_TROU_D = 2.7             # vis M2,5 (trou du couvercle)

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


def plot_couches_ecrou():
    """Nombre de couches à alvéole hexagonale d'un plot : la hauteur de l'alvéole (n x T) la plus proche de l'épaisseur
    de l'écrou. Si l'alvéole est moins profonde que l'écrou, celui-ci dépasse du plot : coller alors les plots *avec le
    couvercle vissé* (voir docs/boitier.md). Ici (écrou 3,2 mm, T = 5 mm) l'écrou tient entièrement dans l'alvéole."""
    return max(1, round(ECROU_H / T))


def paroi_doigts(largeur, hauteur, doigts_pairs, n=None, tenons=()):
    """Paroi à queues droites sur ses deux bords verticaux.
    doigts_pairs=True : les segments pairs (0, 2, ...) avancent jusqu'au bord ;
    False : ce sont les segments impairs (la paroi complémentaire).
    tenons : [(a, b), ...] tenons de hauteur T sous le bord inférieur (dans des encoches du fond)."""
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
    bas = []
    for a, b in sorted(tenons):
        bas += [(a, 0), (a, -T), (b, -T), (b, 0)]
    return _clean(gauche[0:1] + bas + droite + gauche[::-1])


def positions_tenons(n, debut, fin, longueur=None):
    """n tenons de `longueur` répartis régulièrement entre debut et fin (coordonnée le long de la paroi)."""
    longueur = longueur or TENON_L
    pas = (fin - debut) / n
    return [(debut + pas * (k + 0.5) - longueur / 2, debut + pas * (k + 0.5) + longueur / 2) for k in range(n)]


def fond_contour(t_long, t_court):
    """Contour du fond : rectangle Ex x Ey avec une encoche de T x longueur par tenon des quatre parois."""
    pts = [(0, 0)]
    for a, b in t_long:                                     # paroi avant (y = 0)
        pts += [(a, 0), (a, T), (b, T), (b, 0)]
    pts.append((Ex, 0))
    for a, b in t_court:                                    # paroi droite (x = Ex)
        pts += [(Ex, a), (Ex - T, a), (Ex - T, b), (Ex, b)]
    pts.append((Ex, Ey))
    for a, b in reversed(t_long):                           # paroi arrière (y = Ey)
        pts += [(b, Ey), (b, Ey - T), (a, Ey - T), (a, Ey)]
    pts.append((0, Ey))
    for a, b in reversed(t_court):                          # paroi gauche (x = 0)
        pts += [(0, b), (T, b), (T, a), (0, a)]
    return pts


# ------------------------------------------------------------------ pièces
Ex, Ey = L + 2 * T, W + 2 * T
PI_X0 = L - PI_JOUR_DROITE - PI_L        # coin du Pi, repère intérieur
PI_Y0 = PI_JOUR_AVANT


def pieces():
    P = {}

    # Fond : trous des entretoises du Pi + fentes d'aération
    # trous hexagonaux des écrous du Pi (insérés par le dessous) ; les piliers se collent au-dessus (repères gravés)
    trous = [hexagone(T + PI_X0 + x, T + PI_Y0 + y, PI_VIS_ECROU_PLATS) for x, y in PI_TROUS]
    fentes = [slot(T + 62 + j * 6, T + cy, 2.5, 15) for j in range(6) for cy in (W / 2 - 10, W / 2 + 10)]
    # pas d'accès à la carte micro-SD : mises à jour en OTA ; ouvrir le couvercle si besoin
    grav = [rect(T + CARTE_X, T + CARTE_Y, T + CARTE_X + CARTE_L, T + CARTE_Y + CARTE_W)]   # repère de la carte
    for x, y in PI_TROUS:                                   # repères des piliers du Pi
        yb = max(T + PI_Y0 + y - PI_PILIER / 2, T)             # le pilier ne déborde pas sur le mur avant
        grav.append(rect(T + PI_X0 + x - PI_PILIER / 2, yb, T + PI_X0 + x + PI_PILIER / 2, T + PI_Y0 + y + PI_PILIER / 2))
    if CARTE_TROUS:
        trous += [("c", T + CARTE_X + x, T + CARTE_Y + y, 1.1) for x, y in CARTE_TROUS]
    t_long = positions_tenons(N_TENONS_LONG, T, Ex - T)
    t_court = positions_tenons(N_TENONS_COURT, T, Ey - T)
    P["fond"] = dict(contour=fond_contour(t_long, t_court), trous=trous + fentes, gravure=grav, n=1)

    # Couvercle : 4 vis M2,5, bouton, fenêtre et 4 vis M2,5 de l'écran
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
    P["mur_avant"] = dict(contour=paroi_doigts(Ex, H, True, tenons=t_long),
                          trous=[slot(pwr_u, T + PI_PCB + 1.3, 18.0, 11.0)], n=1)   # micro-USB d'alimentation
    vent = [slot(T + 50 + j * 8, 18.0, 2.5, 20.0) for j in range(8)]
    P["mur_arriere"] = dict(contour=paroi_doigts(Ex, H, True, tenons=t_long), trous=vent, n=1)

    # Murs gauche/droit : largeur Ey, doigts complémentaires (segments impairs)
    vent_g = [slot(T + 14 + j * 8, 18.0, 2.5, 20.0) for j in range(6)]
    P["mur_gauche"] = dict(contour=paroi_doigts(Ey, H, False, tenons=t_court), trous=vent_g, n=1)
    # Une seule grande ouverture pour Ethernet + 2 USB : le manchon moulé d'une fiche est plus large et plus haut que la
    # prise (RJ45 avec ergot ~ 16 x 15 mm, USB jusqu'à ~ 17 mm). Prises : Ethernet y 10,25 +/- 8, USB y 29 et 47 +/- 6,65,
    # hauteur max 15,6 mm au-dessus de la carte. Marges : ~ 1,3 mm côté avant (mur voisin), ~ 2 mm ailleurs.
    pcb = T + PI_PCB
    ouv = [rect(T + PI_Y0 + 10.25 - 8 - 1.3, pcb - 2.0, T + PI_Y0 + 47 + 6.65 + 2.0, pcb + 15.6 + 3.0)]
    P["mur_droit"] = dict(contour=paroi_doigts(Ey, H, False, tenons=t_court), trous=ouv, n=1)

    # Plots d'angle : 3 couches (vis / écrou / vis), 4 jeux
    n_ecrou = plot_couches_ecrou()
    P["plot_ecrou"] = dict(contour=rect(0, 0, PLOT, PLOT), trous=[hexagone(PLOT / 2, PLOT / 2, ECROU_PLATS)], n=4 * n_ecrou)
    rogne = max(0.0, PI_PILIER / 2 - (PI_Y0 + PI_TROUS[0][1]))   # les piliers avant sont rognés côté mur avant
    P["pilier_pi"] = dict(contour=rect(0, rogne, PI_PILIER, PI_PILIER),
                          trous=[("c", PI_PILIER / 2, PI_PILIER / 2, PI_TROU_D / 2)], n=4)
    P["plot_fond"] = dict(contour=rect(0, 0, PLOT, PLOT), trous=[("c", PLOT / 2, PLOT / 2, VIS_D / 2)], n=4)
    return P


def essai():
    """Planche d'essai : réglage du kerf (doigts), fenêtre/trous de l'écran, bouton, écrou."""
    P = {}
    h = 5 * 1.6 * T                                  # 5 segments de 1,6 x T
    P["essai_doigts_A"] = dict(contour=paroi_doigts(30, h, True, n=5), trous=[], n=1)
    P["essai_doigts_B"] = dict(contour=paroi_doigts(30, h, False, n=5), trous=[], n=1)
    cx, cy = 45.0, 30.0
    tr = [slot(cx, cy, *ECRAN_FENETRE)]
    for sx in (-1, 1):
        for sy in (-1, 1):
            tr.append(("c", cx + sx * ECRAN_TROUS[0] / 2, cy + sy * ECRAN_TROUS[1] / 2, ECRAN_TROU_D / 2))
    P["essai_ecran"] = dict(contour=rect(0, 0, 90, 60), trous=tr, n=1)
    P["essai_bouton"] = dict(contour=rect(0, 0, 30, 30), trous=[("c", 15, 15, BOUTON_D / 2)], n=1)
    P["essai_pilier"] = dict(contour=rect(0, 0, 3 * PI_PILIER, 2 * PI_PILIER),
                             trous=[hexagone(PI_PILIER, PI_PILIER, PI_VIS_ECROU_PLATS)], n=1)   # essai de l'écrou du Pi
    # essai des tenons : une encoche dans un bout de fond, un tenon sous un bout de paroi
    P["essai_tenon_fond"] = dict(contour=[(0, 0), (10, 0), (10, T), (10 + TENON_L, T), (10 + TENON_L, 0), (40, 0), (40, 25), (0, 25)],
                                 trous=[], n=1)
    P["essai_tenon_paroi"] = dict(contour=[(0, 0), (10, 0), (10, -T), (10 + TENON_L, -T), (10 + TENON_L, 0), (40, 0), (40, 20), (0, 20)],
                                  trous=[], n=1)
    P["essai_ecrou"] = dict(contour=rect(0, 0, PLOT, PLOT),
                            trous=[hexagone(PLOT / 2, PLOT / 2, ECROU_PLATS)], n=1)
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
    w, h = ecrire_svg({"couvercle": pieces()["couvercle"]}, os.path.join(a.sortie, "couvercle.svg"), KERF)
    print("couvercle.svg : %.0f x %.0f mm" % (w, h))
    w, h = ecrire_svg(essai(), os.path.join(a.sortie, "essai.svg"), KERF)
    print("essai.svg : %.0f x %.0f mm" % (w, h))


if __name__ == "__main__":
    main()
