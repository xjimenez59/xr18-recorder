# Boîtier (contreplaqué 5 mm, découpe laser)

Généré par `boitier/boitier.py` (stdlib) : `py boitier/boitier.py [--kerf 0.15] [--epaisseur 5.0]`
→ `boitier/boitier.svg` (toutes les pièces, ~368 × 168 mm, rouge = découpe) `boitier/couvercle.svg` (le couvercle seul, pour le refaire) et `boitier/essai.svg` (planche d'essai).
**Jamais découpé à ce jour** : cotes du Pi tirées du plan standard, cotes de l'écran lues sur la photo du vendeur (**à mesurer**).

## Dimensions
Extérieur 138 × 84 × 48 mm (cavité 128 × 74 × 38, contreplaqué **5 mm** mesuré). Pi à droite et **près du mur avant** (jeu 1,5 mm : USB/Ethernet contre le mur droit, prise micro-USB d'alimentation contre le mur avant, fiche accessible malgré 5 mm de paroi), bouton (filetage 11,62 mm) à gauche, écran 2,42" (70,1 × 48,1) à droite du bouton. Le bouton (corps ≈ 25 mm) est hors de l'emprise du Pi ; l'écran n'est pas au-dessus des ports USB/Ethernet (qui montent à ~25 mm). La hauteur de 38 mm laisse la place aux fils Dupont sur les broches GPIO.

## Faire d'abord la planche d'essai
1. Épaisseur du contreplaqué : 5 mm (mesuré le 22/11/2026) → valeur par défaut de `T`.
2. Découper `essai.svg` : les deux pièces `essai_doigts_A/B` doivent s'emboîter à frottement doux (sinon ajuster `--kerf` : trop serré → augmenter, trop lâche → diminuer ; en général 0,10 à 0,20). `essai_ecran` : poser le module (fenêtre, 4 trous M2,5) ; `essai_bouton` ; `essai_ecrou` (écrou M2,5 dans l'alvéole) ; `essai_tenon_fond` / `essai_tenon_paroi` (tenon dans son encoche).
3. Reporter dans `boitier.py` : `ECRAN_FENETRE`, `ECRAN_FENETRE_DECALAGE`, `ECRAN_TROUS` (entraxe réel des trous), puis régénérer.

## Assemblage
- **Assemblage à blanc d'abord** : les 4 parois s'emboîtent par leurs queues droites, et le **fond** par des **tenons** (3 sous les parois avant/arrière, 2 sous les parois gauche/droite, 20 mm de long, hauteur = épaisseur du fond) dans des encoches du fond. Tout se tient sans colle, ce qui permet de vérifier l'ajustement avant de coller.
- **Collé (colle à bois)** : les 4 parois entre elles, puis le fond sur les tenons. Serrer à l'élastique, essuyer l'excès. Les tenons affleurent le bord du fond : ils restent visibles (aspect « bout de bois »), ponçables après séchage.
- **Plots d'angle (×4)** : 2 couches de 12 × 12 mm, `plot_ecrou` (alvéole hexagonale traversante de 5,2 mm sur plats pour un écrou M2,5) au-dessus de `plot_fond` (trou Ø2,7), collées ensemble avec l'écrou dans l'alvéole, non collé (il se pose au fond de l'alvéole et ne tourne pas). Coller chaque plot dans un angle intérieur, **face haute à fleur du bord supérieur des parois**.
- **Couvercle démontable** : 4 boulons **M2,5 raccourcis à 14 mm** (rondelle sous la tête) traversent le couvercle (5 mm) et l'écrou posé au fond de l'alvéole (de 8 à 10 mm sous la face du couvercle) ; la pointe finit dans le trou de la couche inférieure. Rien ne tourne dans le bois.
- **Pi** : les trous de la carte font 2,75 mm : le M2,5 y passe ajusté (le M4 ne passe pas). 4 **piliers** `pilier_pi` (9 × 9 × 5 mm, trou Ø2,7) sont collés sur le fond aux repères gravés ; un écrou M2,5 est inséré **par le dessous** dans chaque trou hexagonal du fond (il ne tourne pas ; une goutte de colle l'empêche de ressortir), puis 4 boulons **M2,5 raccourcis à 10 mm** (rondelle sous la tête) traversent la carte et le pilier et se vissent dans l'écrou. Pas d'entretoise filetée à acheter.
- **Écran** : 4 boulons **M2,5 raccourcis à 10 mm** depuis la face avant (tête visible), carte plaquée contre le couvercle (le verre entre dans la fenêtre), écrou M2,5 derrière : couvercle 5 mm + carte 1,6 + écrou 2 (+ rondelles 2 × 0,5) = 9,6 mm. Si une entretoise est nécessaire (verre trop épais pour la fenêtre), ajouter sa hauteur (vis de 12 mm). **Les trous de l'écran (Ø2,7) ne laissent que 0,45 mm de bois contre les coins de la fenêtre** : ne pas serrer fort, ou ne pas mettre de rondelle côté couvercle. Fils Dupont assez longs (≈ 15 cm) pour poser le couvercle à côté de la boîte.
- **Bouton** : trou de Ø12 mm (filetage mesuré 11,62 mm), bague de serrage derrière.

## Ouvertures
Mur droit : **une seule ouverture** de 54,7 × 20,6 mm pour Ethernet + 2 USB (marges de 1,3 à 3 mm autour des prises : un manchon de fiche est plus large que la prise) ; mur avant : micro-USB d'alimentation, 18 × 11 mm. Les fiches doivent traverser 5 mm de contreplaqué + 0,5 mm de jeu (1,5 mm côté micro-USB) avant d'atteindre la prise : **à essayer avec vos vraies fiches** (gabarit en carton ou découpe d'essai de la paroi) avant de tout coller. Fentes d'aération : mur gauche, mur arrière, fond (sans toucher aux queues droites). **Carte micro-SD** : pas d'accès extérieur (elle est sous le Pi, loin des parois ; les mises à jour se font en OTA). Pour la changer, ouvrir le couvercle.

## Quincaillerie
12 boulons **M2,5 × 20** avec écrou (5,0 mm sur plats, 2 mm) et rondelle, à raccourcir à la scie ou à la meuleuse : 4 à 14 mm (couvercle), 8 à 10 mm (4 pour le Pi, 4 pour l'écran) ; ébavurer les bouts coupés pour que l'écrou passe ; 1 bouton à anneau rouge (filetage 11,62 mm) ; 4 pieds adhésifs (facultatifs).

## Câblage interne et petite carte
**Attention, GPIO sous l'écran** : le Pi étant près du mur avant, son connecteur GPIO (y ≈ 51-56 mm) passe sous l'écran (y 13-61 mm) : embouts des Dupont droits vers z ≈ 30, dos de l'écran vers z ≈ 34-36 : 4 à 6 mm de jeu pour courber les fils. Si ça frotte : connecteurs coudés ou fils soudés.
Fils Dupont droits : l'écran (dos de la carte vers z ≈ 34 mm, embouts vers z ≈ 17-20) et le connecteur GPIO du Pi (embouts vers z ≈ 30) tiennent sous le couvercle ; broches à 90° inutiles tant que le connecteur mesure moins de ~25 mm. Fils de 15 cm minimum. La carte du transistor (30 × 20 mm) se pose au fond à l'avant gauche (repère **gravé en bleu** sur le fond, `CARTE_*` dans `boitier.py` ; fixation par mousse double face, ou `CARTE_TROUS` si l'on veut la visser) : cette zone est hors de l'emprise du bouton (corps à z > 13 mm, y 26-48).

## Gravures (bleu dans `boitier.svg`, à régler à part de la découpe)
- Fond : contour de 30 × 20 mm repérant la carte du transistor (face **intérieure** vers le haut pour graver).
- Couvercle : « REC » sous le bouton et « XR18 REC » sous l'écran, capitales de 5 mm, lisibles depuis l'avant (face extérieure vers le haut). Texte plein (remplissage) ; contours générés avec matplotlib (DejaVu Sans Bold), omis si matplotlib est absent.

## Aperçu
`py boitier/rendu.py` (nécessite matplotlib, uniquement pour ce rendu) produit `apercu-sans-accessoires.png` et `apercu-avec-accessoires.png` (vues avant-droite et arrière-gauche) et `apercu-plan.png` (vue de dessus, couvercle ôté : Pi, carte, empreintes du bouton et de l'écran).
