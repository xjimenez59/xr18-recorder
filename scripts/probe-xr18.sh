#!/usr/bin/env bash
# Affiche ce que le Pi voit de l'XR18 (à lancer avec l'XR18 branché en USB et allumé).
set -u
echo "== lsusb =="; lsusb 2>/dev/null | grep -i -E "behringer|midas|x18|xr18" || echo "(aucun périphérique évident ; voir lsusb complet)"
echo; echo "== cartes ALSA =="; cat /proc/asound/cards
echo; echo "== arecord -l =="; arecord -l
CARD="${1:-}"
if [ -z "$CARD" ]; then
  CARD=$(awk '/\[/{gsub(/[\[\]]/,"",$2); print $2; exit}' /proc/asound/cards 2>/dev/null)
  echo; echo "Carte non précisée ; essai avec la première : '$CARD' (relancer avec : $0 <nom_de_carte>)"
fi
echo; echo "== paramètres matériels hw:CARD=$CARD,DEV=0 =="
timeout 3 arecord -D "hw:CARD=$CARD,DEV=0" --dump-hw-params -d 1 /dev/null 2>&1 | head -30
echo; echo "== formats et fréquences annoncés (/proc/asound/card*/stream0) =="
cat /proc/asound/card*/stream0 2>/dev/null | grep -E "^[A-Za-z0-9 ]+$|Format|Channels|Rates|Momentary" | head -20 || true
echo "(« Momentary freq » n'apparaît que pendant une capture. La fréquence de l'XR18 se règle sur la console :"
echo " X Air Edit > Setup > Audio/MIDI ; le Pi doit demander la même.)"
