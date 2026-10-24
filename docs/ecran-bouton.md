# Face avant : écran OLED, bouton REC/STOP, LED

Matériel **facultatif** : une seule image pour tous les boîtiers. Au démarrage, l'application cherche un écran OLED sur le bus I²C ;
**s'il n'y en a pas, rien ne change** (aucun processus supplémentaire ne reste actif). Tout ce qui suit est **à vérifier sur le matériel**.

## Matériel

| Pièce | Remarque |
|---|---|
| Écran OLED 128×64 I²C, 4 broches (GND, VCC, SDA, SCL) | SSD1306 ou SSD1309 (2,42" en général) ou SH1106 (1,3" en général) ; alimentation **3,3 V** |
| Bouton poussoir (contact fermé = appuyé) | entre la broche GPIO et la masse |
| 2 LED (rouge, verte) + 2 résistances de 330 Ω | facultatives |

## Câblage (numéros de broches physiques du connecteur 40 broches ; GPIO = numérotation BCM)

| Fonction | Broche | GPIO |
|---|---|---|
| Écran VCC (3,3 V) | 1 | |
| Écran GND | 6 | |
| Écran SDA | 3 | GPIO2 |
| Écran SCL | 5 | GPIO3 |
| Bouton (l'autre côté à la masse, broche 9) | 11 | GPIO17 |
| LED rouge « enregistrement » (anode → résistance → broche, cathode à la masse, broche 14) | 13 | GPIO27 |
| LED verte « prêt » (anode → résistance → broche, cathode à la masse, broche 20) | 15 | GPIO22 |

Les numéros de GPIO se changent dans `xr18rec.json` (`panel_button_gpio`, `panel_led_rec_gpio`, `panel_led_ok_gpio` ; 0 = non câblé).
Les GPIO2 et GPIO3 (bus I²C) ne peuvent pas servir au bouton ni aux LED.

## Réglages (`xr18rec.json` sur la partition de démarrage)

| Clé | Défaut | Rôle |
|---|---|---|
| `panel` | `auto` | `auto` : actif seulement si un écran répond ; `off` : jamais ; `on` : aussi sans écran (bouton et LED seuls) |
| `panel_driver` | `ssd1306` | `sh1106` pour la plupart des écrans 1,3" (sinon l'image est décalée de 2 colonnes) |
| `panel_i2c_address` | `0` | 0 = essai de 0x3C puis 0x3D |
| `panel_rotate` | `false` | écran monté à l'envers |
| `panel_sleep_s` | `300` | l'écran s'éteint après ce délai au repos (ménage l'OLED) ; jamais pendant une prise ni en cas d'alerte ; 0 = jamais |

## Utilisation

- **Appui court au repos** : démarre un enregistrement (nom automatique daté).
- **Pendant une prise, maintenir 2 s** : arrête (barre de progression à l'écran). Un appui court affiche seulement un rappel : pas d'arrêt accidentel.
- **Au repos, maintenir 5 s** : éteint le Pi proprement (à faire avant de couper l'alimentation).
- **LED rouge** : fixe pendant la prise, clignote vite en cas d'alerte. **LED verte** : prêt ; clignote s'il y a une alerte.
- **Écran** : au repos, version, « PRET » (ou « ERREUR »), temps restant, stockage, adresse IP et nom `.local` en alternance. En prise : point rouge clignotant, nom de la session, chronomètre, temps restant, espace libre. Une alerte remplace la dernière ligne (barre inversée, plusieurs alertes défilent).

Aperçu sans matériel : `python3 -m xr18rec panel --preview repos|enregistrement|alerte` (image en texte).

**Simulateur interactif** (poste de développement) : `PORT=8099 sh scripts/dev-ui.sh` lance aussi un simulateur sur `http://localhost:8100` (port de l'enregistreur + 1 ; `SIM_PORT` pour le changer) : l'écran, les deux LED et un bouton cliquable (clic court, ou maintenir la souris 2 s / 5 s). C'est le vrai code de `panel.py` contre le serveur de développement (vrais REC/STOP avec le faux arecord) ; l'extinction est simulée. Lancement à la main : `python3 -m xr18rec panel --simulate --config <fichier>`.

## Mise en route sur un Pi déjà installé (sans reflasher)

L'image publiée à partir de cette version active le bus I²C. Sur une carte installée avant, deux choses sont à faire **une fois** :

1. Dans `config.txt` (sur la partition de démarrage, lisible depuis Windows), ajouter la ligne `dtparam=i2c_arm=on`, puis redémarrer le Pi.
2. Mettre à jour l'application (page ⚙ Paramètres) : le service charge le module `i2c-dev` et reçoit les groupes `i2c` et `gpio`.

Dépannage : `ls /dev/i2c-1` doit exister ; `i2cdetect -y 1` (paquet `i2c-tools`) doit montrer `3c` ou `3d`. Le journal (`journalctl -u xr18rec`) indique « face avant : écran détecté » ou « aucun écran détecté ».

## Ce qui n'est pas validé

Branchement réel (adresse, type de contrôleur, orientation), structures GPIO (`gpio.py`, tailles testées mais pas l'appel noyau), lecture du bouton, charge sur le Pi 2 B, permissions des groupes `i2c`/`gpio` pour l'utilisateur du service.
