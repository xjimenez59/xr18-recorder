"""Côté application : trouver, recevoir et demander l'installation d'une mise à jour.

L'application n'a aucun droit sur le programme installé : elle dépose le paquet dans le dossier d'échange
(`/var/lib/xr18rec/update`), le fait vérifier (même code que l'installateur, pour répondre tout de suite),
puis écrit un fichier `ready` qui déclenche le service root `xr18rec-update` (voir `updater.py`).
Trois sources, vérifiées de la même façon : disque USB, envoi depuis la page, téléchargement sur Internet.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
import urllib.error
import urllib.request

from . import storage, updater
from .updater import UpdateError

GITHUB_API = "https://api.github.com"  # remplaçable dans les tests
STAGED = "incoming.tar"
NAME_RE = re.compile(r"xr18rec-update-(\d+\.\d+\.\d+)\.tar")
USER_AGENT = "xr18rec-updater"


class UpdateService:
    def __init__(self, cfg, recorder):
        self.cfg, self.rec = cfg, recorder

    # ---- fichier déposé -------------------------------------------------------
    def _path(self, name: str) -> str:
        return os.path.join(updater.STATE_DIR, name)

    def _stage_file(self, part: str, source: str, size: int, label: str | None = None) -> dict:
        """Vérifie le fichier reçu (`part`) et, s'il est valide, en fait le paquet prêt à installer."""
        try:
            man = updater.inspect(part)
        except UpdateError:
            os.remove(part)
            raise
        os.replace(part, self._path(STAGED))
        info = {"version": man["version"], "notes": man.get("notes", ""), "source": source, "size": size}
        with open(self._path("staged.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        return info

    def _receive(self, src, limit: int = updater.MAX_BUNDLE) -> tuple[str, int]:
        os.makedirs(updater.STATE_DIR, exist_ok=True)
        part = self._path(STAGED + ".part")
        try:
            size = updater._write_limited(src, part, limit)
        except Exception:
            if os.path.exists(part):
                os.remove(part)
            raise
        return part, size

    def staged(self) -> dict | None:
        try:
            with open(self._path("staged.json"), encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            return None
        return info if os.path.isfile(self._path(STAGED)) else None

    # ---- sources ----------------------------------------------------------------
    def stage_stream(self, src, length: int) -> dict:
        """Envoi depuis la page : `length` octets lus sur `src`."""
        if length <= 0 or length > updater.MAX_BUNDLE:
            raise UpdateError("fichier de mise à jour absent ou trop volumineux")
        part, size = self._receive(_Limited(src, length))
        return self._stage_file(part, "envoi depuis la page", size)

    def usb_candidates(self) -> list[dict]:
        """Fichiers `xr18rec-update-X.Y.Z.tar` à la racine des disques USB montés (avec le résultat de la vérification)."""
        out = []
        for dev, mount, _fs in storage.read_mounts():
            if not dev.startswith(storage._USB_DEVICES) or mount == "/":
                continue
            for path in sorted(glob.glob(os.path.join(glob.escape(mount), "xr18rec-update-*.tar"))):
                m = NAME_RE.fullmatch(os.path.basename(path))
                entry = {"path": path, "name": os.path.basename(path), "disk": os.path.basename(mount.rstrip("/")) or dev,
                         "version": m.group(1) if m else None, "error": None}
                try:
                    entry["version"] = updater.inspect(path)["version"]
                except UpdateError as exc:
                    entry["error"] = str(exc)
                out.append(entry)
        return out

    def stage_usb(self, path: str) -> dict:
        if path not in {c["path"] for c in self.usb_candidates() if not c["error"]}:
            raise UpdateError("fichier de mise à jour introuvable sur les disques branchés")
        with open(path, "rb") as src:
            part, size = self._receive(src)
        return self._stage_file(part, "disque USB", size)

    def _github_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise UpdateError("aucune version publiée trouvée (dépôt privé ou sans release)") from exc
            raise UpdateError(f"GitHub a répondu avec l'erreur {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise UpdateError("pas d'accès à Internet depuis le Pi (ou GitHub injoignable)") from exc

    def check_online(self) -> dict:
        data = self._github_json(f"{GITHUB_API}/repos/{self.cfg.update_repo}/releases/latest")
        tag = str(data.get("tag_name", ""))
        asset = next((a for a in data.get("assets", []) if NAME_RE.fullmatch(str(a.get("name", "")))), None)
        current = updater.current_version()
        version = (updater.parse_version(tag) and ".".join(map(str, updater.parse_version(tag)))) or None
        newer = bool(version and updater.parse_version(version) > (updater.parse_version(current) or (0, 0, 0)))
        return {"current": current, "latest": version, "tag": tag, "newer": newer and asset is not None,
                "has_package": asset is not None, "url": asset.get("browser_download_url") if asset else None,
                "notes": str(data.get("body") or "")[:600]}

    def stage_online(self) -> dict:
        info = self.check_online()
        if not info["has_package"]:
            raise UpdateError("cette version n'a pas de paquet de mise à jour (seulement une image)")
        if not info["newer"]:
            raise UpdateError(f"déjà à jour (version {info['current']})")
        req = urllib.request.Request(info["url"], headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                part, size = self._receive(r)
        except (urllib.error.URLError, OSError) as exc:
            raise UpdateError("téléchargement impossible : " + str(exc)) from exc
        return self._stage_file(part, "Internet", size)

    # ---- installation -------------------------------------------------------------
    def install(self, allow_downgrade: bool = False) -> dict:
        from .session import RecorderError

        info = self.staged()
        if not info:
            raise UpdateError("aucune mise à jour préparée")
        if self.rec.is_recording():
            raise RecorderError("un enregistrement est en cours : la mise à jour redémarrerait l'application")
        current = updater.current_version()
        newer = updater.parse_version(info["version"]) > (updater.parse_version(current) or (0, 0, 0))
        if not newer and not allow_downgrade:
            raise UpdateError(f"la version {info['version']} n'est pas plus récente que la version installée ({current})")
        with open(self._path("ready"), "w", encoding="utf-8") as f:  # déclenche xr18rec-update.path
            json.dump({"bundle": STAGED, "allow_downgrade": allow_downgrade}, f)
        return self.status()

    def status(self) -> dict:
        last = updater.read_state()
        if os.path.exists(self._path("ready")) and last["state"] != "running":  # demandée, pas encore prise en charge
            last = {"state": "requested", "message": "installation demandée", "version": "", "time": int(time.time())}
        return {"current": updater.current_version(), "last": last, "staged": self.staged(),
                "repo": self.cfg.update_repo, "can_verify": os.path.isfile(updater.PUBKEY)}


class _Limited:
    """Lit au plus `length` octets d'un flux (corps d'une requête HTTP)."""

    def __init__(self, src, length: int):
        self.src, self.left = src, length

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        n = self.left if n < 0 else min(n, self.left)
        data = self.src.read(n)
        self.left -= len(data)
        return data
