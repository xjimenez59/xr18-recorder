#!/bin/sh
# Lance l'interface avec le faux arecord (aucun matériel requis). À exécuter sous Linux/WSL.
# Puis ouvrir http://localhost:8080 (enregistreur) et http://localhost:8080/mix (autre port : PORT=8099 sh scripts/dev-ui.sh)
#   (retours) dans le navigateur Windows.
set -e
PORT="${PORT:-8080}"
OSC_PORT="${OSC_PORT:-10024}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="${TMPDIR:-/tmp}/xr18rec-dev"
mkdir -p "$TMP"
cat > "$TMP/arecord" <<EOF
#!/bin/sh
export FAKE_ARECORD_SIGNAL=1  # signaux de test : la voie 3 sature 1 s après le début
export FAKE_ARECORD_BPS=200000  # débit simulé plus proche du réel pour le contrôle des niveaux
export FAKE_ARECORD_LOCK="$TMP/carte.lock"  # carte exclusive : un seul programme à la fois, comme l'XR18
exec python3 "$ROOT/tests/fake_arecord.py" "\$@"
EOF
chmod +x "$TMP/arecord"
cat > "$TMP/config.json" <<EOF
{"output_dir": "$TMP/rec", "arecord_bin": "$TMP/arecord", "segment_seconds": 10,
 "min_free_gb": 0.1, "host": "0.0.0.0", "port": $PORT,
 "xr18_host": "127.0.0.1", "xr18_osc_port": $OSC_PORT,
 "state_file": "$TMP/state.json", "settings_file": "$TMP/settings.json", "panel": "off"}
EOF
export XR18REC_UPDATE_DIR="$TMP/update"            # mises à jour : dossiers de test (sur le Pi, ce sont ceux de /var/lib)
export XR18REC_UPDATE_STATE="$TMP/update-state"
export XR18REC_UPDATE_PUBKEY="$ROOT/image/files/update-key.pub"
cd "$ROOT"
python3 tests/fake_xr18.py "$OSC_PORT" &  # faux XR18 : noms de voies et page /mix
FAKE_XR18_PID=$!
python3 -m xr18rec panel --simulate --sim-port "${SIM_PORT:-$((PORT + 1))}" --config "$TMP/config.json" &  # face avant simulée
trap 'kill $FAKE_XR18_PID $! 2>/dev/null' EXIT INT TERM
python3 -m xr18rec serve --config "$TMP/config.json"
