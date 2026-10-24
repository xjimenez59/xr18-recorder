"""État partagé de l'application : configuration modifiable à chaud, pont vers l'XR18, redémarrage."""

from __future__ import annotations

import dataclasses
from typing import Callable

from . import config as config_mod
from .mixer import MixerClient
from .session import Recorder, RecorderError
from .updates import UpdateService

_TYPES = {"xr18_host": str, "device": str, "rate": int, "format": str, "channels": int,
          "segment_seconds": int, "min_free_gb": float, "mix_max_db": float, "mix_buses": list}


class App:
    def __init__(self, cfg: config_mod.Config, rec: Recorder, mixer: MixerClient | None = None,
                 base_path: str | None = None, restart: Callable[[], None] | None = None):
        self.cfg, self.rec, self.mixer = cfg, rec, mixer
        self.updates = UpdateService(cfg, rec)
        self.base_path = base_path  # fichier de configuration de base (pour « revenir aux réglages d'origine »)
        self.restart = restart

    # ---- lecture ----------------------------------------------------------
    def settings(self) -> dict:
        overlay = config_mod.read_overlay(self.cfg)
        return {"values": {k: getattr(self.cfg, k) for k in config_mod.EDITABLE},
                "modified": sorted(overlay), "recording": self.rec.is_recording(),
                "audio_keys": list(config_mod.AUDIO_KEYS)}

    # ---- écriture ---------------------------------------------------------
    @staticmethod
    def _coerce(key: str, value):
        want = _TYPES[key]
        if want is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} : nombre attendu")
            return float(value)
        if want is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} : entier attendu")
            return value
        if want is list:
            if not isinstance(value, list):
                raise ValueError(f"{key} : liste attendue")
            return value
        if not isinstance(value, str):
            raise ValueError(f"{key} : texte attendu")
        return value.strip()

    def apply(self, changes: dict) -> dict:
        """Valide, applique à chaud et mémorise. ValueError si invalide, RecorderError si interdit maintenant."""
        unknown = set(changes) - set(config_mod.EDITABLE)
        if unknown:
            raise ValueError(f"réglage non modifiable : {', '.join(sorted(unknown))}")
        changes = {k: self._coerce(k, v) for k, v in changes.items()}
        changes = {k: v for k, v in changes.items() if getattr(self.cfg, k) != v}  # seulement ce qui change
        if self.rec.is_recording() and set(changes) & set(config_mod.AUDIO_KEYS):
            raise RecorderError("réglages audio verrouillés pendant un enregistrement")
        dataclasses.replace(self.cfg, **changes).validate()  # lève ValueError avec un message lisible
        self._apply_live(changes)
        if changes:
            config_mod.save_overlay(self.cfg, changes)
        return self.settings()

    def _apply_live(self, changes: dict) -> None:
        for k, v in changes.items():
            setattr(self.cfg, k, v)
        if "xr18_host" in changes:  # nouvelle console : nouveau client OSC
            if self.mixer:
                self.mixer.close()
            self.mixer = (MixerClient(self.cfg.xr18_host, self.cfg.xr18_osc_port, self.cfg.mix_max_db,
                                      self.cfg.mix_buses) if self.cfg.xr18_host else None)
        elif self.mixer:
            self.mixer.max_db = self.cfg.mix_max_db
            self.mixer.buses = [b for b in self.cfg.mix_buses if 1 <= b <= 6]

    def reset(self) -> dict:
        """Abandonne les réglages faits dans l'interface : retour au fichier de configuration."""
        if self.rec.is_recording():
            raise RecorderError("impossible pendant un enregistrement")
        base = config_mod.load(self.base_path) if self.base_path else config_mod.Config()
        config_mod.clear_overlay(self.cfg)
        self._apply_live({k: getattr(base, k) for k in config_mod.EDITABLE if getattr(self.cfg, k) != getattr(base, k)})
        return self.settings()

    def restart_app(self) -> None:
        if self.rec.is_recording():
            raise RecorderError("impossible pendant un enregistrement")
        if not self.restart:
            raise RecorderError("redémarrage indisponible dans ce mode")
        self.restart()
