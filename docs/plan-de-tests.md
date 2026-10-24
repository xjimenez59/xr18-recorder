# Plan de tests matériel

1. **Détection** : XR18 en USB (mode « USB audio » actif) ; `scripts/probe-xr18.sh` ; noter le nom de carte, formats, débits : ________
2. **Capture courte** : `arecord -D <dev> -c 18 -r 48000 -f <fmt> -d 10 /mnt/recordings/t.wav` ; ouvrir dans un DAW : 18 pistes, signal sur la bonne voie (taper sur un micro, vérifier l'ordre des voies : 1-16 puis mix G/D).
3. **Endurance** : `scripts/soak-test.sh 3600` (1 h) avec disque réel, Ethernet connecté et interface web ouverte sur la tablette. Résultat : overruns = ____ ; taille attendue = ____ ; taille réelle = ____.
   **Fait le 24/10/2026 sur le Pi 2 B** (voir « Résultats » ci-dessous) : 0 overrun sur 1 h.
4. **Jonction des segments** : enregistrer un signal continu (générateur de sinus du XR18 sur une voie), segment de 60 s, concaténer (`xr18rec split`) et chercher des sauts à la jonction (Audacity, vue spectrogramme).
5. **Coupure brutale** : débrancher l'alimentation pendant l'enregistrement ; vérifier que les segments précédents sont lisibles et que le dernier est récupérable (`xr18rec info`).
6. **Disque plein** : tester `min_free_gb` élevé ; l'enregistrement s'arrête proprement et l'interface le signale.
7. **Alimentation** : disque sur son alimentation propre ; consommation mesurée du Pi + disque : ____
8. **Température** du Pi après 1 h : 46 °C au maximum (voir ci-dessous).

## Résultats

### Endurance 1 h (24/10/2026, Pi 2 B, v0.3.3)

Configuration : XR18 en USB, 18 voies, 48 kHz, S24_3LE, disque USB « Elements » (SSD/disque externe), Ethernet connecté, interface interrogée toutes les 30 s (`/api/status` et `/api/health`) par un script lancé depuis le PC. Prise lancée par l'API HTTP, arrêtée à 60 min.

| Mesure | Résultat |
|---|---|
| Durée | 3 636 s (1 h 00 min 36 s) |
| Dépassements (overruns) | **0**, aucune alerte |
| Processeur | 17 à 23 % en régime normal, pointes à 43 et 50 % |
| Charge (1 min) | 0,6 à 1,26 (4 cœurs) |
| Température | 41 °C au départ, 46 °C au maximum, stable |
| Sous-tension | jamais détectée |
| Requêtes web échouées | 0 sur environ 120 |
| Volume | 9,4 Go (≈ 2,6 Mo/s), conforme au débit calculé |

Conclusion : le Pi 2 B tient 18 voies à 48 kHz en 24 bits sur disque USB avec l'Ethernet actif et l'interface consultée (le bus USB partagé n'a pas été un obstacle sur cette durée).

**Limites de l'essai** : pas de lecture, de téléchargement ni de contrôle de niveaux simultanés (refusés pendant une prise de toute façon) ; face avant non branchée ; une seule prise d'1 h ; pas de signal audio mesuré à la jonction des fichiers (point 4 non fait) ; durées supérieures à 1 h non essayées.

**Segmentation observée** : `arecord` coupe à **2 Gio** (2 147 483 654 octets, ≈ 13,8 min à 18 voies S24_3LE) avant les 900 s configurés. Nommage confirmé : `take-01.wav`, `take-02.wav`… (le premier fichier est renommé `-01` à la rotation). Session d'essai : 4 fichiers de 2 Gio + 1 de 0,83 Go.
