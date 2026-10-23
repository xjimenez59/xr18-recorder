#!/usr/bin/env bash
# Installation sur le Pi (Raspberry Pi OS Lite 32 bits). À lancer en root depuis le dépôt copié.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "à lancer avec sudo" >&2; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-pi}"

apt-get update
apt-get install -y python3 alsa-utils xfsprogs usbutils rsync avahi-daemon

# Nom d'hôte : http://xr18rec:8080 (DNS du routeur) ou http://xr18rec.local:8080 (mDNS/avahi)
HOST_NAME="${XR18_HOSTNAME:-xr18rec}"
hostnamectl set-hostname "$HOST_NAME"
if grep -q '^127\.0\.1\.1' /etc/hosts; then sed -i "s/^127\.0\.1\.1.*/127.0.1.1 $HOST_NAME/" /etc/hosts
else echo "127.0.1.1 $HOST_NAME" >> /etc/hosts; fi
systemctl enable --now avahi-daemon

mkdir -p /mnt/recordings
[ -f /etc/xr18rec.json ] || install -m 644 "$REPO/config/xr18rec.json" /etc/xr18rec.json

sed "s|@REPO@|$REPO|g; s|@USER@|$USER_NAME|g" "$REPO/scripts/xr18rec.service" > /etc/systemd/system/xr18rec.service
systemctl daemon-reload
echo "Installé. Monter le disque sur /mnt/recordings (voir docs/disque-et-formats.md), adapter /etc/xr18rec.json,"
echo "lancer scripts/probe-xr18.sh, puis : sudo systemctl enable --now xr18rec"
echo "Nom d'hôte : $HOST_NAME (http://$HOST_NAME.local:8080 ; redémarrer pour que le routeur le voie)"
