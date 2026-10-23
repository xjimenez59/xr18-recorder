# Disque et formats

| Réglage | Octets/s | Go/h | Segment de 900 s |
|---|---|---|---|
| 18 voies, 48 kHz, S24_3LE | 2 592 000 | 9,3 | 2,3 Go |
| 18 voies, 48 kHz, S32_LE | 3 456 000 | 12,4 | 3,1 Go |
| 18 voies, 44,1 kHz, S24_3LE | 2 381 400 | 8,6 | 2,1 Go |

- Un concert de 3 h en S32_LE ≈ 37 Go. Prévoir 128 Go minimum.
- `format` : l'XR18 annonce probablement 24 bits (S24_3LE) ou 32 bits (S32_LE) **(à vérifier)** ; le choix se fait selon `arecord --dump-hw-params`.
- Formatage XFS : `sudo mkfs.xfs -L recordings /dev/sdX1` ; `/etc/fstab` : `LABEL=recordings /mnt/recordings xfs defaults,noatime,nofail 0 2`.
- Disque 2,5" à plateaux : alimentation externe obligatoire (hub alimenté). SSD en boîtier USB recommandé.
- Pas de swap sur la carte SD pendant l'enregistrement.
