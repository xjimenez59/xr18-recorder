#!/usr/bin/env python3
"""Aperçu 3D (projection orthographique) du boîtier, avec et sans accessoires.

    py boitier/rendu.py     # -> apercu-sans-accessoires.png, apercu-avec-accessoires.png

Nécessite matplotlib (uniquement pour ce rendu). Les ouvertures viennent de boitier.pieces().
Simplifications : les queues droites des angles ne sont pas dessinées.
"""
import math
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, PathPatch
from matplotlib.textpath import TextPath
from matplotlib.transforms import Affine2D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boitier as B  # noqa: E402

BOIS, BOIS_CL, BOIS_FONCE = "#d8b07a", "#e6c698", "#b98d55"
TRANCHE = "#8a6234"
EL = math.radians(30)


class Vue:
    def __init__(self, phi_deg):
        self.phi = math.radians(phi_deg)
        self.c = (math.sin(self.phi), -math.cos(self.phi))      # direction horizontale vers la caméra

    def p(self, x, y, z):
        u = x * math.cos(self.phi) + y * math.sin(self.phi)
        v = z * math.cos(EL) - (x * self.c[0] + y * self.c[1]) * math.sin(EL)
        return (u, v)

    def visible(self, normale):
        return normale[0] * self.c[0] + normale[1] * self.c[1] > 1e-6


def hull(pts):
    pts = sorted(set(pts))
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, up = [], []
    for q in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], q) <= 0:
            lo.pop()
        lo.append(q)
    for q in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], q) <= 0:
            up.pop()
        up.append(q)
    return lo[:-1] + up[:-1]


def cercle(vue, cx, cy, z, r, n=40):
    return [vue.p(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n), z) for i in range(n)]


def cylindre(ax, vue, cx, cy, z0, z1, r, couleur, bord="#222", zorder=5):
    """Cylindre vertical plein : enveloppe convexe des deux cercles, puis le dessus."""
    h = hull(cercle(vue, cx, cy, z0, r) + cercle(vue, cx, cy, z1, r))
    ax.add_patch(Polygon(h, closed=True, fc=couleur, ec=bord, lw=0.6, zorder=zorder))
    ax.add_patch(Polygon(cercle(vue, cx, cy, z1, r), closed=True, fc=couleur, ec=bord, lw=0.6, zorder=zorder + 0.1))


def dessiner(ax, vue, accessoires):
    H, T, Ex, Ey = B.H, B.T, B.Ex, B.Ey
    zt = H + 2 * T
    P = B.pieces()
    pied = 5.0

    def trous_face(nom, mapping, zorder):
        for t in P[nom]["trous"]:
            if isinstance(t, tuple):
                continue
            ax.add_patch(Polygon([vue.p(*mapping(u, v)) for u, v in t], closed=True, fc="#1b1410", ec="#000", lw=0.5,
                                 zorder=zorder))

    # pieds (accessoires) : sous le fond
    if accessoires:
        for cx, cy in ((10, 10), (Ex - 10, 10), (10, Ey - 10), (Ex - 10, Ey - 10)):
            cylindre(ax, vue, cx, cy, -pied, 0, 5.0, "#2b2b2b", zorder=1)

    faces = [
        ("mur_avant", (0, -1), lambda u, v: (u, 0, T + v), (0, 0, 0), (Ex, 0, 0)),
        ("mur_arriere", (0, 1), lambda u, v: (u, Ey, T + v), (0, 0, 0), (Ex, 0, 0)),
        ("mur_gauche", (-1, 0), lambda u, v: (0, u, T + v), (0, 0, 0), (0, Ey, 0)),
        ("mur_droit", (1, 0), lambda u, v: (Ex, u, T + v), (0, 0, 0), (0, Ey, 0)),
    ]
    for nom, normale, f, _, _ in faces:
        if not vue.visible(normale):
            continue
        pts = [f(0, 0)[:2] + (0,), f(0, 0)[:2] + (zt,)]
        # face complète z = 0..zt (bois), bandes de plaques en plus clair/foncé
        x0, y0 = f(0, 0)[0], f(0, 0)[1]
        xe, ye = (Ex, y0) if normale[1] else (x0, Ey)
        quad = [vue.p(x0, y0, 0), vue.p(xe, ye, 0), vue.p(xe, ye, zt), vue.p(x0, y0, zt)]
        ax.add_patch(Polygon(quad, closed=True, fc=BOIS if abs(normale[1]) else BOIS_FONCE, ec=TRANCHE, lw=0.8, zorder=2))
        for z in (T, T + H):                                            # joints fond / parois / couvercle
            ax.plot(*zip(vue.p(x0, y0, z), vue.p(xe, ye, z)), color=TRANCHE, lw=0.8, zorder=3)
        trous_face(nom, f, 3)

    # dessus : couvercle
    top = [vue.p(0, 0, zt), vue.p(Ex, 0, zt), vue.p(Ex, Ey, zt), vue.p(0, Ey, zt)]
    ax.add_patch(Polygon(top, closed=True, fc=BOIS_CL, ec=TRANCHE, lw=0.8, zorder=2))
    for t in P["couvercle"]["trous"]:
        if isinstance(t, tuple):
            _, cx, cy, r = t
            ax.add_patch(Polygon(cercle(vue, cx, cy, zt, r), closed=True, fc="#1b1410", ec="#000", lw=0.5, zorder=3))
        else:
            ax.add_patch(Polygon([vue.p(x, y, zt) for x, y in t], closed=True, fc="#1b1410", ec="#000", lw=0.5, zorder=3))

    if not accessoires:
        return

    # écran : verre noir dans la fenêtre, texte du chronomètre, 4 vis M2
    cy_mid = Ey / 2
    ex_c = B.T + B.ECRAN_X + B.ECRAN_L / 2
    fw, fh = B.ECRAN_FENETRE
    zs = zt - 1.0
    ax.add_patch(Polygon([vue.p(ex_c - fw / 2, cy_mid - fh / 2, zs), vue.p(ex_c + fw / 2, cy_mid - fh / 2, zs),
                          vue.p(ex_c + fw / 2, cy_mid + fh / 2, zs), vue.p(ex_c - fw / 2, cy_mid + fh / 2, zs)],
                         closed=True, fc="#05070a", ec="#000", lw=0.6, zorder=4))
    # texte : repère local de la face supérieure -> écran (transformation affine)
    ox, oy = vue.p(ex_c, cy_mid, zt - 0.9)
    ux = vue.p(ex_c + 1, cy_mid, zt - 0.9)
    uy = vue.p(ex_c, cy_mid + 1, zt - 0.9)
    aff = Affine2D.from_values(ux[0] - ox, ux[1] - oy, uy[0] - ox, uy[1] - oy, ox, oy)
    for txt, taille, dx, dy, col in (("00:42:17", 9.5, 0, 1.2, "#e8f4ff"),
                                     ("REC", 4.5, -17.5, -9.0, "#ff5050"),
                                     ("18 voies  48 kHz", 3.0, 7.0, -9.0, "#9bb7c9")):
        tp = TextPath((0, 0), txt, size=taille, prop={"family": "DejaVu Sans Mono", "weight": "bold"})
        bb = tp.get_extents()
        if dx == 0:
            tr = Affine2D().translate(-(bb.x0 + bb.x1) / 2 + dx, dy - taille * 0.35)
        else:
            tr = Affine2D().translate(-(bb.x0 + bb.x1) / 2 + dx, dy)
        ax.add_patch(PathPatch(tp, fc=col, ec="none", transform=tr + aff + ax.transData, zorder=4.5))
    for t in P["couvercle"]["trous"]:
        if isinstance(t, tuple):
            _, cx, cy, r = t
            if abs(r - B.ECRAN_TROU_D / 2) < 1e-6 + B.KERF:
                cylindre(ax, vue, cx, cy, zt, zt + 1.2, 1.8, "#303030", zorder=4.2)
            elif abs(r - B.VIS_D / 2) < 0.2:
                cylindre(ax, vue, cx, cy, zt, zt + 2.0, 3.0, "#4a4f55", bord="#111", zorder=4.2)

    # bouton : bague chromée + capuchon, anneau lumineux rouge
    bx, by = B.T + B.BOUTON_X, cy_mid
    cylindre(ax, vue, bx, by, zt, zt + 2.2, 10.0, "#9aa0a6", zorder=4.3)
    cylindre(ax, vue, bx, by, zt + 2.2, zt + 4.5, 8.4, "#c82020", bord="#400", zorder=4.4)
    ax.add_patch(Polygon(cercle(vue, bx, by, zt + 4.5, 6.2), closed=True, fc="#e25555", ec="#600", lw=0.6, zorder=4.6))


def main():
    sortie = os.path.dirname(os.path.abspath(__file__))
    for accessoires, nom in ((False, "apercu-sans-accessoires.png"), (True, "apercu-avec-accessoires.png")):
        fig, axes = plt.subplots(1, 2, figsize=(15, 6.2))
        for ax, phi, titre in ((axes[0], 35, "Avant et côté droit (USB, Ethernet)"),
                               (axes[1], 215, "Arrière et côté gauche (aération)")):
            dessiner(ax, Vue(phi), accessoires)
            ax.set_aspect("equal")
            ax.autoscale_view()
            ax.axis("off")
            ax.set_title(titre, fontsize=11)
        fig.suptitle("Boîtier XR18 - %s (134 x 80 x 44 mm, CP 3 mm)"
                     % ("avec écran, bouton, vis et pieds (facultatifs)" if accessoires else "sans accessoires"), fontsize=13)
        fig.tight_layout()
        fig.savefig(os.path.join(sortie, nom), dpi=110, facecolor="white")
        plt.close(fig)
        print(nom)


if __name__ == "__main__":
    main()
