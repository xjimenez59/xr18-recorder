#!/usr/bin/env bash
# Usage : scripts/deploy.sh pi@192.168.8.11
set -euo pipefail
TARGET="${1:?usage: deploy.sh utilisateur@hote}"
cd "$(dirname "$0")/.."
rsync -av --delete --exclude '.git' --exclude '__pycache__' --exclude '.venv' ./ "$TARGET:xr18-recorder/"
