# CLAUDE.md : enregistreur multipiste Behringer XR18 (Raspberry Pi 2 B)

Contexte pour l'assistant (extension Claude pour VS Code). Projet en français ; réponds en français.
Projet **séparé** du contrôleur d'éclairage (`stage-lights`) : un **second** Pi 2 B dédié, pour ne pas mêler enregistrement audio et DMX.
Les éléments marqués **(à vérifier)** ne sont pas validés sur matériel.

## Besoin
Enregistrer une répétition ou un concert : les 16 entrées indépendantes de l'XR18 + le mix général (18 voies USB, 24 bits, 44,1/48 kHz) vers un disque externe branché sur le Pi, commande depuis la tablette (navigateur), puis récupération des fichiers pour le mixage dans un DAW sur PC.

## Faits techniques retenus
- XR18 : USB 2.0 type B, interface audio **18×18** conforme à la classe USB audio (reconnue par ALSA/Linux sans pilote) **(à vérifier sur le Pi)**.
- Débit à 48 kHz : 18 voies × 3 octets (S24_3LE) ≈ 2,6 Mo/s ≈ 9,3 Go/h ; en S32_LE ≈ 3,5 Mo/s ≈ 12,4 Go/h. Format réellement exposé par l'XR18 : **(à vérifier)** avec `scripts/probe-xr18.sh`.
- Limite d'un WAV classique : 4 Gio ≈ 27 min à 18 voies S24_3LE. D'où la **segmentation** (`arecord --max-file-time`, 900 s par défaut ; `config.validate()` refuse un segment trop long). Nommage observé : `take.wav` puis `take-01.wav`, `take-02.wav`… (**à vérifier** : le premier fichier est renommé `-01` à la rotation).
- `arecord` finalise l'en-tête WAV sur SIGINT ; un arrêt brutal (coupure) laisse un en-tête non finalisé (`wavtools.read_info` le tolère).
- Disque : XFS ou ext4 (le projet X32-Recorder sur Pi 4 recommande XFS). SSD ou disque auto-alimenté ; le port USB du Pi 2 B ne fournit pas assez de courant pour un disque 2,5" à plateaux (`max_usb_current=1` aide mais ne suffit pas toujours).
- **Point faible du Pi 2 B** : USB 2.0 partagé avec l'Ethernet (LAN9514) : disque + 18 voies + réseau sur le même bus. Prévoir un test de 1 h avec comptage des « overrun » (`scripts/soak-test.sh`). Si insuffisant : Pi 4/5 ou enregistrer sur un seul disque réseau n'est PAS une solution (même bus).
- Le Pi 2 B n'a pas de WiFi : il se branche en Ethernet sur le routeur de scène (Opal), comme dans `stage-lights`.

## Contenu du dépôt
- `xr18rec/` (stdlib uniquement) : `config.py` (JSON + validation), `session.py` (pilote `arecord`, compte les overruns, surveille le disque), `server.py` (HTTP + API JSON), `wavtools.py` (lecture d'en-têtes, séparation en WAV mono), `xr18osc.py` (noms/couleurs des voies via OSC UDP 10024), `storage.py` (emplacements d'enregistrement), `mixer.py` (pont HTTP→OSC du mix de retour), `static/mix.html` (page musiciens), `qr.py` (QR code SVG, stdlib, servi sur `/qr/mix.svg` avec l'adresse utilisée par le client), `reaper.py` (génère le `.rpp`), `static/index.html` (interface tablette), `__main__.py` (CLI : `serve`, `split`, `info`, `config`, `names`, `reaper`, `mixprobe`).
- `tests/` : `python3 -m unittest discover -s tests` avec un **faux arecord** (`tests/fake_arecord.py`).
- `scripts/` : `probe-xr18.sh`, `soak-test.sh`, `install-pi.sh`, `deploy.sh`, `xr18rec.service`.
- `config/xr18rec.json` : exemple de configuration. `docs/` : architecture, disque/formats, plan de tests.

## API
`GET /api/status`, `GET /api/sessions`, `POST /api/start {"name": "..."}`, `POST /api/stop`, `GET /files/<session>/<fichier>`. Aucune authentification : réseau de scène privé uniquement.

## Choix du stockage
`storage.py` liste la carte SD (`output_dir`, défaut `/var/lib/xr18rec/recordings`) et les disques USB déjà montés (`/dev/sd*`, `/dev/nvme*` d'après `/proc/mounts`, dossier `<montage>/xr18-recordings`). Sélection dans l'interface (`GET/POST /api/storage`), refusée pendant un enregistrement, mémorisée dans `state_file`. Pas de montage automatique (droits root) : monter via fstab avec `nofail`. Les sessions de tous les emplacements sont listées et téléchargeables. **À vérifier sur le Pi** : droits d'écriture sur le disque, détection des étiquettes (`/dev/disk/by-label`).

## Mix de retour (in-ear)
Page `/mix` (navigateur, sans app) : chaque musicien choisit son bus Aux 1..6 et règle les départs des 16 voies (noms/couleurs XR18). `mixer.py` = pont HTTP→OSC, un seul client OSC (`/xremote` renouvelé toutes les 8 s ; l'XR18 accepte 8 abonnés max). Sécurité in-ear : `mix_max_db` plafonne côté serveur les départs et le volume général du bus (`/bus/N/mix/fader`, affiché seulement si l'XR18 répond : **à vérifier**). Retours `/rtn/aux` et `/fxrtn/NN` : affichés seulement si l'XR18 répond (**à vérifier** : `python3 -m xr18rec mixprobe --host IP`). API : `GET /api/mix/buses`, `GET /api/mix/<bus>`, `POST /api/mix/<bus>/send {source, db}`. Aucune authentification : un musicien peut ouvrir le bus d'un autre.

Nom d'hôte : `install-pi.sh` règle `xr18rec` (variable `XR18_HOSTNAME`) et installe avahi → `http://xr18rec.local:8080`. Résolution par le routeur GL.iNet (`http://xr18rec:8080`) **à vérifier** ; sinon réservation DHCP + QR code (carte « Retours des musiciens » sur la page d'enregistrement).

## Projet Reaper
Au démarrage, si `xr18_host` est défini, les noms/couleurs sont lus en OSC et mémorisés dans `channels.json` de la session ; à l'arrêt, `<session>.rpp` est écrit (16 pistes mono + piste stéréo « mix » = voies USB 17/18, noms et couleurs XR18). Les pistes pointent sur les mêmes WAV 18 voies via le mode de canal de la prise. Régénération : `python3 -m xr18rec reaper <dossier> [--host IP]`.

## Points non validés
0. Reaper : numérotation `CHANMODE` (mono voie N = N+2, stéréo N/N+1 = 66+N) et routage USB par défaut de l'XR18 (voies 17/18 = main L/R) à valider sur matériel/Reaper ; `/lr/config/name|color` en OSC à valider.
1. Nom de la carte ALSA et formats/débits annoncés par l'XR18 (`arecord -l`, `--dump-hw-params`).
2. Tenue du Pi 2 B (0 overrun sur 1 h, avec disque et réseau actifs).
3. Nommage réel des fichiers segmentés par la version d'`arecord` installée.
4. Disque : alimentation, système de fichiers, montage automatique (`nofail`).
5. Absence de trou (échantillons perdus) à la jonction de deux segments : à vérifier en comparant à une capture continue ; sinon passer à une capture continue avec `sox`/`ffmpeg` ou JACK.

## Pistes d'évolution
Mesure de niveau par voie (barres VU), marqueurs de piste, copie automatique vers un second disque, authentification simple, passage à RF64/W64 pour des fichiers plus longs.

## Conventions
Python ≥ 3.9, stdlib uniquement, tests `unittest`, messages en français.
