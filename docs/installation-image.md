# Installation par image (carte SD à flasher)

L'image contient Raspberry Pi OS Lite 32 bits (compatible avec tous les Pi 2 B) et l'enregistreur, déjà
installé et démarré automatiquement. **Aucun accès Internet n'est nécessaire sur le Pi.**

## 1. Obtenir l'image

L'image est construite par GitHub Actions ([workflow](../.github/workflows/build-image.yml), 15 à 25 min) :

- **Version** : publier un tag `vX.Y.Z` (`git tag v0.1.0 && git push --tags`) ; l'image est jointe à la release GitHub.
- **Essai** : onglet *Actions* → *Image Raspberry Pi* → *Run workflow* ; l'image est un artefact téléchargeable 14 jours.

Le fichier est `xr18rec-<version>.img.xz` (+ somme `.sha256`). Inutile de le décompresser.

## 2. Flasher la carte SD (8 Go minimum, classe A1 conseillée)

[Raspberry Pi Imager](https://www.raspberrypi.com/software/) → *Choisir l'OS* → *Utiliser personnalisé* → le fichier `.img.xz`.
Si l'outil propose de personnaliser le système (nom d'hôte, utilisateur, Wi-Fi…), répondre **Non** :
l'image est déjà configurée. [balenaEtcher](https://etcher.balena.io/) convient aussi.

## 3. Régler l'adresse de l'XR18 (avant le premier démarrage)

Remettre la carte dans le PC : la partition `bootfs` apparaît comme un lecteur. Ouvrir `xr18rec.json`,
renseigner `"xr18_host": "192.168.x.y"` (l'adresse de l'XR18, à **réserver** dans le routeur), enregistrer.
Sans cette adresse, l'enregistrement fonctionne mais sans noms de voies, projet Reaper ni retours musiciens.
Les autres réglages sont décrits dans `LISEZMOI-XR18.txt` sur la même partition.

## 4. Brancher et démarrer

Carte SD dans le Pi, **câble Ethernet vers le routeur de scène**, XR18 en USB, disque USB (ext4 ou XFS conseillé), alimentation.
Le premier démarrage est plus long (agrandissement de la partition, redémarrage automatique). Ensuite :

- `http://xr18rec.local/` (ou l'adresse IP du Pi lue dans le routeur) ;
- page d'accueil : état, REC/STOP, QR code des retours musiciens, mute ;
- page `/rec` : emplacement des enregistrements (carte SD ou disque USB) et sessions ;
- page `/settings` (⚙ Paramètres) : adresse de l'XR18 (avec détection sur le réseau), audio, retours, heure du Pi, redémarrage et extinction.
  Ces réglages sont mémorisés sur la carte (`/var/lib/xr18rec/settings.json`) et passent **avant** `xr18rec.json` ; « Revenir aux réglages d'origine » les efface.

## Ce que fait l'image

| Élément | Détail |
|---|---|
| Application | `/opt/xr18rec`, service `xr18rec` (utilisateur dédié `xr18rec`, port 80) |
| Réglages | `/boot/firmware/xr18rec.json` validé puis copié dans `/etc/xr18rec.json` à chaque démarrage ; erreur → `xr18rec-erreur.txt` sur la même partition |
| Disques USB | montés automatiquement dans `/media/xr18/<étiquette>` (udev + service `xr18rec-mount@`), dossier `xr18-recordings` confié au service |
| Réseau | DHCP, nom `xr18rec` (mDNS avec avahi → `xr18rec.local`) |
| Réglages de l'interface | `/var/lib/xr18rec/settings.json` ; règle polkit autorisant le service à régler l'heure, redémarrer et éteindre le Pi |
| SSH | **désactivé** ; créer un fichier vide `ssh` sur `bootfs` pour l'activer. Utilisateur `xr18`, mot de passe initial `xr18rec`, **changement imposé à la première connexion** |
| Carte SD | `noatime`, journaux bornés à 64 Mo, carte son interne désactivée, `max_usb_current=1` |

## Dépannage

- *La page ne s'ouvre pas avec `xr18rec.local`* : utiliser l'adresse IP (liste des clients du routeur) ; voir `docs/architecture.md` pour la résolution des noms.
- *Réglages ignorés* : lire `xr18rec-erreur.txt` sur `bootfs`.
- *Disque USB absent de la liste* : branché avant le démarrage ? formaté ext4/XFS/exFAT/NTFS/FAT ? (avec SSH : `journalctl -u 'xr18rec-mount@*'`).

## À vérifier sur le matériel (non testé)

L'image n'a pas encore été démarrée sur un Pi 2 B réel. À contrôler : démarrage et agrandissement de la partition,
résolution `xr18rec.local`, montage d'un disque ext4/XFS/exFAT, droits d'écriture, test d'une heure
(`scripts/soak-test.sh`), arrêt brutal (coupure de courant).

## Mises à jour (à venir)

Pour l'instant : reflasher l'image. Les réglages sont sur `bootfs` (à noter avant de reflasher). Piste : mise à jour
depuis GitHub quand le Pi a accès à Internet, sans reflasher.
