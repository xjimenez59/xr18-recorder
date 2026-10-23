#!/usr/bin/env bash
# Test d'endurance : enregistre N secondes (défaut 3600) avec arecord seul et compte les overruns.
# Usage : soak-test.sh [secondes] [périphérique] [format] [dossier]
set -u
DUR="${1:-3600}"
DEV="${2:-hw:CARD=XR18,DEV=0}"
FMT="${3:-S32_LE}"
DIR="${4:-/mnt/recordings/soak}"
mkdir -p "$DIR"
LOG="$DIR/soak-$(date +%Y%m%d-%H%M%S).log"
echo "Enregistrement de $DUR s sur $DEV ($FMT, 18 voies, 48 kHz) -> $DIR ; journal : $LOG"
arecord -D "$DEV" -c 18 -r 48000 -f "$FMT" -t wav --max-file-time 900 -d "$DUR" "$DIR/soak.wav" 2> "$LOG"
echo "overruns : $(grep -ci overrun "$LOG")"
ls -l "$DIR"/soak*.wav
echo "Octets attendus ≈ $((DUR * 18 * 48000 * $( [ "$FMT" = S24_3LE ] && echo 3 || echo 4 )))"
