# Métronome (clic sur un retour USB de l'XR18)

Le Pi envoie un clic à l'XR18 par une voie de **retour USB** (`aplay`, mêmes 18 voies que la capture). Il sert de
référence commune aux musiciens (in-ear) et il est enregistré sur sa piste, ce qui donne le tempo réel de la prise.

## Réglage de la console (X Air Edit)
1. **Voie du clic** (16 par défaut, `click_channel`) : Routing > Input, source « USB 16 » (elle remplace l'entrée analogique 16).
2. Désassigner la voie du **LR** (la salle ne l'entend pas) ; régler ses **départs** vers les bus des retours in-ear
   (ou le faire depuis la page `/mix` : le clic y apparaît comme une voie ordinaire).
3. Pour l'enregistrer : Routing > Out > USB sends > **USB send 16** = la voie 16, avec un « signal tap » **autre
   qu'Analog** (par ex. Pre EQ). Avec « Analog » le retour USB n'est jamais renvoyé vers l'USB (essai du 19/11/2026).

## Réglages (carte « Métronome » de l'accueil, mémorisés dans `settings.json`)
tempo 30-240 bpm, temps par mesure (1-12), temps 1 plus aigu et plus fort (désactivable : tous les temps identiques),
son, niveau (crête, -40..0 dB), « démarrer avec REC ». Réglages appliqués au battement suivant, même pendant une prise.

Sons (synthèse, attaque de 2 ms pour éviter le claquement, durée ≤ 200 ms) : **bois** (bloc de bois, défaut : le plus
recommandé pour de longues séances), **cloche** (cowbell), **bip** (sinus doux, enveloppe en cosinus), **tic** (sec, pour
se faire entendre dans un mix chargé ; plus fatigant). Avantages/inconvénients d'après les guides courants : le bois
« se tient » sous un instrument acoustique, la cloche plaît aux batteurs, les bips percent mais fatiguent.

« Avec REC » : le clic démarre au début de la prise (≈ 0,5 s plus tard à cause de la mémoire tampon d'`aplay`) et s'arrête
avec elle ; un clic lancé à la main avant REC continue après l'arrêt. Une panne du clic n'empêche jamais la prise.

## API
`GET /api/click` (état, réglages, liste des sons), `POST /api/click/start`, `POST /api/click/stop` ; les réglages passent
par `POST /api/settings` (`click_bpm`, `click_beats`, `click_accent`, `click_sound`, `click_channel`, `click_level_db`,
`click_with_rec`). Refus (409) si l'XR18 n'est pas branché.

## Précision mesurée sur Pi 2 B (19/11/2026, capture 18 voies en parallèle, 0 overrun, 0 underrun)
Chaque battement est placé à l'échantillon près côté Pi. Sur 5 min à 120 bpm : gigue de 0,7 ms (écart-type, 2 ms au pire),
aucun clic manquant, tempo constant mais **environ 0,03 % plus rapide** que la consigne (période de 23 992 échantillons au lieu
de 24 000) : la référence est le clic enregistré, pas la consigne. Un essai plus long (1 h) n'a pas été fait.
Outils : `scripts/click-play.py` (essai sans application) et `scripts/click-analyse.py` (dérive et gigue d'un clic enregistré).
