# Architecture

```
XR18 ──USB 2.0 (18×18, classe audio)──► Pi 2 B ──USB──► disque externe (XFS/ext4)
  │                                         │
  └─ Ethernet ── Opal ◄── Ethernet ─────────┘   (tablette : navigateur → http://pi:8080)
```

- `arecord` capture le périphérique ALSA brut `hw:` (pas de `plughw`, pas de rééchantillonnage) et écrit des WAV segmentés.
- `xr18rec serve` (processus unique, stdlib) lance/arrête `arecord`, lit son stderr pour compter les « overrun », surveille l'espace libre, et expose l'interface + l'API JSON.
- Arrêt propre : SIGINT envoyé à `arecord` (en-tête WAV finalisé) ; `systemd` envoie SIGTERM au serveur qui arrête `arecord` avant de quitter.

## Alternatives si le Pi 2 B ne tient pas
- Pi 3 B+/4 (USB et Ethernet moins partagés) ; une seule carte est nécessaire.
- Capture JACK ou `ffmpeg`/`sox` en flux continu avec rotation de fichiers gérée par le programme (sans trou de jonction).
- Format W64/RF64 pour dépasser 4 Gio par fichier.
