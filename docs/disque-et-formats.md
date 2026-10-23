# Disque et formats

| Réglage | Octets/s | Go/h | Segment de 900 s |
|---|---|---|---|
| 18 voies, 48 kHz, S24_3LE | 2 592 000 | 9,3 | 2,3 Go |
| 18 voies, 48 kHz, S32_LE | 3 456 000 | 12,4 | 3,1 Go |
| 18 voies, 44,1 kHz, S24_3LE | 2 381 400 | 8,6 | 2,1 Go |

- Un concert de 3 h en S32_LE ≈ 37 Go. Prévoir 128 Go minimum.
- `format` : l'XR18 annonce probablement 24 bits (S24_3LE) ou 32 bits (S32_LE) **(à vérifier)** ; le choix se fait selon `arecord --dump-hw-params`.
- Formatage XFS : `sudo mkfs.xfs -L recordings /dev/sdX1` ; `/etc/fstab` : `LABEL=recordings /mnt/usb xfs defaults,noatime,nofail 0 2` (le point de montage est libre, `nofail` évite un blocage du démarrage sans disque).
- Choix de l'emplacement : l'interface propose la carte SD (`output_dir`) et tout disque USB monté (`/dev/sd*`, `/dev/nvme*`) ; les sessions d'un disque vont dans `<montage>/xr18-recordings`. Le disque doit être inscriptible par l'utilisateur du service (`sudo chown -R pi /mnt/usb` pour ext4/XFS). Le choix est mémorisé dans `state_file`. Si le disque est absent au démarrage, retour à la carte SD avec un avertissement.
- Disque 2,5" à plateaux : alimentation externe obligatoire (hub alimenté). SSD en boîtier USB recommandé.
- Pas de swap sur la carte SD pendant l'enregistrement.

## Fréquence d'échantillonnage (44,1 ou 48 kHz)

Ce que l'on sait, d'après les fiches techniques et les discussions en ligne (à confirmer sur la console) :

- L'XR18 fonctionne en 24 bits à **44,1 ou 48 kHz** (fiche technique).
- **C'est la console qui fixe l'horloge** : le réglage est dans X Air Edit, *Setup > Audio/MIDI* (témoignage d'un utilisateur sur le forum Audacity). Un modérateur du forum Behringer rappelle que l'horloge de la console et celle de l'ordinateur doivent correspondre, et que le logiciel d'enregistrement doit être réglé sur la même fréquence.
- Aucun paramètre OSC documenté ne donne la fréquence actuelle. `/-prefs` expose en revanche des données réseau sensibles : à ne pas interroger.
- Les problèmes de coupures (xruns) sur Linux, dans les discussions trouvées, viennent surtout du **câble USB** et du **port USB** (l'XR18 est en USB 2.0 ; des ports USB 3 posent problème à certains) et du nombre de périodes (3 plutôt que 2 sous JACK). Le Pi 2 B n'a que de l'USB 2.0, ce qui est plutôt favorable.
- Un message de 2015 sur la liste Linux Audio mentionne un mode 2×2 en plus du mode 18×18 : à vérifier.

Ce que fait le programme :

- Le réglage `rate` est demandé à `arecord`. Avec l'accès direct à la carte (`hw:`), ALSA ne rééchantillonne pas : une fréquence non acceptée est refusée ou ajustée (« rate is not accurate »).
- L'en-tête de chaque WAV porte la fréquence réellement négociée ; **le projet Reaper reprend cette valeur** (pas le réglage), il est donc toujours cohérent avec les fichiers.
- Après le premier segment, l'accueil affiche la fréquence réelle et signale un écart avec le réglage.
- La page *Paramètres* (« Relever ce que la carte accepte ») liste les fréquences, formats et voies que la carte expose et compare au réglage.

À vérifier sur la console : la carte expose-t-elle les deux fréquences ou seulement celle de l'horloge ? (`scripts/probe-xr18.sh`).
