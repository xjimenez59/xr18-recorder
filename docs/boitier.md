# Boîtier (contreplaqué 3 mm, découpe laser)

Généré par `boitier/boitier.py` (stdlib) : `py boitier/boitier.py [--kerf 0.15] [--epaisseur 3.0]`
→ `boitier/boitier.svg` (toutes les pièces, ~356 × 164 mm, rouge = découpe) et `boitier/essai.svg` (planche d'essai).
**Jamais découpé à ce jour** : cotes du Pi tirées du plan standard, cotes de l'écran lues sur la photo du vendeur (**à mesurer**).

## Dimensions
Extérieur 134 × 80 × 44 mm (cavité 128 × 74 × 38). Pi à droite (USB/Ethernet contre le mur droit, alimentation sur le mur avant), bouton Ø16 à gauche, écran 2,42" (70,1 × 48,1) à droite du bouton. Le bouton (corps ≈ 25 mm) est hors de l'emprise du Pi ; l'écran n'est pas au-dessus des ports USB/Ethernet (qui montent à ~25 mm). La hauteur de 38 mm laisse la place aux fils Dupont sur les broches GPIO.

## Faire d'abord la planche d'essai
1. Mesurer l'épaisseur réelle du contreplaqué au pied à coulisse → `--epaisseur`.
2. Découper `essai.svg` : les deux pièces `essai_doigts_A/B` doivent s'emboîter à frottement doux (sinon ajuster `--kerf` : trop serré → augmenter, trop lâche → diminuer ; en général 0,10 à 0,20). `essai_ecran` : poser le module (fenêtre, 4 trous M2) ; `essai_bouton` ; `essai_ecrou` (écrou M3 dans l'alvéole).
3. Reporter dans `boitier.py` : `ECRAN_FENETRE`, `ECRAN_FENETRE_DECALAGE`, `ECRAN_TROUS` (entraxe réel des trous), puis régénérer.

## Assemblage
- **Collé (colle à bois)** : les 4 parois entre elles (queues droites), puis le fond. Serrer à l'élastique, essuyer l'excès.
- **Plots d'angle (×4)** : coller ensemble `plot_haut` + `plot_milieu` + `plot_bas` (l'alvéole hexagonale au milieu, un écrou M3 dedans, non collé), puis coller chaque plot dans un angle intérieur, **face haute à fleur du bord supérieur des parois**.
- **Couvercle démontable** : 4 vis M3 × 10 mm (tête bombée) traversent le couvercle et se vissent dans les écrous des plots. Les écrous sont emprisonnés, rien ne tourne dans le bois.
- **Pi** : fond percé pour 4 vis M2,5 × 10 depuis le dessous, entretoises M2,5 de 6 mm, puis 4 vis M2,5 × 6 sur la carte.
- **Écran** : 4 vis M2 × 8 depuis la face avant (tête visible), entretoises M2 (hauteur = épaisseur du verre, à mesurer) entre le couvercle et la carte, écrous derrière. Fils Dupont assez longs (≈ 15 cm) pour poser le couvercle à côté de la boîte.
- **Bouton** : trou de Ø16,2 mm, bague de serrage derrière (le filetage doit dépasser d'au moins 4 mm du contreplaqué de 3 mm).

## Ouvertures
Mur droit : Ethernet (17 mm), USB × 2 (15 mm) ; mur avant : micro-USB d'alimentation (14 × 9). Fentes d'aération : mur gauche, mur arrière, fond (sans toucher aux queues droites). **Carte micro-SD** : pas d'accès extérieur (elle est sous le Pi, loin des parois ; les mises à jour se font en OTA). Pour la changer, ouvrir le couvercle.
Prises USB à gros surmoulage : une fiche de plus de 15 mm de large ne passera pas, élargir l'ouverture dans `pieces()` au besoin.

## Quincaillerie
4 vis M3×10 + 4 écrous M3 ; 4 vis M2,5×10 + 4 entretoises M2,5×6 + 4 vis M2,5×6 ; 4 vis M2×8 + 4 écrous M2 + 4 entretoises M2 ; 1 bouton Ø16 à anneau rouge ; 4 pieds adhésifs (facultatifs).

## Aperçu
`py boitier/rendu.py` (nécessite matplotlib, uniquement pour ce rendu) produit `apercu-sans-accessoires.png` et `apercu-avec-accessoires.png` (vues avant-droite et arrière-gauche).
