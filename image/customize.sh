#!/bin/bash
# Personnalise Raspberry Pi OS Lite pour en faire l'enregistreur XR18.
# Exécuté DANS l'image (chroot ARM émulé) par la CI ; le dépôt est dans le répertoire courant.
set -euo pipefail

APP=/opt/xr18rec
export DEBIAN_FRONTEND=noninteractive
HERE="$(pwd)"

echo "== Image de base : $(. /etc/os-release && echo "$PRETTY_NAME") ($(dpkg --print-architecture))"

# ---- paquets -------------------------------------------------------------------------------
apt-get update
apt-get install -y --no-install-recommends python3 alsa-utils usbutils xfsprogs exfatprogs ntfs-3g avahi-daemon rsync

# ---- application ---------------------------------------------------------------------------
install -d "$APP"
cp -r "$HERE/xr18rec" "$APP/"
echo "${IMAGE_VERSION:-dev}" > "$APP/VERSION"
python3 -m compileall -q "$APP" || true   # le service, non root, ne peut pas écrire les .pyc lui-même

id xr18rec >/dev/null 2>&1 || useradd --system --home-dir /var/lib/xr18rec --shell /usr/sbin/nologin --groups audio xr18rec
install -d -o xr18rec -g xr18rec /var/lib/xr18rec /var/lib/xr18rec/recordings
install -d /media/xr18

install -m 644 "$HERE/image/files/xr18rec.json" /etc/xr18rec.json
install -m 644 "$HERE/image/files/xr18rec.service" /etc/systemd/system/xr18rec.service
install -m 644 "$HERE/image/files/xr18rec-mount@.service" /etc/systemd/system/xr18rec-mount@.service
install -m 644 "$HERE/image/files/99-xr18rec-usb.rules" /etc/udev/rules.d/99-xr18rec-usb.rules
install -d /etc/polkit-1/rules.d
install -m 644 "$HERE/image/files/50-xr18rec.rules" /etc/polkit-1/rules.d/50-xr18rec.rules
install -d /etc/systemd/journald.conf.d
install -m 644 "$HERE/image/files/journald-xr18rec.conf" /etc/systemd/journald.conf.d/xr18rec.conf
systemctl enable xr18rec.service avahi-daemon.service

# ---- partition de démarrage (visible depuis Windows) ---------------------------------------
# Dans ce chroot, la partition de démarrage est montée sur /boot (c'est /boot/firmware au runtime).
install -m 644 "$HERE/image/files/xr18rec.json" /boot/xr18rec.json
install -m 644 "$HERE/image/files/LISEZMOI-XR18.txt" /boot/LISEZMOI-XR18.txt
grep -q '^max_usb_current' /boot/config.txt || printf '\n# Enregistreur XR18\nmax_usb_current=1\n' >> /boot/config.txt
sed -i 's/^dtparam=audio=on/dtparam=audio=off/' /boot/config.txt   # la carte son de l'XR18 est la seule utile

# ---- système -------------------------------------------------------------------------------
echo xr18rec > /etc/hostname
if grep -q '^127\.0\.1\.1' /etc/hosts; then sed -i 's/^127\.0\.1\.1.*/127.0.1.1\txr18rec/' /etc/hosts
else printf '127.0.1.1\txr18rec\n' >> /etc/hosts; fi
echo Europe/Paris > /etc/timezone
ln -sf /usr/share/zoneinfo/Europe/Paris /etc/localtime
sed -i '0,/defaults/s/defaults/defaults,noatime/' /etc/fstab   # moins d'écritures sur la carte SD

# Utilisateur local (console / SSH si activé) : mot de passe initial à changer dès la première connexion.
# Sans cela, Raspberry Pi OS attend un assistant interactif sur l'écran au premier démarrage.
if ! id xr18 >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash xr18
    for g in sudo adm audio video plugdev netdev dialout; do
        if getent group "$g" >/dev/null; then usermod -aG "$g" xr18; fi
    done
    echo 'xr18:xr18rec' | chpasswd
    chage -d 0 xr18
fi
systemctl disable userconfig.service 2>/dev/null || true
systemctl enable getty@tty1.service
# SSH reste désactivé : il s'active en déposant un fichier « ssh » sur la partition de démarrage.

# Services inutiles sur un enregistreur sans Bluetooth ni écran
for s in triggerhappy.service bluetooth.service hciuart.service ModemManager.service; do
    systemctl disable "$s" 2>/dev/null || true
done

# ---- vérifications -------------------------------------------------------------------------
(cd "$APP" && python3 -m xr18rec config --config /etc/xr18rec.json >/dev/null)
systemctl is-enabled xr18rec.service
test -f /boot/xr18rec.json && test -f /boot/LISEZMOI-XR18.txt
echo "== Version : $(cat "$APP/VERSION")"

# ---- nettoyage -----------------------------------------------------------------------------
apt-get clean
rm -rf /var/lib/apt/lists/*
