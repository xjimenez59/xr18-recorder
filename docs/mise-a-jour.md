# Mise à jour de l'application sans reflasher la carte SD

Chaque release publiée sur GitHub contient, en plus de l'image à flasher, un petit fichier
**`xr18rec-update-X.Y.Z.tar`** (environ 70 Ko) : le programme de l'enregistreur, signé par le projet.
Le Pi l'installe sans toucher à la carte SD, aux réglages ni aux sessions.

> Le mécanisme est présent dans les images **`v0.3.0` et suivantes** : le passage vers cette première
> version se fait encore par reflashage ; les mises à jour suivantes n'en ont plus besoin.

## Installer une mise à jour (page ⚙ Paramètres > « Mise à jour de l'application »)

Trois façons de remettre le paquet au Pi ; il est vérifié de la même manière dans tous les cas :

1. **Disque USB** : copier `xr18rec-update-X.Y.Z.tar` **à la racine** d'un disque branché sur le Pi (par exemple le disque d'enregistrement). La page le propose d'elle-même (« Chercher sur un disque USB »).
2. **Envoi depuis la page** : télécharger le fichier sur le PC, puis « Envoyer un fichier… » (le PC doit être sur le même réseau que le Pi ; le Pi n'a pas besoin d'Internet).
3. **Internet** : « Rechercher sur Internet » interroge la dernière release du dépôt public (`update_repo` dans la configuration). Demande que le Pi ait accès à Internet à ce moment-là.

Le paquet est d'abord **vérifié** (signature, contenu) et affiché comme « prêt à installer » ; l'installation ne
démarre qu'après « Installer maintenant ». **Elle est refusée pendant un enregistrement**, car elle redémarre l'application
(quelques secondes).

## Ce qui se passe à l'installation

1. Le service `xr18rec-update` (root, distinct de l'application web) recopie le paquet, **vérifie sa signature** avec la clé publique de l'image (`/etc/xr18rec/update-key.pub`), l'extrait sans suivre de lien ni de chemin sortant, et contrôle que la nouvelle version se charge avec la configuration actuelle.
2. La version est installée dans `/opt/xr18rec/releases/X.Y.Z/` ; le lien `/opt/xr18rec/current` est basculé d'un seul coup.
3. L'application redémarre. Le service attend qu'elle réponde **avec la nouvelle version** (45 s maximum).
4. Sinon : **retour automatique à la version précédente** (lien, services et règles système remis comme avant) et message d'erreur dans la page.

Les trois dernières versions sont conservées. Une version identique ou plus ancienne est refusée (sauf demande explicite).

## Sécurité

La page de paramètres n'a pas de code PIN : sans signature, n'importe quel appareil du réseau pourrait faire exécuter du code au Pi.
Chaque paquet est donc signé (Ed25519) ; **seule la clé privée du projet permet d'en fabriquer un accepté**. Cette clé n'est pas dans le
dépôt : elle est dans le secret GitHub `UPDATE_SIGNING_KEY` (utilisé par la construction) et dans une copie de sauvegarde du mainteneur.
La clé publique, elle, est dans `image/files/update-key.pub` et dans chaque image.
Le paquet ne peut installer que : le programme, des services systemd, des règles udev et des règles polkit (dossiers fixes).

Si la clé privée est perdue ou compromise, il faut en créer une nouvelle et **reflasher** (la clé publique est dans l'image).

## Comment fonctionne la clé de signature

C'est une **paire de clés** : la clé *privée* (`update-signing.pem`) sert à **signer**, la clé *publique* (`update-key.pub`) sert à **vérifier**.
Tout le monde peut vérifier ; seul celui qui détient la clé privée peut signer. C'est pour cela que la clé privée n'est jamais dans le dépôt :
publiée, elle permettrait à n'importe qui de fabriquer un paquet que les Pi accepteraient. La clé publique, elle, peut être publique :
elle est dans le dépôt et dans chaque image.

## Reprendre le projet (utilisateurs et forks)

- **Vous utilisez les images et les releases du projet d'origine** : rien à faire. Votre Pi contient la clé publique du mainteneur, il accepte
  ses paquets (disque USB, envoi, ou Internet) et refuse tout le reste. Vous n'avez aucune clé à gérer.
- **Vous modifiez le projet et fabriquez vos propres images ou paquets** : vous devez avoir **votre** paire de clés, car un paquet signé par une autre
  clé est refusé (c'est voulu : sinon la signature ne protégerait rien). Dans votre copie (fork) :
  1. générer votre paire : `openssl genpkey -algorithm ED25519 -out update-signing.pem` puis `openssl pkey -in update-signing.pem -pubout -out image/files/update-key.pub` ;
  2. garder `update-signing.pem` **hors du dépôt**, et le placer dans le secret `UPDATE_SIGNING_KEY` de votre dépôt (`gh secret set UPDATE_SIGNING_KEY < update-signing.pem`) ;
  3. remplacer `update_repo` (défaut dans `xr18rec/config.py`, ou dans `xr18rec.json`) par le nom de votre dépôt, pour que la recherche sur Internet interroge vos releases ;
  4. publier un tag : vos images font confiance à votre clé, et vos Pi n'acceptent que vos paquets.
- Un Pi flashé avec l'image d'un mainteneur n'accepte **pas** les paquets d'un autre : pour changer de mainteneur, il faut reflasher avec l'image de l'autre.

## Ce que la mise à jour ne couvre pas

- Le système (noyau, paquets Debian, pilotes) : il se met à jour en reflashant l'image.
- Un paquet qui exige un installateur plus récent que celui de l'image est refusé avec le message « il faut reflasher ».

## Mainteneur : publier une version

1. Une fois : créer la paire de clés (`openssl genpkey -algorithm ED25519 -out update-signing.pem`, clé publique : `openssl pkey -in update-signing.pem -pubout -out image/files/update-key.pub`), garder la clé privée **hors du dépôt**, et la stocker dans le secret du dépôt :
   `gh secret set UPDATE_SIGNING_KEY < update-signing.pem`.
2. Publier un tag `vX.Y.Z` (`git tag -a vX.Y.Z -m "résumé" && git push origin vX.Y.Z`) : la construction teste, fabrique l'image **et** le paquet signé, et les joint à la release.
3. Fabriquer un paquet à la main (essais) : `python3 scripts/make_update_package.py --version X.Y.Z --key update-signing.pem --out dist/` ;
   le vérifier : `python3 -m xr18rec update inspect dist/xr18rec-update-X.Y.Z.tar` (avec `XR18REC_UPDATE_PUBKEY=image/files/update-key.pub` hors du Pi).

## Fichiers et services

| Élément | Rôle |
|---|---|
| `xr18rec/updater.py` | installateur (root) : vérification, extraction, bascule, retour arrière |
| `xr18rec/updates.py` | côté application : réception (USB, envoi, Internet) et demande d'installation |
| `xr18rec-update.path` / `.service` | déclenchés par le fichier `/var/lib/xr18rec/update/ready` que l'application écrit |
| `/var/lib/xr18rec/update/` | échange avec l'application (non fiable : tout y est revérifié) |
| `/var/lib/xr18rec/update-state/last.json` | résultat de la dernière installation (écrit par root seul) |
| `scripts/make_update_package.py` | fabrication et signature d'un paquet |
