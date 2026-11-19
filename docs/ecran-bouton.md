# Face avant : écran OLED, bouton REC/STOP, LED

Matériel **facultatif** : une seule image pour tous les boîtiers. Au démarrage, l'application cherche un écran OLED sur le bus I²C ;
**s'il n'y en a pas, rien ne change** (aucun processus supplémentaire ne reste actif). Tout ce qui suit est **à vérifier sur le matériel**.

## Matériel

| Pièce | Remarque |
|---|---|
| Écran OLED 128×64 I²C, 4 broches (GND, VCC, SDA, SCL) | SSD1306 ou SSD1309 (2,42" en général) ou SH1106 (1,3" en général) ; alimentation **3,3 V** |
| Bouton poussoir **momentané** (contact fermé = appuyé), de préférence 16 mm à **anneau lumineux rouge** (3 à 6 V ou 5 à 12 V selon le modèle) | contact entre la broche GPIO et la masse |
| Transistor NPN (BC337 ou 2N2222) + résistance de 1 kΩ | pour allumer l'anneau : il consomme ~20 mA, trop pour une broche GPIO |
| LED verte « prêt » + résistance de 330 Ω | **facultative**, désactivée par défaut : l'écran indique l'état |

## Câblage (numéros de broches physiques du connecteur 40 broches ; GPIO = numérotation BCM)

| Fonction | Broche | GPIO |
|---|---|---|
| Écran VCC (3,3 V) | 1 | |
| Écran GND | 6 | |
| Écran SDA | 3 | GPIO2 |
| Écran SCL | 5 | GPIO3 |
| Bouton (l'autre côté à la masse, broche 9) | 11 | GPIO17 |
| Anneau rouge « enregistrement » : GPIO27 → résistance 1 kΩ → base du transistor ; émetteur à la masse (broche 14) ; collecteur → cathode (−) de l'anneau ; anode (+) de l'anneau → 5 V (broche 2) si l'anneau accepte 5 V (sinon son alimentation propre, masses reliées) | 13 | GPIO27 |
| LED verte « prêt » (facultative ; réglage `panel_led_ok_gpio` : 22) : anode → résistance 330 Ω → broche, cathode à la masse (broche 20) | 15 | GPIO22 |

Les numéros de GPIO se changent dans `xr18rec.json` (`panel_button_gpio`, `panel_led_rec_gpio`, `panel_led_ok_gpio` ; 0 = non câblé, valeur par défaut de la LED verte).
Vérifiez sur la fiche de l'anneau les cosses de la LED (souvent marquées +/−) et sa tension : un anneau 5 V peut rester faible en 3,3 V.
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
- **Au repos, maintenir 5 s** : éteint le Pi proprement (à faire avant de couper l'alimentation). Écrans : « Maintenir : ÉTEINDRE » avec barre (dès 1 s), puis « EXTINCTION / Ne pas débrancher » pendant l'arrêt, puis, quand tout est arrêté, « Vous pouvez ÉTEINDRE / Pi arrêté » (écran qui reste allumé tant que l'alimentation est là). Ce dernier écran vient de `xr18rec-bye.service` (`python3 -m xr18rec.bootscreen --bye`) : son `ExecStop` passe en dernier à l'arrêt (après `sync`, avant le démontage de la carte) et n'affiche rien pour un simple redémarrage (`systemctl list-jobs` sans `poweroff.target`/`halt.target`). Sur un Pi déjà installé : `systemctl enable xr18rec-bye` une fois. **Validé sur le Pi le 19/11/2026** (extinction au bouton : les trois écrans s'enchaînent). Essai à blanc : `python3 -m xr18rec.bootscreen --bye --force` (en root, application arrêtée).
- **Anneau (ou LED) rouge** : fixe pendant la prise, clignote vite en cas d'alerte. **LED verte** (si câblée) : prêt ; clignote s'il y a une alerte.
- **Veille de l'écran** : au repos seulement, après `panel_sleep_s` (5 min). Il ne s'éteint **jamais** pendant une prise, tant qu'une alerte est présente ni pendant un appui. Il se rallume tout seul au démarrage d'une prise (bouton ou interface web) ou à l'apparition d'une alerte. Pour le rallumer à la main, appuyer sur le bouton : **ce premier appui ne fait que réveiller l'écran** (il ne lance pas de prise).
- **Écran** : au repos, version, « PRET » (ou « ERREUR »), temps restant, stockage, adresse IP et nom `.local` en alternance. En prise : point rouge clignotant, nom de la session, chronomètre en chiffres sept segments (le plus grand possible), temps restant. **XR18 non branché** (carte absente de `/proc/asound/cards`, `status()["xr18_usb"]` = false) : « PAS D'XR18 » à la place de « PRÊT » et « Brancher l'USB » en bas ; l'interface web affiche « XR18 non branché » ; REC reste actif (il signalera « XR18 INTROUVABLE »). Valeur inconnue (nom de carte non standard) : on n'affirme rien. Une alerte remplace la ligne d'en-tête (barre inversée en haut, plusieurs alertes défilent).

Aperçu sans matériel : `python3 -m xr18rec panel --preview repos|enregistrement|alerte` (image en texte).

**Simulateur interactif** (poste de développement) : `PORT=8099 sh scripts/dev-ui.sh` lance aussi un simulateur sur `http://localhost:8100` (port de l'enregistreur + 1 ; `SIM_PORT` pour le changer) : l'écran, les deux LED et un bouton cliquable (clic court, ou maintenir la souris 2 s / 5 s). C'est le vrai code de `panel.py` contre le serveur de développement (vrais REC/STOP avec le faux arecord) ; l'extinction est simulée. Lancement à la main : `python3 -m xr18rec panel --simulate --config <fichier>`.

## Mise en route sur un Pi déjà installé (sans reflasher)

L'image publiée à partir de cette version active le bus I²C. Sur une carte installée avant, deux choses sont à faire **une fois** :

1. Dans `config.txt` (sur la partition de démarrage, lisible depuis Windows), ajouter la ligne `dtparam=i2c_arm=on`, puis redémarrer le Pi.
2. Mettre à jour l'application (page ⚙ Paramètres) : le service charge le module `i2c-dev` et reçoit les groupes `i2c` et `gpio`.

Dépannage : `ls /dev/i2c-1` doit exister ; `i2cdetect -y 1` (paquet `i2c-tools`) doit montrer `3c` ou `3d`. Le journal (`journalctl -u xr18rec`) indique « face avant : écran détecté » ou « aucun écran détecté ».

## Premier montage (18/11/2026) : écran 2,42" `M242-12864-4-V2.0` (SSD1309, I²C) validé

Pi 2 B, câblage du tableau ci-dessus : l'écran affiche « PRET ». Deux pièges rencontrés :

1. **I²C inactif** : sur le Pi testé (image 0.3.4), `dtparam=i2c_arm=on` était **commenté** dans `/boot/firmware/config.txt` (`/dev/i2c-1` absent). Corrigé à la main (`sudo sed`, redémarrage). `image/customize.sh` contient pourtant la ligne : à contrôler sur une image neuve (l'image flashée est peut-être antérieure).
2. **Pas d'ACK sur le module** : le balayage I²C ne trouvait **rien**, alors que câblage, tensions et GPIO2/3 (`pinctrl get 2,3` → `SDA1`/`SCL1`) étaient bons. Au dos du module, l'inscription « 如需ACK应答，请短接D2 » (« pour l'ACK, court-circuiter D2 ») : D2 est une diode en série sur la ligne SDA ; il faut **relier ses deux plots par une goutte d'étain** (ne pas toucher D1, ni les plots d'adresse `0X7A`/`0X7B`). Ensuite l'écran répond.

Diagnostic sans `i2c-tools` : balayage par `ioctl(I2C_SLAVE)` + `write`/`read` sur `/dev/i2c-1` (en root) ; journal « face avant : écran détecté ». Sur un module muet, vérifier d'abord ce pont avant de chercher dans le câblage. Brochage du module : GND, VDD, SCL, SDA (SCL avant SDA, à l'inverse du tableau : attention).

### Bouton et anneau (19/11/2026)

- **Bouton** `E1745` (marque Gebildet, 16 mm, 4 fils) : **deux fils verts** = contact (broches 11 et 9), **rouge/noir** = anneau LED. Aucune tension écrite sur le bouton ni l'étiquette : essai avec 220 Ω en série, **franc en 5 V** (broche 4). Un appui court au repos lance bien l'enregistrement (sans XR18, l'écran affiche « ERREUR » + « XR18 INTROUVABLE »).
- **Anneau** : 5 V (broche 4) → 220 Ω → fil rouge ; fil noir → collecteur d'un **BC547** (face plate vers soi : C, B, E) ; émetteur → masse (broche 14) ; base → 1 kΩ → GPIO27 (broche 13). Validé par un clignotement forcé (`sudo pinctrl set 27 op dh|dl`) ; le service réécrit GPIO27 chaque seconde, `pinctrl` ne tient donc qu'un instant (clignoter en boucle, ou arrêter le service).
- Pas encore vu avec une vraie prise (anneau fixe, clignotement d'alerte), ni arrêt (2 s) ni extinction (5 s).

## Signe de vie pendant l'allumage (19/11/2026)

Le Pi 2 B met ~45 s à démarrer (noyau 7 s, système 38 s) : sans rien, écran et anneau resteraient noirs tout ce temps. Deux étages :

1. **Anneau allumé par le firmware** : `gpio=27=op,dh` dans `config.txt` (posé par `image/customize.sh`) met GPIO27 à l'état haut dès la mise sous tension, avant tout logiciel. Si `panel_led_rec_gpio` change, changer aussi cette ligne.
2. **Service précoce `xr18rec-boot.service`** (`python3 -m xr18rec.bootscreen`, module léger `bootscreen.py` : ~0,5 s à charger contre 1,1 s pour tout `xr18rec`) : lancé dès `systemd-modules-load` (≈ 15 s après la mise sous tension, sans attendre réseau ni montages), affiche « XR18 REC / Démarrage... » (points animés) et fait clignoter l'anneau à 1 Hz. `xr18rec.service` l'arrête par `ExecStartPre=-+systemctl stop --no-block xr18rec-boot` : la face avant normale prend le relais (« PRÊT », anneau éteint) vers 45 s. Mesure : écran à ≈ 19 s.

Pièges rencontrés : **pas de `Conflicts=` entre les deux services** (systemd en abandonne un au hasard : l'application ne démarrait pas) ; la LED est demandée par `_LazyLed` avec une nouvelle tentative par seconde (à 18 s, `/dev/gpiochip0` n'a pas encore son groupe udev : « permission denied ») ; sur un Pi déjà installé, `systemctl enable xr18rec-boot` une fois (la mise à jour ne fait pas `enable`) et `gpio=27=op,dh` à ajouter à la main. **Toujours faire `sync` avant de couper l'alimentation après une modification** : des fichiers écrits juste avant la coupure sont revenus vides (service masqué, application non lancée).

## Ce qui n'est pas validé

Comportement du bouton et de l'anneau pendant une vraie prise, orientation, structures GPIO (`gpio.py`, tailles testées mais pas l'appel noyau), lecture du bouton, charge sur le Pi 2 B, permissions des groupes `i2c`/`gpio` pour l'utilisateur du service.

## Polices

Texte et titres : polices bitmap **Spleen** (6×12, 12×24, 16×32), © Frédéric Cambus, licence BSD 2 clauses (<https://github.com/fcambus/spleen>). Les données sont dans `xr18rec/fontdata.py` (la mention de copyright et la licence y figurent), générées par `scripts/make_font.py` à partir des fichiers BDF d'origine. Le chronomètre est dessiné en sept segments (`panel.big_clock`). Texte sans accents : les accents sont retirés, l'OLED affiche « Pret ».
