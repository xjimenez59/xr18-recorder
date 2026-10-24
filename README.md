# xr18-recorder : enregistreur multipiste pour Behringer XR18

Un Raspberry Pi 2 B enregistre les 18 voies USB de l'XR18 (16 entrées + mix) sur un disque externe ; démarrage/arrêt depuis la tablette via une page web. Contexte détaillé : `CLAUDE.md`.

## Poste de développement

```bash
python3 -m unittest discover -s tests          # ~7 s, utilise un faux arecord

# Essayer le serveur avec le faux arecord (aucun matériel)
cat > /tmp/xr.json <<EOF
{"arecord_bin": "$PWD/tests/fake_arecord.py", "output_dir": "/tmp/rec", "port": 8080,
 "segment_seconds": 5, "min_free_gb": 0}
EOF
chmod +x tests/fake_arecord.py
python3 -m xr18rec serve --config /tmp/xr.json     # puis http://localhost:8080
```

## Sur le Pi

```bash
scripts/deploy.sh pi@192.168.8.11
ssh pi@192.168.8.11 'sudo ~/xr18-recorder/scripts/install-pi.sh'
~/xr18-recorder/scripts/probe-xr18.sh              # XR18 en USB : carte, formats, débits
```
**Installation par image (recommandée)** : voir [docs/installation-image.md](docs/installation-image.md). **Mise à jour sans reflashage** : [docs/mise-a-jour.md](docs/mise-a-jour.md).

Installation manuelle : adapter `config/xr18rec.json` (`device`, `format`, `output_dir`), la copier en `/etc/xr18rec.json`, puis `sudo systemctl enable --now xr18rec`. Interface : `http://<ip>:8080`.

## Après le concert (sur PC)

```bash
python3 -m xr18rec split take*.wav --out ./pistes     # un WAV mono par voie, segments concaténés
```
Ou importer directement les segments multicanaux dans le DAW.

## Avant de s'y fier
Faire le test de `docs/plan-de-tests.md` (1 h sans overrun avec le disque réel). Rien n'a été validé sur un XR18 ni sur un Pi.
