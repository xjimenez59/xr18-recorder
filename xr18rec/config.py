"""Configuration de l'enregistreur (fichier JSON + valeurs par défaut)."""

from __future__ import annotations

import dataclasses
import json
import os
import re
from dataclasses import asdict, dataclass, field, fields

# Octets par échantillon selon le format ALSA
FORMAT_BYTES = {"S16_LE": 2, "S24_3LE": 3, "S24_LE": 4, "S32_LE": 4}

WAV_LIMIT = 4 * 1024**3  # un WAV classique est limité à 4 Gio (en-tête sur 32 bits)


@dataclass
class Config:
    # Périphérique ALSA brut (pas plughw) ; vérifier avec `scripts/probe-xr18.sh`.
    device: str = "hw:CARD=XR18,DEV=0"
    channels: int = 18  # 16 entrées + mix général (stéréo) = 18 voies USB
    rate: int = 48000  # 44100 ou 48000
    format: str = "S32_LE"  # ou S24_3LE : à confirmer avec les paramètres matériels de l'XR18
    # Emplacement par défaut (carte SD). Les disques USB montés sont proposés en plus (usb_auto)
    # et le choix se fait dans l'interface ; il est mémorisé dans state_file.
    output_dir: str = "/var/lib/xr18rec/recordings"
    usb_auto: bool = True
    update_repo: str = "xjimenez59/xr18-recorder"  # dépôt GitHub public où chercher les mises à jour
    state_file: str = "/var/lib/xr18rec/state.json"
    # Réglages modifiés depuis la page /settings : appliqués par-dessus ce fichier de configuration.
    settings_file: str = "/var/lib/xr18rec/settings.json"
    segment_seconds: int = 900  # un fichier toutes les 15 min (reste < 4 Gio)
    min_free_gb: float = 2.0  # refuse de démarrer sous ce seuil, arrête si le seuil est franchi
    arecord_bin: str = "arecord"
    host: str = "0.0.0.0"
    port: int = 8080
    # Adresse IP de l'XR18 pour lire noms/couleurs des voies (OSC) ; vide = désactivé.
    xr18_host: str = ""
    xr18_osc_port: int = 10024
    # Mix de retour (page /mix) : bus Aux proposés (1..6) et plafond des départs, par sécurité (in-ear).
    mix_buses: list = field(default_factory=lambda: [1, 2, 3, 4, 5, 6])
    mix_max_db: float = 0.0

    @property
    def bytes_per_second(self) -> int:
        return self.channels * self.rate * FORMAT_BYTES[self.format]

    @property
    def segment_bytes(self) -> int:
        return self.bytes_per_second * self.segment_seconds

    def validate(self) -> None:
        if self.format not in FORMAT_BYTES:
            raise ValueError(f"format inconnu : {self.format} (attendu : {', '.join(FORMAT_BYTES)})")
        if not 1 <= self.channels <= 64:
            raise ValueError("channels hors de 1..64")
        if self.rate not in (44100, 48000):
            raise ValueError("rate doit valoir 44100 ou 48000")
        if self.segment_seconds < 1:
            raise ValueError("segment_seconds doit être >= 1")
        if self.segment_bytes >= WAV_LIMIT - 64 * 1024**2:
            max_s = (WAV_LIMIT - 64 * 1024**2) // self.bytes_per_second
            raise ValueError(
                f"segment trop long : {self.segment_bytes / 1024**3:.2f} Gio >= limite WAV (4 Gio). "
                f"Maximum avec ces réglages : {max_s} s"
            )
        if self.min_free_gb < 0:
            raise ValueError("min_free_gb < 0")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.update_repo or ""):
            raise ValueError("update_repo : attendu « compte/dépôt »")
        if not all(isinstance(b, int) and 1 <= b <= 6 for b in self.mix_buses):
            raise ValueError("mix_buses : entiers de 1 à 6")
        if not -90 <= self.mix_max_db <= 10:
            raise ValueError("mix_max_db hors de -90..10")


def load(path: str | None = None) -> Config:
    cfg = Config()
    if path:
        with open(path, encoding="utf-8-sig") as f:  # tolère le BOM ajouté par certains éditeurs Windows
            raw = json.load(f)
        known = {f.name for f in fields(Config)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"clés inconnues dans {path} : {', '.join(sorted(unknown))}")
        for k, v in raw.items():
            setattr(cfg, k, v)
    cfg.validate()
    return cfg


# Réglages modifiables depuis l'interface (les autres exigent de modifier le fichier de configuration).
EDITABLE = ("xr18_host", "device", "rate", "format", "channels", "segment_seconds", "min_free_gb",
            "mix_max_db", "mix_buses")
AUDIO_KEYS = ("device", "rate", "format", "channels", "segment_seconds")  # figés pendant un enregistrement


def apply_overlay(cfg: Config) -> dict:
    """Applique les réglages enregistrés par l'interface ; renvoie ce qui a été appliqué.

    Un fichier absent ou invalide est ignoré (la configuration de base reste valable).
    """
    try:
        with open(cfg.settings_file, encoding="utf-8-sig") as f:
            raw = json.load(f)
        changes = {k: v for k, v in raw.items() if k in EDITABLE}
        dataclasses.replace(cfg, **changes).validate()
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    for k, v in changes.items():
        setattr(cfg, k, v)
    return changes


def read_overlay(cfg: Config) -> dict:
    try:
        with open(cfg.settings_file, encoding="utf-8-sig") as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items() if k in EDITABLE} if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_overlay(cfg: Config, changes: dict) -> None:
    """Ajoute `changes` aux réglages de l'interface (écriture atomique)."""
    merged = {**read_overlay(cfg), **changes}
    os.makedirs(os.path.dirname(cfg.settings_file) or ".", exist_ok=True)
    tmp = cfg.settings_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg.settings_file)


def clear_overlay(cfg: Config) -> None:
    try:
        os.remove(cfg.settings_file)
    except OSError:
        pass


def dump(cfg: Config) -> str:
    return json.dumps(asdict(cfg), indent=2, ensure_ascii=False)
