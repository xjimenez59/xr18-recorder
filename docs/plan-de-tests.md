# Plan de tests matériel

1. **Détection** : XR18 en USB (mode « USB audio » actif) ; `scripts/probe-xr18.sh` ; noter le nom de carte, formats, débits : ________
2. **Capture courte** : `arecord -D <dev> -c 18 -r 48000 -f <fmt> -d 10 /mnt/recordings/t.wav` ; ouvrir dans un DAW : 18 pistes, signal sur la bonne voie (taper sur un micro, vérifier l'ordre des voies : 1-16 puis mix G/D).
3. **Endurance** : `scripts/soak-test.sh 3600` (1 h) avec disque réel, Ethernet connecté et interface web ouverte sur la tablette. Résultat : overruns = ____ ; taille attendue = ____ ; taille réelle = ____.
4. **Jonction des segments** : enregistrer un signal continu (générateur de sinus du XR18 sur une voie), segment de 60 s, concaténer (`xr18rec split`) et chercher des sauts à la jonction (Audacity, vue spectrogramme).
5. **Coupure brutale** : débrancher l'alimentation pendant l'enregistrement ; vérifier que les segments précédents sont lisibles et que le dernier est récupérable (`xr18rec info`).
6. **Disque plein** : tester `min_free_gb` élevé ; l'enregistrement s'arrête proprement et l'interface le signale.
7. **Alimentation** : disque sur son alimentation propre ; consommation mesurée du Pi + disque : ____
8. **Température** du Pi après 1 h : ____
